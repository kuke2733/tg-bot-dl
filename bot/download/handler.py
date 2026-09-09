from __future__ import annotations

import logging
import os
import time

from pyrogram.enums.parse_mode import ParseMode
from pyrogram.types import Message

from bot import folder
from bot.app import app, user
from bot.download.groups import MediaGroupCollector
from bot.download.manager import (
    downloads,
    find_rename_target,
    refresh_batch,
    register_rename_target,
    rename_targets,
    safe_edit,
)
from bot.download.names import (
    album_folder_name,
    extract_rename_name,
    media_file_size,
    relative_name,
    replace_filename,
    resolve_filename,
    sanitize_folder_name,
    unique_filename,
    unique_folder,
    with_media_extension,
)
from bot.download.types import Batch, BatchItem, Download
from bot.util import humanReadableSize


groups = MediaGroupCollector()
# chat_id -> (name, timestamp) 用于“先名字、后文件”（转发附带留言）
PENDING_RENAMES: dict[int, tuple[str, float]] = {}
RENAME_WINDOW_SECONDS = 30.0


def _chat_id(message: Message) -> int | None:
    return message.chat.id if message.chat else None


def remember_pending_rename(chat_id: int, name: str) -> None:
    PENDING_RENAMES[chat_id] = (name, time.time())


def take_pending_rename(chat_id: int | None) -> str | None:
    if chat_id is None:
        return None
    pending = PENDING_RENAMES.get(chat_id)
    if not pending:
        return None
    name, ts = pending
    if time.time() - ts > RENAME_WINDOW_SECONDS:
        PENDING_RENAMES.pop(chat_id, None)
        return None
    PENDING_RENAMES.pop(chat_id, None)
    return name


async def apply_rename(download: Download, raw_name: str) -> str:
    filename = with_media_extension(raw_name, download.from_message)
    if download.started:
        download.pending_rename = raw_name.strip()
        return replace_filename(download.filename, filename)

    directory = os.path.dirname(os.path.join(folder.get(), download.filename.replace("/", os.sep)))
    used = {filename.lower()}
    if download.batch:
        for sibling in download.batch.items:
            if sibling.download_id != download.id:
                used.add(sibling.name.lower())
    filename = unique_filename(filename, directory, used)
    download.filename = replace_filename(download.filename, filename)
    if download.batch_item:
        download.batch_item.name = filename
    if download.batch:
        await refresh_batch(download.batch, force=True)
    else:
        size = download.expected_size or download.size
        size_text = f"（{humanReadableSize(size)}）" if size else ""
        await safe_edit(
            download.progress_message,
            f"文件 `{download.filename}` 已加入下载队列{size_text}。\n"
            f"想改名请回复本条进度消息，直接发新名字。",
            parse_mode=ParseMode.MARKDOWN,
        )
    return download.filename


async def rename_batch_folder(batch: Batch, raw_name: str) -> str:
    folder_name = sanitize_folder_name(raw_name)
    if not folder_name:
        return batch.folder
    new_folder = unique_folder(folder_name)
    batch.folder = new_folder
    batch.directory = os.path.join(folder.get(), new_folder)
    for queued in list(rename_targets.values()):
        if queued.batch is batch and not queued.started:
            queued.filename = f"{new_folder}/{os.path.basename(queued.filename)}"
            if queued.batch_item:
                queued.batch_item.name = os.path.basename(queued.filename)
            register_rename_target(queued)
    await refresh_batch(batch, force=True)
    return new_folder


async def enqueue_file(
    message: Message,
    client,
    reply_to: Message | None = None,
    override: str | None = None,
    subfolder: str | None = None,
    use_caption_name: bool = True,
    used_names: set[str] | None = None,
    batch: Batch | None = None,
) -> None:
    if not override and batch is None:
        override = take_pending_rename(_chat_id(reply_to or message))
    filename = resolve_filename(message, override, use_caption_name=use_caption_name)
    directory = os.path.join(folder.get(), subfolder) if subfolder else folder.get()
    filename = unique_filename(filename, directory, used_names if used_names is not None else set())
    rel_filename = f"{subfolder}/{filename}" if subfolder else filename
    rel = relative_name(rel_filename)
    real_file = os.path.join(directory, filename)
    target = reply_to or message
    if any(item.id == message.id or item.filename == rel for item in downloads):
        logging.debug("跳过重复任务：%s %s", message.id, rel)
        return
    batch_item = None
    if batch is not None:
        batch_item = BatchItem(download_id=message.id, name=filename, status="waiting")
        batch.items.append(batch_item)
    if os.path.isfile(real_file):
        logging.debug("本地已存在：%s", real_file)
        if batch_item is not None:
            batch_item.status = "done"
            return
        await target.reply(text=f"文件 `{rel}` 已经存在。", quote=True)
        return

    size = media_file_size(message)
    if batch is None:
        size_text = f"（{humanReadableSize(size)}）" if size else ""
        logging.warning("收到文件，加入下载队列：%s %s", rel, size_text)
        progress = await target.reply(
            f"文件 `{rel}` 已加入下载队列{size_text}。\n"
            f"想改名请回复本条进度消息，直接发新名字。",
            quote=True,
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        logging.warning("收到文件，加入下载队列：%s", rel)
        progress = batch.message

    download = Download(
        client=client,
        id=message.id,
        filename=rel,
        from_message=message,
        progress_message=progress,
        expected_size=size,
        size=size,
        batch=batch,
        batch_item=batch_item,
    )
    downloads.append(download)
    register_rename_target(download)


async def enqueue_messages(
    messages: list[Message],
    client,
    reply_to: Message | None = None,
    override: str | None = None,
    notice: Message | None = None,
) -> None:
    messages = [message for message in messages if message and message.media]
    if not messages:
        return
    if not override:
        override = take_pending_rename(_chat_id(reply_to or messages[0]))
    if len(messages) == 1:
        if notice:
            try:
                await notice.delete()
            except Exception:
                logging.debug("无法删除分组提示消息", exc_info=True)
        await enqueue_file(
            messages[0],
            client,
            reply_to=reply_to or messages[0],
            override=override,
            use_caption_name=True,
        )
        return

    folder_name = sanitize_folder_name(override) if override else album_folder_name(messages)
    if not folder_name:
        folder_name = album_folder_name([])
    subfolder = unique_folder(folder_name)
    summary = (
        f"这一组 {len(messages)} 个文件将保存到文件夹 `{subfolder}`，"
        "下载进度会在这条消息里更新。\n"
        "回复本条消息可修改文件夹名称。"
    )
    target = reply_to or messages[0]
    if notice:
        try:
            await notice.edit(summary, parse_mode=ParseMode.MARKDOWN)
            status = notice
        except Exception:
            logging.debug("无法编辑分组提示消息", exc_info=True)
            status = await target.reply(summary, quote=True, parse_mode=ParseMode.MARKDOWN)
    else:
        status = await target.reply(summary, quote=True, parse_mode=ParseMode.MARKDOWN)

    batch = Batch(
        id=str(getattr(messages[0], "media_group_id", messages[0].id)),
        folder=subfolder,
        total=len(messages),
        message=status,
        directory=os.path.join(folder.get(), subfolder),
    )

    from bot.download.manager import active_batches
    active_batches[batch.id] = batch

    used_names: set[str] = set()
    for message in messages:
        await enqueue_file(
            message,
            client,
            reply_to=reply_to or message,
            subfolder=subfolder,
            use_caption_name=False,
            used_names=used_names,
            batch=batch,
        )
    await refresh_batch(batch, force=True)


async def addFile(_, message: Message) -> None:
    try:
        consumed = await groups.add(
            message,
            app,
            lambda items, notice: enqueue_messages(items, app, notice=notice),
        )
        if not consumed:
            await enqueue_file(message, app, reply_to=message)
    except Exception:
        logging.exception("处理文件失败：%s", message.id)
        try:
            await message.reply("处理这个文件时出错了，请看日志。", quote=True)
        except Exception:
            pass
        raise


async def addFileFromUser(fileMessage: Message, linkMessage: Message) -> None:
    override = None
    parts = (linkMessage.text or "").split()
    if len(parts) >= 3:
        override = " ".join(parts[2:]).strip() or None
    messages = [fileMessage]
    try:
        grouped = await user.get_media_group(fileMessage.chat.id, fileMessage.id)
        if grouped:
            messages = [item for item in grouped if item and item.media]
    except ValueError:
        pass
    except Exception:
        logging.warning("通过链接获取整组文件失败", exc_info=True)
    await enqueue_messages(messages, user, reply_to=linkMessage, override=override)


async def renameFromText(_, message: Message) -> None:
    """改名：回复进度消息；或先发名字再发文件。一组文件只改文件夹名。"""
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    name = extract_rename_name(text)
    if not name:
        return

    replied = message.reply_to_message
    if replied:
        download = find_rename_target(replied.id)
        batch = download.batch if download and download.batch else None
        if batch is None:
            for item in list(rename_targets.values()):
                if item.batch and item.batch.message and item.batch.message.id == replied.id:
                    batch = item.batch
                    break

        # 一组文件：只允许改文件夹名
        if batch is not None:
            new_folder = await rename_batch_folder(batch, name)
            await message.reply(
                f"文件夹已改为 `{new_folder}`。",
                quote=True,
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if download is None:
            return
        new_path = await apply_rename(download, name)
        note = " 下载完成后生效" if download.started else ""
        await message.reply(
            f"已重命名为 `{new_path}`{note}。",
            quote=True,
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # 没有回复：只作为“先名字后文件”的挂起名
    chat_id = _chat_id(message)
    if chat_id is not None:
        remember_pending_rename(chat_id, name)

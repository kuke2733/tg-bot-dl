from __future__ import annotations

import logging
import os
import time

from pyrogram.enums.parse_mode import ParseMode
from pyrogram.types import InlineKeyboardMarkup, Message

from bot import folder
from bot.app import BASE_FOLDER, app, user
from bot.download.groups import MediaGroupCollector
from bot.download import persist as queue_persist
from bot.download import state
from bot.download.batches import refresh_batch
from bot.download.dedup import (
    UNIQUE_DUP_TEXT,
    mark_batch_unique_duplicate,
    register_unique_prompt,
    unique_duplicate_keyboard,
)
from bot.download.render import safe_edit
from bot.download.state import (
    active_batches,
    downloads,
    emit_download_event,
    find_rename_target,
    is_unique_duplicate,
    queue_download,
    register_rename_target,
    rename_targets,
)
from bot.download.names import (
    album_folder_name,
    extract_rename_name,
    media_file_size,
    media_file_unique_id,
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
# chat_id -> (name, timestamp)，转发时「先名字后文件」
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


async def _status_message(
    target: Message,
    text: str,
    seed: Message | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    if seed is not None:
        try:
            await seed.edit(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
            return seed
        except Exception:
            logging.debug("编辑状态消息失败，改为新发", exc_info=True)
    return await app.send_message(
        target.chat.id,
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_to_message_id=target.id,
        reply_markup=reply_markup,
    )


def _link_override(message: Message) -> str | None:
    parts = (message.text or "").split()
    if len(parts) < 3:
        return None
    return " ".join(parts[2:]).strip() or None


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
            f"文件 `{download.filename}` 已加入下载队列{size_text}。",
            parse_mode=ParseMode.MARKDOWN,
        )
    return download.filename


def _move_download_into_folder(download: Download, folder_name: str) -> None:
    download.filename = f"{folder_name}/{os.path.basename(download.filename)}"
    if download.batch_item:
        download.batch_item.name = os.path.basename(download.filename)


async def rename_batch_folder(batch: Batch, raw_name: str) -> str:
    folder_name = sanitize_folder_name(raw_name)
    if not folder_name:
        return batch.folder
    new_folder = unique_folder(folder_name)
    batch.folder = new_folder
    batch.directory = os.path.join(folder.get(), new_folder)
    for queued in list(rename_targets.values()):
        if queued.batch is batch and not queued.started:
            _move_download_into_folder(queued, new_folder)
            register_rename_target(queued)
    for pending in list(batch.pending_unique):
        _move_download_into_folder(pending, new_folder)
    await refresh_batch(batch, force=True)
    return new_folder


def _make_download(
    message: Message,
    client,
    rel: str,
    progress: Message,
    size: int,
    unique_id: str,
    batch: Batch | None,
    batch_item: BatchItem | None,
    quiet: bool = False,
) -> Download:
    return Download(
        client=client,
        id=message.id,
        filename=rel,
        from_message=message,
        progress_message=progress,
        expected_size=size,
        size=size,
        batch=batch,
        batch_item=batch_item,
        unique_id=unique_id,
        quiet=quiet,
    )


async def enqueue_file(
    message: Message,
    client,
    reply_to: Message | None = None,
    override: str | None = None,
    subfolder: str | None = None,
    use_caption_name: bool = True,
    used_names: set[str] | None = None,
    batch: Batch | None = None,
    progress_seed: Message | None = None,
    base: str | None = None,
    quiet: bool = False,
    notice_chat_id: int | None = None,
) -> None:
    if not override and batch is None:
        override = take_pending_rename(_chat_id(reply_to or message))
    base_dir = base if base is not None else folder.get()
    filename = resolve_filename(message, override, use_caption_name=use_caption_name)
    directory = os.path.join(base_dir, subfolder) if subfolder else base_dir
    filename = unique_filename(filename, directory, used_names if used_names is not None else set())
    # directory 已含 subfolder，相对路径直接拼文件名；再拼一次会把相册套进同名子文件夹
    rel_dir = os.path.relpath(directory, BASE_FOLDER).replace(os.sep, "/").strip("/")
    rel = f"{rel_dir}/{filename}" if rel_dir and rel_dir != "." else filename
    real_file = os.path.join(directory, filename)
    target = reply_to or message
    if any(item.id == message.id or item.filename == rel for item in downloads):
        logging.debug("跳过重复任务：%s %s", message.id, rel)
        if batch is None:
            if quiet:
                emit_download_event("skipped", {"filename": rel, "reason": "已在下载队列", "quiet": True, "chat_id": _chat_id(message)})
            else:
                await _status_message(target, f"文件 `{rel}` 已在下载队列中。", progress_seed)
        return

    size = media_file_size(message)
    unique_id = media_file_unique_id(message)
    batch_item = None
    if batch is not None:
        batch_item = BatchItem(download_id=message.id, name=filename, status="waiting")
        batch.items.append(batch_item)

    if is_unique_duplicate(unique_id):
        logging.warning("下载前命中重复：%s unique_id=%s", rel, unique_id)
        if batch is not None:
            download = _make_download(
                message, client, rel, batch.message, size, unique_id, batch, batch_item, quiet
            )
            mark_batch_unique_duplicate(download)
            return
        if quiet:
            emit_download_event("skipped", {"filename": rel, "reason": "重复文件", "quiet": True, "chat_id": _chat_id(message)})
            return
        download = _make_download(message, client, rel, target, size, unique_id, None, None)
        token = register_unique_prompt(download)
        progress = await _status_message(
            target,
            UNIQUE_DUP_TEXT,
            progress_seed,
            reply_markup=unique_duplicate_keyboard(token),
        )
        download.progress_message = progress
        return

    if os.path.isfile(real_file):
        logging.debug("本地已存在：%s", real_file)
        if batch_item is not None:
            batch_item.status = "done"
            return
        if quiet:
            emit_download_event("skipped", {"filename": rel, "reason": "文件已存在", "quiet": True, "chat_id": _chat_id(message)})
            return
        await _status_message(target, f"文件 `{rel}` 已经存在。", progress_seed)
        return

    if batch is None:
        size_text = f"（{humanReadableSize(size)}）" if size else ""
        logging.warning("收到文件，加入下载队列：%s %s unique_id=%s", rel, size_text, unique_id or "-")
        if quiet:
            progress = await app.send_message(
                notice_chat_id,
                f"📥 `{rel}`{size_text}",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            progress = await _status_message(
                target,
                f"文件 `{rel}` 已加入下载队列{size_text}。",
                progress_seed,
            )
    else:
        logging.warning("收到文件，加入下载队列：%s unique_id=%s", rel, unique_id or "-")
        progress = batch.message

    queue_download(
        _make_download(message, client, rel, progress, size, unique_id, batch, batch_item, quiet)
    )


async def enqueue_messages(
    messages: list[Message],
    client,
    reply_to: Message | None = None,
    override: str | None = None,
    notice: Message | None = None,
    base: str | None = None,
    quiet: bool = False,
    notice_chat_id: int | None = None,
) -> None:
    messages = [message for message in messages if message and message.media]
    if not messages:
        if not quiet and notice is not None and reply_to is not None:
            await _status_message(reply_to, "这条链接对应的消息里没有文件。", notice)
        return
    if not override:
        override = take_pending_rename(_chat_id(reply_to or messages[0]))
    if len(messages) == 1:
        await enqueue_file(
            messages[0],
            client,
            reply_to=reply_to or messages[0],
            override=override,
            use_caption_name=True,
            progress_seed=notice,
            base=base,
            quiet=quiet,
            notice_chat_id=notice_chat_id,
        )
        return

    base_dir = base if base is not None else folder.get()
    folder_name = sanitize_folder_name(override) if override else album_folder_name(messages)
    if not folder_name:
        folder_name = album_folder_name([])
    subfolder = unique_folder(folder_name, base_dir)
    if quiet:
        target = None
        status = await app.send_message(
            notice_chat_id,
            f"📥 相册 `{subfolder}`（{len(messages)} 个文件）",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        summary = f"{len(messages)} 个文件 → 文件夹 `{subfolder}`，回复本条可改名。"
        target = reply_to or messages[0]
        status = await _status_message(target, summary, notice)

    batch = Batch(
        id=str(getattr(messages[0], "media_group_id", messages[0].id)),
        folder=subfolder,
        total=len(messages),
        message=status,
        directory=os.path.join(base_dir, subfolder),
        quiet=quiet,
    )
    active_batches[batch.id] = batch
    queue_persist.add_batch(queue_persist.batch_record(batch))

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
            base=base,
            quiet=quiet,
            notice_chat_id=notice_chat_id,
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
            await message.reply("处理这个文件时出错了，请看日志。")
        except Exception:
            pass
        raise


async def addFromLink(link_message: Message, chat: int | str, message_id: int) -> None:
    notice = await _status_message(link_message, "正在读取这条消息…")
    try:
        fetched = await user.get_messages(chat, [message_id])
    except Exception:
        logging.exception("通过用户账号读取消息失败 chat=%s message_id=%s", chat, message_id)
        await _status_message(
            link_message,
            "用户账号读不到这条消息，请确认已加入该频道。",
            notice,
        )
        return

    file_message = fetched[0] if fetched else None
    if not file_message or not file_message.media:
        await _status_message(link_message, "这条链接对应的消息里没有文件。", notice)
        return

    messages = [file_message]
    try:
        grouped = await user.get_media_group(file_message.chat.id, file_message.id)
        if grouped:
            messages = [item for item in grouped if item and item.media] or messages
    except ValueError:
        pass
    except Exception:
        logging.warning("通过链接获取整组文件失败", exc_info=True)

    try:
        await enqueue_messages(
            messages,
            user,
            reply_to=link_message,
            override=_link_override(link_message),
            notice=notice,
        )
    except Exception:
        logging.exception("通过链接加入下载失败 chat=%s message_id=%s", chat, message_id)
        await _status_message(link_message, "处理这条链接时出错了，请看日志。", notice)


async def renameFromText(_, message: Message) -> None:
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

        if batch is not None:
            new_folder = await rename_batch_folder(batch, name)
            await message.reply(
                f"文件夹已改为 `{new_folder}`。",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if download is None:
            return
        new_path = await apply_rename(download, name)
        note = " 下载完成后生效" if download.started else ""
        await message.reply(
            f"已重命名为 `{new_path}`{note}。",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chat_id = _chat_id(message)
    if chat_id is not None:
        remember_pending_rename(chat_id, name)

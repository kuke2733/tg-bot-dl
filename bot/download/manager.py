from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from time import time

from pyrogram.client import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.app import BASE_FOLDER, MAX_SIMULTANEOUS_TRANSMISSIONS, app, user
from bot.download import persist as queue_persist
from bot.download.cleanup import (
    cleanup_batch_files,
    cleanup_partial_download,
    schedule_cleanup_retry,
)
from bot.download.fileformat import align_filename_to_container
from bot.download.names import replace_filename, unique_filename, with_media_extension
from bot.download.store import (
    delete_by_unique_id,
    find_by_sha256,
    find_by_unique_id,
    hash_file,
    init as init_store,
    remember,
)
from bot.download.transfer import download_with_resume
from bot.download.types import Batch, BatchItem, Download
from bot.util import humanReadableSize, humanReadableTime


downloads: list[Download] = []
running = 0
stop: list[int] = []
active_batches: dict[str, Batch] = {}
# 正在执行 downloadFile 的任务；还在排队的任务只出现在 downloads 里
active_downloads: list[Download] = []
# /pause 暂停出队（进行中的继续），/resume 恢复
paused = False
# 下载事件回调（如频道监听的摘要），签名 (kind, info)
download_event_listeners: list = []
# 进度消息 / 原消息 id -> 仍可改名的下载任务
rename_targets: dict[int, Download] = {}


@dataclass
class HashPrompt:
    download: Download
    path: str
    sha256: str
    success_text: str
    prompt_message: Message


in_flight_unique_ids: set[str] = set()
pending_unique: dict[str, Download] = {}
pending_hash: dict[str, HashPrompt] = {}

BATCH_CLEANUP_TIMEOUT = 15.0
FILE_HANDLE_RELEASE_WAIT = 1.5
BATCH_CLEANUP_CHECK_INTERVAL = 0.3
# 停止后等待传输协程自行退出的时间；断线重试等场景卡住不动就强制取消
STOP_CANCEL_GRACE = 10.0
STOPPED_TEXT = "已停止并删除"
UNIQUE_DUP_TEXT = "这个文件下载过，取消还是继续？"
HASH_DUP_TEXT = "和历史下载的文件内容相同，保留还是删除？"

STATUS_MARK = {
    "done": "✅",
    "waiting": "⏳",
    "downloading": "⬇️",
    "failed": "❌",
    "stopped": "⏹",
    "deleted": "🗑️",
    "duplicate": "⚠️",
    "skipped": "⏭",
    "content_duplicate": "⚠️",
}

ITEM_STATUS_LABEL = {
    "done": "",
    "waiting": "等待中",
    "stopped": "已停止",
    "deleted": "已删除",
    "failed": "失败",
    "duplicate": "重复",
    "skipped": "已跳过",
    "content_duplicate": "内容重复",
}


def register_rename_target(download: Download) -> None:
    if download.from_message:
        rename_targets[download.from_message.id] = download
    if download.progress_message:
        rename_targets[download.progress_message.id] = download
    if download.batch and download.batch.message:
        rename_targets[download.batch.message.id] = download


def unregister_rename_target(download: Download) -> None:
    for message_id, item in list(rename_targets.items()):
        if item is download:
            rename_targets.pop(message_id, None)


def find_rename_target(message_id: int) -> Download | None:
    return rename_targets.get(message_id)


def set_paused(value: bool) -> None:
    global paused
    paused = value


def emit_download_event(kind: str, info: dict) -> None:
    for callback in list(download_event_listeners):
        try:
            callback(kind, info)
        except Exception:
            logging.exception("下载事件回调失败")


def _event_info(download: Download) -> dict:
    chat_id = None
    if download.from_message is not None and download.from_message.chat is not None:
        chat_id = download.from_message.chat.id
    return {"filename": download.filename, "quiet": download.quiet, "chat_id": chat_id}


async def delete_message_later(message, delay: float = 10.0) -> None:
    """安静模式的临时进度消息：展示一小段时间后删除，结果已进频道摘要。"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as exc:
        logging.debug("删除临时消息失败：%s: %s", type(exc).__name__, exc)


def new_token() -> str:
    return secrets.token_hex(4)


def is_unique_duplicate(unique_id: str) -> bool:
    if not unique_id:
        return False
    if unique_id in in_flight_unique_ids:
        return True
    return find_by_unique_id(unique_id) is not None


def track_unique(unique_id: str) -> None:
    if unique_id:
        in_flight_unique_ids.add(unique_id)


def untrack_unique(unique_id: str) -> None:
    if not unique_id:
        return
    still = any(
        item.unique_id == unique_id and not item.stopped
        for item in list(downloads)
    ) or any(
        item.unique_id == unique_id and not item.stopped
        for item in list(rename_targets.values())
    )
    if not still:
        in_flight_unique_ids.discard(unique_id)


def queue_download(download: Download) -> None:
    track_unique(download.unique_id)
    downloads.append(download)
    register_rename_target(download)
    try:
        queue_persist.add_task(queue_persist.task_record(download))
    except Exception:
        logging.exception("持久化下载任务失败：%s", download.filename)


async def send_restore_message(chat_id: int, text: str, reply_to: int | None) -> Message | None:
    """发恢复提示；源消息和提示在同一个会话时带上引用。引用发送失败就退回纯文本，不中断恢复。"""
    try:
        return await app.send_message(
            chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_to_message_id=reply_to
        )
    except Exception:
        if reply_to is None:
            return None
    logging.debug("带引用发送恢复消息失败，改用纯文本重试", exc_info=True)
    try:
        return await app.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        return None


async def restore_saved_queue() -> None:
    """进程重启后恢复上次未完成的下载任务（配合 .temp 断点续传）。"""
    batches, tasks = queue_persist.load()
    if not tasks and not batches:
        return
    logging.warning(
        "发现上次未完成的下载任务：%d 个任务 / %d 个批次，开始恢复", len(tasks), len(batches)
    )
    rebuilt: dict[str, Batch] = {}

    # 每个批次取第一个源消息，恢复消息带上对原媒体消息的引用，方便跳回原帖
    batch_first_source: dict[str, tuple[int, int]] = {}
    for record in tasks.values():
        batch_id = record.get("batch_id")
        if batch_id and record.get("chat_id") is not None and record.get("message_id"):
            batch_first_source.setdefault(str(batch_id), (record["chat_id"], record["message_id"]))

    for batch_id, record in batches.items():
        chat_id = record.get("chat_id")
        if chat_id is None:
            continue
        try:
            if record.get("message_id"):
                fetched = await app.get_messages(chat_id, [record["message_id"]])
                old = fetched[0] if fetched else None
                if old is not None:
                    await safe_edit(
                        old,
                        f"文件夹 `{record['folder']}` 的任务已中断，机器人重启后重新排队，请看新的进度消息。",
                        parse_mode=ParseMode.MARKDOWN,
                    )
        except Exception:
            logging.debug("标注旧批次消息失败", exc_info=True)
        reply_to = None
        source = batch_first_source.get(batch_id)
        if source and source[0] == chat_id:
            reply_to = source[1]
        new_message = await send_restore_message(
            chat_id,
            f"🔄 恢复文件夹任务 `{record['folder']}`，下载进度会在这条消息里更新。",
            reply_to,
        )
        if new_message is None:
            logging.warning("发送批次恢复消息失败：%s", record["folder"])
            continue
        batch = Batch(
            id=batch_id,
            folder=record["folder"],
            total=0,
            message=new_message,
            directory=record.get("directory") or "",
        )
        active_batches[batch.id] = batch
        queue_persist.add_batch(queue_persist.batch_record(batch))
        rebuilt[batch_id] = batch

    restored = 0
    for key, record in tasks.items():
        try:
            ok = await _restore_task(key, record, rebuilt)
        except Exception:
            logging.exception("恢复下载任务失败：%s", record.get("filename"))
            ok = False
        if ok:
            restored += 1
        else:
            queue_persist.remove_task(key)
    for batch_id, batch in list(rebuilt.items()):
        if not batch.items:
            if active_batches.pop(batch_id, None) is not None:
                queue_persist.remove_batch(batch_id)
            await safe_edit(
                batch.message,
                f"文件夹 `{batch.folder}` 没有可恢复的任务（源消息可能已删除）。",
                parse_mode=ParseMode.MARKDOWN,
            )
    logging.warning("下载任务恢复完成：%d/%d", restored, len(tasks))


async def _restore_task(key: str, record: dict, rebuilt: dict[str, Batch]) -> bool:
    filename = record.get("filename") or ""
    if not filename:
        return False
    save_path = str(Path(BASE_FOLDER) / filename)
    if (Path(BASE_FOLDER) / filename).exists():
        logging.warning("恢复跳过：文件已存在（可能中断前刚完成）：%s", filename)
        return False
    client = user if record.get("client") == "user" else app
    if client is None:
        logging.warning("恢复跳过：需要用户账号但未配置：%s", filename)
        cleanup_partial_download(save_path, filename)
        return False
    chat_id = record.get("chat_id")
    message_id = record.get("message_id")
    if chat_id is None or not message_id:
        logging.warning("恢复跳过：缺少源消息位置：%s", filename)
        cleanup_partial_download(save_path, filename)
        return False
    try:
        fetched = await client.get_messages(chat_id, [message_id])
    except Exception:
        logging.exception("恢复任务取源消息失败：%s", filename)
        cleanup_partial_download(save_path, filename)
        return False
    source = fetched[0] if fetched else None
    if source is None or getattr(source, "empty", False) or not source.media:
        logging.warning("恢复跳过：源消息不存在或已删除：%s", filename)
        cleanup_partial_download(save_path, filename)
        return False

    batch = rebuilt.get(record.get("batch_id")) if record.get("batch_id") else None
    batch_item = None
    if batch is not None:
        batch.total += 1
        batch_item = BatchItem(download_id=source.id, name=Path(filename).name, status="waiting")
        batch.items.append(batch_item)

    download = Download(
        client=client,
        id=source.id,
        filename=filename,
        from_message=source,
        progress_message=batch.message if batch else None,
        expected_size=record.get("expected_size") or 0,
        size=record.get("expected_size") or 0,
        batch=batch,
        batch_item=batch_item,
        pending_rename=record.get("pending_rename"),
        unique_id=record.get("unique_id") or "",
    )
    if batch is None:
        old_progress = record.get("old_progress") or {}
        notice_chat = old_progress.get("chat_id")
        if notice_chat is None:
            logging.warning("恢复跳过：缺少进度消息位置：%s", filename)
            return False
        # 源媒体消息和恢复提示在同一个会话（私聊下载）时带上引用；频道任务的源在频道里，不能跨会话引用
        reply_to = message_id if chat_id == notice_chat else None
        download.progress_message = await send_restore_message(
            notice_chat,
            f"🔄 恢复下载任务 `{filename}`。",
            reply_to,
        )
        if download.progress_message is None:
            logging.warning("发送单任务恢复消息失败：%s", filename)
            return False
    queue_download(download)
    return True


def register_unique_prompt(download: Download) -> str:
    token = new_token()
    pending_unique[token] = download
    return token


def unique_duplicate_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("取消下载", callback_data=f"dupx {token}"),
                InlineKeyboardButton("继续下载", callback_data=f"dupc {token}"),
            ]
        ]
    )


def hash_duplicate_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("保留", callback_data=f"hkeep {token}"),
                InlineKeyboardButton("删除", callback_data=f"hdel {token}"),
            ]
        ]
    )


def mark_batch_unique_duplicate(download: Download) -> None:
    if download.batch_item:
        download.batch_item.status = "duplicate"
    if download.batch is not None:
        download.batch.pending_unique.append(download)


def ensure_unique_download_name(download: Download) -> None:
    rel = download.filename.replace("\\", "/")
    abs_path = Path(BASE_FOLDER) / rel
    used: set[str] = set()
    if download.batch:
        for sibling in download.batch.items:
            if sibling.download_id != download.id:
                used.add(sibling.name.lower())
    name = unique_filename(abs_path.name, str(abs_path.parent), used)
    download.filename = replace_filename(rel, name)
    if download.batch_item:
        download.batch_item.name = name


async def run() -> None:
    global running
    init_store()
    while True:
        if not paused:
            for download in list(downloads):
                if running == MAX_SIMULTANEOUS_TRANSMISSIONS:
                    break
                if download not in downloads:
                    continue
                downloads.remove(download)
                active_downloads.append(download)
                download.task = asyncio.create_task(downloadFile(download))
                logging.info("New download initialized: %s", download.filename)
                running += 1
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break


# FloodWait 后按会话暂停进度编辑，避免反复触发限流越罚越重
edit_cooldowns: dict[int, float] = {}


async def safe_edit(message, text: str, *, important: bool = False, **kwargs) -> None:
    """编辑消息；进度类编辑在限流冷却期直接跳过，important 的最终状态编辑仍会尝试。"""
    chat_id = message.chat.id if message.chat else None
    if not important and chat_id is not None and edit_cooldowns.get(chat_id, 0) > time():
        logging.debug("编辑冷却中，跳过进度刷新")
        return
    try:
        await message.edit(text=text, **kwargs)
    except FloodWait as exc:
        wait = int(getattr(exc, "value", 0) or 0)
        if chat_id is not None:
            edit_cooldowns[chat_id] = time() + wait + 1
        logging.warning("编辑消息触发限流 %s 秒，期间暂停该会话的进度刷新", wait)
    except Exception as exc:
        logging.debug("更新下载消息失败：%s: %s", type(exc).__name__, exc)


def batch_keyboard(batch: Batch) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if batch.pending_unique and not batch.stopped:
        rows.append(
            [
                InlineKeyboardButton("跳过重复", callback_data=f"bdupx {batch.id}"),
                InlineKeyboardButton("仍要下载", callback_data=f"bdupc {batch.id}"),
            ]
        )
    has_downloads = any(item.status in {"waiting", "downloading"} for item in batch.items)
    if has_downloads and not batch.stopped:
        rows.append([InlineKeyboardButton("停止全部", callback_data=f"stopb {batch.id}")])
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


def stop_keyboard(download_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("停止", callback_data=f"stop {download_id}")]]
    )


def progress_bar(percent: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(100.0, percent)) / 100 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def resolve_batch_item(download: Download) -> BatchItem | None:
    if download.batch_item is not None:
        return download.batch_item
    if download.batch is not None:
        return download.batch.item_for(download.id)
    return None


def item_speed_line(item: BatchItem) -> str:
    if item.status != "downloading" or not item.started or not item.received:
        return ""
    elapsed = max(time() - item.started, 1)
    avg_speed = item.received / elapsed
    if avg_speed <= 0:
        return ""
    if item.total and item.received < item.total:
        tte = int((item.total - item.received) / avg_speed)
        return f"{humanReadableSize(avg_speed)}/s，预计还需 {humanReadableTime(tte)}"
    return f"{humanReadableSize(avg_speed)}/s"


def render_item(item: BatchItem) -> str:
    mark = STATUS_MARK.get(item.status, "•")
    name = f"`{item.name}`"
    if item.status in ITEM_STATUS_LABEL:
        label = ITEM_STATUS_LABEL[item.status]
        return f"{mark} {name}" + (f"  {label}" if label else "")

    if item.total:
        percent = min(100.0, item.received / item.total * 100)
        bar = progress_bar(percent)
        size = f"{humanReadableSize(item.received)}/{humanReadableSize(item.total)}"
        line = f"{mark} {name}  `{bar}` {percent:0.0f}% {size}"
    elif item.received:
        line = f"{mark} {name}  已下载 {humanReadableSize(item.received)}"
    else:
        return f"{mark} {name}  下载中"

    speed = item_speed_line(item)
    return f"{line}\n__{speed}__" if speed else line


def render_batch(batch: Batch) -> str:
    total = len(batch.items) or batch.total
    if batch.finished >= total:
        parts = [f"完成 {batch.done}/{total}"]
        if batch.skipped:
            parts.append(f"跳过 {batch.skipped}")
        if batch.failed:
            parts.append(f"未完成 {batch.failed}")
        if batch.failed == 0 and batch.skipped == 0:
            status = f"全部完成 {batch.done}/{total}"
        else:
            status = "，".join(parts)
    else:
        status = f"进度 {batch.done}/{total}"
        if batch.skipped:
            status += f"，跳过 {batch.skipped}"
    body = "\n".join(render_item(item) for item in batch.items)
    return f"文件夹 `{batch.folder}`\n{status}\n\n{body}"


async def refresh_batch(batch: Batch, force: bool = False) -> None:
    now = time()
    if not force and batch.last_update and now - batch.last_update < 1:
        return
    batch.last_update = now
    active = batch.finished < (len(batch.items) or batch.total) and not batch.stopped
    await safe_edit(
        batch.message,
        render_batch(batch),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=batch_keyboard(batch) if active else None,
    )


async def wait_for_batch_cleanup(batch_id: str, timeout: float = BATCH_CLEANUP_TIMEOUT) -> bool:
    start_time = time()
    while (time() - start_time) < timeout:
        queued = any(d.batch and d.batch.id == batch_id for d in downloads)
        active = any(d.batch and d.batch.id == batch_id for d in rename_targets.values())
        if not queued and not active:
            await asyncio.sleep(FILE_HANDLE_RELEASE_WAIT)
            return True
        await asyncio.sleep(BATCH_CLEANUP_CHECK_INTERVAL)
    return False


async def finish_batch_item(batch: Batch) -> None:
    if batch.stopped:
        return
    await refresh_batch(batch, force=True)
    if batch.finished < (len(batch.items) or batch.total):
        return

    directory = Path(batch.directory) if batch.directory else None
    if directory and directory.is_dir() and batch.done == 0:
        try:
            if not list(directory.iterdir()):
                directory.rmdir()
                logging.warning("已删除空的分组文件夹：%s", batch.folder)
        except OSError:
            logging.debug("无法删除空文件夹：%s", directory, exc_info=True)
    if active_batches.pop(batch.id, None) is not None:
        queue_persist.remove_batch(batch.id)
    if batch.quiet and batch.message is not None:
        asyncio.create_task(delete_message_later(batch.message))


def mark_download_stopped(download: Download) -> None:
    download.stopped = True
    download.ui_seq += 1
    if download.id not in stop:
        stop.append(download.id)
    schedule_stop_watchdog(download)


def schedule_stop_watchdog(download: Download) -> None:
    """停止后给传输一段时间自行退出；卡住不动就强制取消任务，释放文件句柄。"""
    if download.cancel_scheduled or download.task is None or download.finalizing:
        return
    download.cancel_scheduled = True

    async def force_cancel() -> None:
        await asyncio.sleep(STOP_CANCEL_GRACE)
        task = download.task
        if task is not None and not task.done() and not download.finalizing:
            logging.warning(
                "下载 %s 停止 %s 秒后仍在传输（可能卡在断线重试），强制中断",
                download.filename,
                STOP_CANCEL_GRACE,
            )
            task.cancel()

    asyncio.create_task(force_cancel())


def find_download_by_id(download_id: int) -> Download | None:
    for item in list(rename_targets.values()):
        if item.id == download_id:
            return item
    for item in downloads:
        if item.id == download_id:
            return item
    return None


def _pop_stop(download_id: int) -> None:
    try:
        stop.remove(download_id)
    except ValueError:
        pass


async def finalize_single_stopped(download: Download, save_path: str) -> None:
    mark_download_stopped(download)
    _pop_stop(download.id)
    cleanup_partial_download(save_path, download.filename)
    await safe_edit(download.progress_message, STOPPED_TEXT, parse_mode=ParseMode.MARKDOWN, important=True)
    if download.quiet:
        asyncio.create_task(delete_message_later(download.progress_message))
    emit_download_event("stopped", _event_info(download))


async def finalize_queued_stopped(download: Download) -> None:
    """取消还没开始传输的排队任务：立即收尾，不等它出队。"""
    try:
        downloads.remove(download)
    except ValueError:
        pass
    await finalize_single_stopped(download, str(Path(BASE_FOLDER) / download.filename))
    try:
        queue_persist.remove_task(queue_persist.task_record(download)["key"])
    except Exception:
        logging.exception("移除持久化任务失败：%s", download.filename)
    unregister_rename_target(download)
    untrack_unique(download.unique_id)


async def handle_stopped_download(download: Download, item: BatchItem | None, save_path: str) -> None:
    if item:
        item.status = "deleted"
    if download.batch:
        _pop_stop(download.id)
        cleanup_partial_download(save_path, download.filename)
        await finish_batch_item(download.batch)
        return
    await finalize_single_stopped(download, save_path)


async def start_download_progress(download: Download, item: BatchItem | None) -> None:
    if download.batch is not None:
        if item:
            item.status = "downloading"
            item.name = Path(download.filename).name
            if not item.started:
                item.started = time()
        await refresh_batch(download.batch, force=True)
        return

    if download.expected_size:
        body = f"0/{humanReadableSize(download.expected_size)} 0.00%"
    else:
        body = "下载中..."
    text = dedent(
        f"""
        `{download.filename}`：
        __{body}__
        """
    )
    await safe_edit(
        download.progress_message,
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=stop_keyboard(download.id),
    )


def success_text_for(download: Download, actual_size: int, time_took: str, speed: str) -> str:
    return dedent(
        f"""
        文件 `{download.filename}` 已下载完成。
        共 {humanReadableSize(actual_size)}，用时 {time_took}，平均速度 __{speed}/s__
        """
    )


async def prompt_hash_duplicate(
    download: Download,
    path: str,
    sha256: str,
    success_text: str,
) -> None:
    token = new_token()
    item = resolve_batch_item(download)
    if download.batch:
        if item:
            item.status = "content_duplicate"
            item.name = Path(download.filename).name
        await finish_batch_item(download.batch)
        prompt_message = await download.batch.message.reply(
            f"`{Path(download.filename).name}`\n{HASH_DUP_TEXT}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=hash_duplicate_keyboard(token),
        )
    else:
        prompt_message = download.progress_message
        await safe_edit(
            prompt_message,
            HASH_DUP_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=hash_duplicate_keyboard(token),
            important=True,
        )
    pending_hash[token] = HashPrompt(
        download=download,
        path=path,
        sha256=sha256,
        success_text=success_text,
        prompt_message=prompt_message,
    )


async def finish_download_success(download: Download, item: BatchItem | None, result: str) -> None:
    result, download.filename = align_filename_to_container(result, download.filename)
    if download.pending_rename:
        result, download.filename = apply_pending_rename_on_disk(download, result)
    finished = time()
    seconds_took = max(finished - download.started, 1)
    actual_size = Path(result).stat().st_size if Path(result).exists() else (
        download.expected_size or download.size
    )

    # 完成校验：实际大小与 Telegram 报的大小不一致视为失败
    if result and download.expected_size and Path(result).exists() and actual_size != download.expected_size:
        logging.warning(
            "下载完成校验失败：%s 预期 %d 字节，实际 %d 字节",
            download.filename, download.expected_size, actual_size,
        )
        cleanup_partial_download(result, download.filename)
        note = f"大小不符（预期 {humanReadableSize(download.expected_size)}，实际 {humanReadableSize(actual_size)}），已删除"
        await handle_download_failure(download, item, note=note)
        return

    speed = humanReadableSize(actual_size / seconds_took)
    time_took = humanReadableTime(int(seconds_took))
    success_text = success_text_for(download, actual_size, time_took, speed)

    sha256 = ""
    try:
        sha256 = await asyncio.to_thread(hash_file, result)
    except OSError:
        logging.exception("计算文件哈希失败：%s", result)

    if sha256 and not download.skip_hash_check and find_by_sha256(sha256):
        logging.warning("下载后内容重复：%s sha256=%s", download.filename, sha256[:12])
        if download.quiet:
            if item:
                item.status = "content_duplicate"
                item.name = Path(download.filename).name
            if download.batch:
                await finish_batch_item(download.batch)
            else:
                await safe_edit(
                    download.progress_message,
                    f"`{download.filename}` 与历史下载内容相同，已保留。",
                    parse_mode=ParseMode.MARKDOWN,
                    important=True,
                )
                asyncio.create_task(delete_message_later(download.progress_message))
            emit_download_event("duplicate_keep", _event_info(download))
            return
        await prompt_hash_duplicate(download, result, sha256, success_text)
        return

    remember(download.unique_id, sha256, actual_size, download.filename)

    if download.batch:
        if item:
            item.status = "done"
            item.name = Path(download.filename).name
            item.received = actual_size
            item.total = actual_size
        await finish_batch_item(download.batch)
        emit_download_event("done", _event_info(download) | {"size": actual_size})
        return

    await safe_edit(
        download.progress_message,
        success_text,
        parse_mode=ParseMode.MARKDOWN,
        important=True,
    )
    if download.quiet:
        asyncio.create_task(delete_message_later(download.progress_message))
    emit_download_event("done", _event_info(download) | {"size": actual_size})


def apply_pending_rename_on_disk(download: Download, result_path: str) -> tuple[str, str]:
    raw = (download.pending_rename or "").strip()
    download.pending_rename = None
    if not raw:
        return result_path, download.filename

    filename = with_media_extension(raw, download.from_message)
    parent = Path(download.filename).parent
    directory = str(Path(BASE_FOLDER) / parent) if str(parent) != "." else str(BASE_FOLDER)
    filename = unique_filename(filename, directory, set())
    new_rel = replace_filename(download.filename, filename)
    new_abs = Path(BASE_FOLDER) / new_rel
    new_abs.parent.mkdir(parents=True, exist_ok=True)
    old = Path(result_path)
    if old.exists():
        if new_abs.exists():
            new_abs.unlink()
        old.replace(new_abs)
        return str(new_abs), new_rel
    logging.warning("下载完成后改名失败，找不到文件：%s", result_path)
    return result_path, download.filename


async def handle_download_failure(download: Download, item: BatchItem | None, note: str = "") -> None:
    if item:
        item.status = "failed"
    if download.batch:
        await finish_batch_item(download.batch)
    else:
        text = f"文件 `{download.filename}` 下载失败。"
        if note:
            text = f"文件 `{download.filename}` 下载失败：{note}"
        await safe_edit(
            download.progress_message,
            text,
            parse_mode=ParseMode.MARKDOWN,
            important=True,
        )
        if download.quiet:
            asyncio.create_task(delete_message_later(download.progress_message))
    emit_download_event("failed", _event_info(download) | ({"note": note} if note else {}))


async def downloadFile(download: Download) -> None:
    global running
    item = resolve_batch_item(download)
    save_path = str(Path(BASE_FOLDER) / download.filename)

    try:
        if download.id in stop or (download.batch and download.batch.stopped):
            await handle_stopped_download(download, item, save_path)
            return

        await start_download_progress(download, item)
        download.started = time()

        async def on_retry(attempt: int, exc: BaseException, resumed: int) -> None:
            logging.warning(
                "续传 %s（第 %d 次）：%s，已有 %s",
                download.filename,
                attempt,
                type(exc).__name__,
                humanReadableSize(resumed) if resumed else "0",
            )
            if download.batch is None and download.progress_message:
                await safe_edit(
                    download.progress_message,
                    f"`{download.filename}` 中断，正在续传（第 {attempt} 次）...",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=stop_keyboard(download.id),
                )

        result, download.from_message = await download_with_resume(
            download.client,
            download.from_message,
            save_path,
            progress=createProgress(download.client),
            progress_args=(download,),
            file_size=download.expected_size or download.size,
            on_retry=on_retry,
        )
        download.finalizing = True

        if not isinstance(result, str):
            if item:
                stopped = download.stopped or (download.batch and download.batch.stopped)
                item.status = "stopped" if stopped else "failed"
            if download.batch:
                await finish_batch_item(download.batch)
            elif download.stopped or download.id in stop:
                await finalize_single_stopped(download, save_path)
            else:
                await handle_download_failure(download, item)
            return

        await finish_download_success(download, item, result)

    except asyncio.CancelledError:
        # 停止看门狗强制中断：按已停止收尾，确保临时文件被清理、消息有交代
        logging.warning("下载 %s 被强制中断，按已停止处理", download.filename)
        if download.batch:
            await handle_stopped_download(download, item, save_path)
        else:
            await finalize_single_stopped(download, save_path)
        raise
    except Exception:
        logging.exception("下载失败：%s", download.filename)
        cleanup_partial_download(save_path, download.filename)
        await handle_download_failure(download, item)
    finally:
        try:
            active_downloads.remove(download)
        except ValueError:
            pass
        try:
            queue_persist.remove_task(queue_persist.task_record(download)["key"])
        except Exception:
            logging.exception("移除持久化任务失败：%s", download.filename)
        unregister_rename_target(download)
        untrack_unique(download.unique_id)
        running -= 1


def createProgress(client: Client):
    async def progress(received: int, total: int, download: Download) -> None:
        if download.stopped or (download.batch and download.batch.stopped) or download.id in stop:
            mark_download_stopped(download)
            client.stop_transmission()
            return

        # 进度用 create_task，避免 await 发消息拖慢下载
        now = time()
        if download.last_update != 0 and (now - download.last_update) < 1.5:
            return
        download.last_update = now
        expected = download.expected_size or 0
        if expected and (not total or total > expected * 2):
            total = expected
        if expected:
            download.size = expected
        elif total:
            download.size = total
        if total:
            received = min(received, total)
            percent = received / total * 100
            size_line = f"{humanReadableSize(received)}/{humanReadableSize(total)} {percent:0.2f}%"
        else:
            size_line = f"已下载 {humanReadableSize(received)}"
        elapsed = max(now - download.started, 1)
        avg_speed = received / elapsed
        if total and avg_speed > 0:
            tte = int((total - received) / avg_speed)
            speed_line = f"{humanReadableSize(avg_speed)}/s，预计还需 {humanReadableTime(tte)}"
        else:
            speed_line = f"{humanReadableSize(avg_speed)}/s"

        seq = download.ui_seq
        if download.batch:
            item = resolve_batch_item(download)
            if item is not None:
                item.status = "downloading"
                item.name = Path(download.filename).name
                item.received = received
                item.total = total
                item.started = download.started or item.started or now
            batch = download.batch

            async def _refresh():
                if download.stopped or download.ui_seq != seq:
                    return
                await refresh_batch(batch)

            asyncio.create_task(_refresh())
            return

        text = dedent(
            f"""
            `{download.filename}`：
            __{size_line}
            {speed_line}__
            """
        )
        markup = stop_keyboard(download.id)

        async def _edit_progress():
            if download.stopped or download.ui_seq != seq:
                return
            await safe_edit(
                download.progress_message,
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup,
            )

        asyncio.create_task(_edit_progress())

    return progress


async def continue_unique_download(download: Download) -> None:
    if download.unique_id:
        delete_by_unique_id(download.unique_id)
    download.skip_hash_check = True
    ensure_unique_download_name(download)
    if download.batch_item:
        download.batch_item.status = "waiting"
    queue_download(download)
    if download.batch:
        return
    size = download.expected_size or download.size
    size_text = f"（{humanReadableSize(size)}）" if size else ""
    await safe_edit(
        download.progress_message,
        f"文件 `{download.filename}` 已加入下载队列{size_text}。",
        parse_mode=ParseMode.MARKDOWN,
    )


async def skip_unique_download(download: Download) -> None:
    if download.batch_item:
        download.batch_item.status = "skipped"
    if download.batch is None:
        await safe_edit(
            download.progress_message,
            "已取消下载",
            parse_mode=ParseMode.MARKDOWN,
            important=True,
        )


async def handle_unique_decision(callback: CallbackQuery, continue_download: bool) -> None:
    token = (callback.data or "").split(" ", 1)[-1]
    download = pending_unique.pop(token, None)
    if download is None:
        await callback.answer("已处理")
        return
    if continue_download:
        await callback.answer("继续下载")
        await continue_unique_download(download)
        return
    await callback.answer("已取消")
    await skip_unique_download(download)


async def handle_batch_unique_decision(callback: CallbackQuery, continue_download: bool) -> None:
    batch_id = (callback.data or "").split(" ", 1)[-1]
    batch = active_batches.get(batch_id)
    if batch is None:
        await callback.answer("该批次已完成或不存在")
        return
    pending = list(batch.pending_unique)
    batch.pending_unique.clear()
    if not pending:
        await callback.answer("没有待处理的重复文件")
        await refresh_batch(batch, force=True)
        return
    if continue_download:
        await callback.answer("继续下载重复文件")
        for download in pending:
            await continue_unique_download(download)
    else:
        await callback.answer("已跳过重复文件")
        for download in pending:
            await skip_unique_download(download)
        await finish_batch_item(batch)
        return
    await refresh_batch(batch, force=True)


async def handle_hash_decision(callback: CallbackQuery, keep: bool) -> None:
    token = (callback.data or "").split(" ", 1)[-1]
    prompt = pending_hash.pop(token, None)
    if prompt is None:
        await callback.answer("已处理")
        return
    download = prompt.download
    item = resolve_batch_item(download)
    name = Path(download.filename).name
    if keep:
        await callback.answer("已保留")
        if item:
            item.status = "done"
            item.name = name
        text = prompt.success_text if download.batch is None else f"`{name}` 已保留"
    else:
        await callback.answer("已删除")
        try:
            Path(prompt.path).unlink(missing_ok=True)
        except OSError:
            logging.warning("删除重复文件失败：%s", prompt.path, exc_info=True)
        if item:
            item.status = "deleted"
            item.name = name
        text = "已删除重复文件" if download.batch is None else f"`{name}` 已删除"
    if download.batch:
        await refresh_batch(download.batch, force=True)
    await safe_edit(prompt.prompt_message, text, parse_mode=ParseMode.MARKDOWN, important=True)


async def stop_batch_now(target: Batch) -> None:
    """停止一个批次并清理其文件；只负责停止本身，不回复回调。"""
    target.stopped = True
    target.pending_unique.clear()
    for item in list(downloads):
        if item.batch and item.batch.id == target.id:
            mark_download_stopped(item)
    for item in list(rename_targets.values()):
        if item.batch and item.batch.id == target.id:
            mark_download_stopped(item)
    for batch_item in target.items:
        if batch_item.status in {"waiting", "downloading"}:
            batch_item.status = "stopped"

    if not await wait_for_batch_cleanup(target.id):
        logging.warning("批次 %s 等待超时，强制清理", target.id)

    deleted, remaining = cleanup_batch_files(target)
    logging.warning("批次 %s 清理完成：删除 %d 个文件，剩余 %d 个", target.id, deleted, remaining)
    if remaining and target.directory:
        schedule_cleanup_retry(target.directory, target.folder)
    for batch_item in target.items:
        batch_item.status = "deleted"
    await safe_edit(target.message, STOPPED_TEXT, parse_mode=ParseMode.MARKDOWN, important=True)
    if target.quiet and target.message is not None:
        asyncio.create_task(delete_message_later(target.message))
    if active_batches.pop(target.id, None) is not None:
        queue_persist.remove_batch(target.id)


async def handle_stop_batch(callback: CallbackQuery, batch_id: str) -> None:
    target = active_batches.get(batch_id)
    if target is None:
        await callback.answer("该批次已完成或不存在")
        return

    await callback.answer("正在停止...")
    await stop_batch_now(target)


async def handle_stop_single(callback: CallbackQuery, download_id: int) -> None:
    target = find_download_by_id(download_id)
    if target is None:
        await callback.answer("任务已结束")
        return
    if target.stopped:
        await callback.answer("已停止")
        return
    mark_download_stopped(target)
    await callback.answer("正在停止...")
    if target.batch is None and target.task is None:
        # 排队中还没开始传输：出队顺序可能排在几个大任务之后，立即收尾，别让消息干等
        await finalize_queued_stopped(target)


def path_in_use(rel_path: str) -> bool:
    """文件是否正被下载任务占用（任务的目标文件，或它的 .temp 临时文件）。"""
    base = Path(BASE_FOLDER)
    try:
        target = str((base / rel_path).resolve())
    except OSError:
        return False
    candidates = [download.filename for download in active_downloads]
    candidates += [download.filename for download in downloads]
    candidates += [prompt.path for prompt in pending_hash.values()]
    for name in candidates:
        try:
            if str((base / name).resolve()) == target:
                return True
            if str((base / f"{name}.temp").resolve()) == target:
                return True
        except OSError:
            continue
    return False


async def handle_callback(_, callback: CallbackQuery) -> None:
    data = callback.data or ""
    if data.startswith("qstopb ") or data.startswith("qstop ") or data.startswith("qgoto ") or data == "qref" or data == "qcancelall":
        # /queue 队列视图的回调
        from bot.download import queueview

        await queueview.handle_queue_callback(callback)
        return
    if data.startswith("stopb "):
        await handle_stop_batch(callback, data.split(" ", 1)[1])
        return
    if data.startswith("stop "):
        await handle_stop_single(callback, int(data.split()[-1]))
        return
    if data.startswith("dupc "):
        await handle_unique_decision(callback, True)
        return
    if data.startswith("dupx "):
        await handle_unique_decision(callback, False)
        return
    if data.startswith("bdupc "):
        await handle_batch_unique_decision(callback, True)
        return
    if data.startswith("bdupx "):
        await handle_batch_unique_decision(callback, False)
        return
    if data.startswith("hkeep "):
        await handle_hash_decision(callback, True)
        return
    if data.startswith("hdel "):
        await handle_hash_decision(callback, False)
        return
    if data.startswith("unlisten "):
        # 监听面板的「取消监听」按钮
        from bot import listener

        await listener.handle_unlisten_callback(callback)
        return
    if data.startswith("menu "):
        # /start 的按钮菜单
        from bot import commands

        await commands.handle_menu_callback(callback)
        return
    if data.startswith("f"):
        # /files 文件管理的回调统一由 filebrowser 处理
        from bot.filebrowser import handle_files_callback

        await handle_files_callback(callback)
        return
    await callback.answer()



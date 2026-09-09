from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from textwrap import dedent
from time import time

from pyrogram.client import Client
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.app import BASE_FOLDER, MAX_SIMULTANEOUS_TRANSMISSIONS
from bot.download.fileformat import align_filename_to_container
from bot.download.transfer import download_with_resume
from bot.download.types import Batch, BatchItem, Download
from bot.util import humanReadableSize, humanReadableTime


downloads: list[Download] = []
running = 0
stop: list[int] = []
active_batches: dict[str, Batch] = {}
# 进度消息 / 原消息 id -> 仍可改名的下载任务
rename_targets: dict[int, Download] = {}

BATCH_CLEANUP_TIMEOUT = 15.0
FILE_HANDLE_RELEASE_WAIT = 1.5
BATCH_CLEANUP_CHECK_INTERVAL = 0.3
STOPPED_TEXT = "已停止并删除"

STATUS_MARK = {
    "done": "✅",
    "waiting": "⏳",
    "downloading": "⬇️",
    "failed": "❌",
    "stopped": "⏹",
    "deleted": "🗑️",
}

ITEM_STATUS_LABEL = {
    "done": "",
    "waiting": "等待中",
    "stopped": "已停止",
    "deleted": "已删除",
    "failed": "失败",
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


async def run() -> None:
    global running
    while True:
        for download in list(downloads):
            if running == MAX_SIMULTANEOUS_TRANSMISSIONS:
                break
            if download not in downloads:
                continue
            downloads.remove(download)
            asyncio.create_task(downloadFile(download))
            logging.info("New download initialized: %s", download.filename)
            running += 1
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break


async def safe_edit(message, text: str, **kwargs) -> None:
    try:
        await message.edit(text=text, **kwargs)
    except Exception as exc:
        logging.debug("更新下载消息失败：%s: %s", type(exc).__name__, exc)


def batch_keyboard(batch: Batch) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("停止全部", callback_data=f"stopb {batch.id}")]]
    )


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
        return f"{mark} {name}  `{bar}` {percent:0.0f}% {size}"
    if item.received:
        return f"{mark} {name}  已下载 {humanReadableSize(item.received)}"
    return f"{mark} {name}  下载中"


def render_batch(batch: Batch) -> str:
    total = len(batch.items) or batch.total
    if batch.finished >= total:
        status = (
            f"全部完成 {batch.done}/{total}"
            if batch.failed == 0
            else f"完成 {batch.done}/{total}，未完成 {batch.failed}"
        )
    else:
        status = f"进度 {batch.done}/{total}"
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


def cleanup_batch_files(batch: Batch) -> tuple[int, int]:
    directory = Path(batch.directory) if batch.directory else None
    if not directory or not directory.is_dir():
        return 0, 0
    if directory.resolve() == Path(BASE_FOLDER).resolve():
        return 0, 0

    deleted = 0
    remaining = 0
    try:
        for file_path in directory.iterdir():
            if not file_path.is_file():
                continue
            try:
                file_path.unlink()
                deleted += 1
                logging.warning("已删除文件：%s", file_path.name)
            except OSError as e:
                remaining += 1
                logging.warning("无法删除文件 %s: %s", file_path.name, e)

        if remaining == 0:
            try:
                directory.rmdir()
                logging.warning("已删除分组文件夹：%s", batch.folder)
            except OSError as e:
                logging.warning("无法删除文件夹 %s: %s", batch.folder, e)
                remaining = len(list(directory.iterdir()))
        else:
            logging.warning("文件夹 %s 仍有 %d 个文件无法删除", batch.folder, remaining)
    except OSError as e:
        logging.error("清理文件夹失败 %s: %s", batch.folder, e)
    return deleted, remaining


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
    active_batches.pop(batch.id, None)


def cleanup_partial_download(save_path: str, filename: str) -> None:
    try:
        file_path = Path(save_path)
        if file_path.exists():
            file_path.unlink()
            logging.warning("已删除部分下载文件：%s", filename)
        temp_path = Path(save_path + ".temp")
        if temp_path.exists():
            temp_path.unlink()
    except OSError:
        logging.debug("无法删除文件：%s", save_path, exc_info=True)


def mark_download_stopped(download: Download) -> None:
    download.stopped = True
    download.ui_seq += 1
    if download.id not in stop:
        stop.append(download.id)


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
    await safe_edit(download.progress_message, STOPPED_TEXT, parse_mode=ParseMode.MARKDOWN)


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


async def finish_download_success(download: Download, item: BatchItem | None, result: str) -> None:
    result, download.filename = align_filename_to_container(result, download.filename)
    if download.pending_rename:
        result, download.filename = apply_pending_rename_on_disk(download, result)
    finished = time()
    seconds_took = max(finished - download.started, 1)
    actual_size = Path(result).stat().st_size if Path(result).exists() else (
        download.expected_size or download.size
    )
    speed = humanReadableSize(actual_size / seconds_took)
    time_took = humanReadableTime(int(seconds_took))

    if download.batch:
        if item:
            item.status = "done"
            item.name = Path(download.filename).name
            item.received = actual_size
            item.total = actual_size
        await finish_batch_item(download.batch)
    else:
        await safe_edit(
            download.progress_message,
            dedent(
                f"""
                文件 `{download.filename}` 已下载完成。
                共 {humanReadableSize(actual_size)}，用时 {time_took}，平均速度 __{speed}/s__
                """
            ),
            parse_mode=ParseMode.MARKDOWN,
        )


def apply_pending_rename_on_disk(download: Download, result_path: str) -> tuple[str, str]:
    from bot.download.names import (
        replace_filename,
        unique_filename,
        with_media_extension,
    )

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


async def handle_download_failure(download: Download, item: BatchItem | None) -> None:
    if item:
        item.status = "failed"
    if download.batch:
        await finish_batch_item(download.batch)
    else:
        await safe_edit(
            download.progress_message,
            f"文件 `{download.filename}` 下载失败。",
            parse_mode=ParseMode.MARKDOWN,
        )


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

    except Exception:
        logging.exception("下载失败：%s", download.filename)
        cleanup_partial_download(save_path, download.filename)
        await handle_download_failure(download, item)
    finally:
        unregister_rename_target(download)
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


async def stopDownload(_, callback: CallbackQuery) -> None:
    data = callback.data or ""

    if data.startswith("stopb "):
        batch_id = data.split(" ", 1)[1]
        target = active_batches.get(batch_id)
        if target is None:
            await callback.answer("该批次已完成或不存在")
            return

        target.stopped = True
        for item in list(downloads):
            if item.batch and item.batch.id == batch_id:
                mark_download_stopped(item)
        for item in list(rename_targets.values()):
            if item.batch and item.batch.id == batch_id:
                mark_download_stopped(item)
        for batch_item in target.items:
            if batch_item.status in {"waiting", "downloading"}:
                batch_item.status = "stopped"

        await callback.answer("正在停止...")
        if not await wait_for_batch_cleanup(batch_id):
            logging.warning("批次 %s 等待超时，强制清理", batch_id)

        deleted, remaining = cleanup_batch_files(target)
        logging.warning("批次 %s 清理完成：删除 %d 个文件，剩余 %d 个", batch_id, deleted, remaining)
        for batch_item in target.items:
            batch_item.status = "deleted"
        await safe_edit(target.message, STOPPED_TEXT, parse_mode=ParseMode.MARKDOWN)
        active_batches.pop(batch_id, None)
        return

    download_id = int(data.split()[-1])
    target = find_download_by_id(download_id)
    if target is None:
        await callback.answer("任务已结束")
        return
    if target.stopped:
        await callback.answer("已停止")
        return
    mark_download_stopped(target)
    await callback.answer("正在停止...")

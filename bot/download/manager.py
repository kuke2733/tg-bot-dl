from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from textwrap import dedent
from time import time

from pyrogram.client import Client
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery

from bot import callbacks
from bot.app import BASE_FOLDER, MAX_SIMULTANEOUS_TRANSMISSIONS
from bot.download import cleanup, persist as queue_persist, state
from bot.download.batches import finish_batch_item, refresh_batch, start_download_progress
from bot.download.dedup import prompt_hash_duplicate
from bot.download.fileformat import align_filename_to_container
from bot.download.lifecycle import (
    finalize_single_stopped,
    handle_disk_full,
    handle_download_exhausted,
    handle_download_failure,
    handle_stopped_download,
    requeue_paused_download,
)
from bot.download.names import replace_filename, unique_filename, with_media_extension
from bot.download.render import MIN_EDIT_INTERVAL, delete_message_later, safe_edit, stop_keyboard, success_text_for
from bot.download.state import (
    _event_info,
    active_downloads,
    downloads,
    emit_download_event,
    mark_download_stopped,
    resolve_batch_item,
    untrack_unique,
    unregister_rename_target,
    is_user_stopped,
    should_pause,
)
from bot.download.store import find_by_sha256, hash_file, init as init_store, remember
from bot.download.transfer import DownloadExhausted, PauseTransmission, download_with_resume, prepare_temp_file, temp_path_for
from bot.download.types import BatchItem, Download
from bot.util import humanReadableSize, humanReadableTime


running = 0


async def run() -> None:
    global running
    init_store()
    while True:
        if not state.paused:
            for download in list(downloads):
                if running == MAX_SIMULTANEOUS_TRANSMISSIONS:
                    break
                if download not in downloads:
                    continue
                downloads.remove(download)
                active_downloads.append(download)
                download.task = asyncio.create_task(downloadFile(download))
                logging.info("开始下载：%s", download.filename)
                running += 1
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break


async def finish_download_success(download: Download, item: BatchItem | None, result: str) -> None:
    result, download.filename = align_filename_to_container(result, download.filename)
    if download.pending_rename:
        result, download.filename = apply_pending_rename_on_disk(download, result)
    finished = time()
    seconds_took = max(finished - download.started, 1)
    actual_size = Path(result).stat().st_size if Path(result).exists() else (
        download.expected_size or download.size
    )
    session_bytes = max(actual_size - (download.resume_from or 0), 0)

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

    speed = humanReadableSize(session_bytes / seconds_took)
    time_took = humanReadableTime(int(seconds_took))
    success_text = success_text_for(download, actual_size, time_took, speed)

    # 大文件哈希可能较慢：先把进度钉到完成态，避免一直停在 99%
    if download.batch is None and download.progress_message is not None:
        await safe_edit(
            download.progress_message,
            dedent(
                f"""
                `{download.filename}`：
                __{humanReadableSize(actual_size)}/{humanReadableSize(actual_size)} 100.00%
                下载完成，正在校验...__
                """
            ),
            parse_mode=ParseMode.MARKDOWN,
            important=True,
        )
    elif download.batch and item is not None:
        item.status = "downloading"
        item.received = actual_size
        item.total = actual_size
        await refresh_batch(download.batch, force=True)

    sha256 = ""
    try:
        sha256 = await asyncio.to_thread(hash_file, result)
    except OSError:
        logging.exception("计算文件哈希失败：%s", result)

    if sha256 and not download.skip_hash_check and find_by_sha256(sha256):
        logging.info("下载后内容重复：%s sha256=%s", download.filename, sha256[:12])
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
    logging.info(
        "下载完成：%s，%s，用时 %s，平均 %s/s",
        download.filename,
        humanReadableSize(actual_size),
        time_took,
        speed,
    )

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


async def downloadFile(download: Download) -> None:
    global running
    item = resolve_batch_item(download)
    save_path = str(Path(BASE_FOLDER) / download.filename)

    try:
        if is_user_stopped(download):
            await handle_stopped_download(download, item, save_path)
            return

        if should_pause(download):
            await requeue_paused_download(download, item)
            return

        await start_download_progress(download, item)
        download.resume_from = prepare_temp_file(temp_path_for(save_path))
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
            pause_check=lambda: should_pause(download),
        )
        download.finalizing = True
        download.ui_seq += 1

        if not isinstance(result, str):
            user_stop = is_user_stopped(download)
            if not user_stop and should_pause(download):
                download.finalizing = False
                await requeue_paused_download(download, item)
                return
            if item:
                item.status = "stopped" if user_stop else "failed"
            if download.batch:
                await finish_batch_item(download.batch)
            elif user_stop:
                await finalize_single_stopped(download, save_path)
            else:
                await handle_download_failure(download, item)
            return

        await finish_download_success(download, item, result)

    except DownloadExhausted as exc:
        # 短周期重试耗尽：保留断点转入长周期自动重试；磁盘满走单独通道
        if exc.disk_full:
            await handle_disk_full(download, item)
        else:
            await handle_download_exhausted(download, item, exc)
    except asyncio.CancelledError:
        # 用户停止：按已停止收尾；进程重启等取消：保留 queue.json 与断点
        user_stop = is_user_stopped(download)
        if user_stop:
            logging.warning("下载 %s 被强制中断，按已停止处理", download.filename)
            if download.batch:
                await handle_stopped_download(download, item, save_path)
            else:
                await finalize_single_stopped(download, save_path)
        else:
            download.will_requeue = True
            logging.warning("下载 %s 被取消但非用户停止，保留队列记录与断点", download.filename)
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
        if not download.will_requeue:
            # 驻留等待自动重试/磁盘恢复期间保留记录，重启后照常恢复
            try:
                queue_persist.remove_task(queue_persist.task_record(download)["key"])
            except Exception:
                logging.exception("移除持久化任务失败：%s", download.filename)
            unregister_rename_target(download)
            untrack_unique(download.unique_id)
        running -= 1


def createProgress(client: Client):
    async def progress(received: int, total: int, download: Download) -> None:
        if download.finalizing or is_user_stopped(download):
            if not download.finalizing:
                mark_download_stopped(download)
                client.stop_transmission()
            return

        if should_pause(download):
            download.pausing = True
            download.speed = 0.0
            download.eta = None
            raise PauseTransmission

        # 进度用 create_task，避免 await 发消息拖慢下载
        now = time()
        if download.last_update != 0 and (now - download.last_update) < MIN_EDIT_INTERVAL:
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
        session_bytes = max(received - (download.resume_from or 0), 0)
        elapsed = max(now - download.started, 1)
        avg_speed = session_bytes / elapsed
        download.received = received
        download.speed = avg_speed
        if total and received < total and avg_speed > 0:
            tte = int((total - received) / avg_speed)
            download.eta = tte
            speed_line = f"{humanReadableSize(avg_speed)}/s，预计还需 {humanReadableTime(tte)}"
        elif total and received >= total:
            download.eta = 0
            speed_line = f"{humanReadableSize(avg_speed)}/s，即将完成"
        else:
            download.eta = None
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
                item.resume_from = download.resume_from
            batch = download.batch

            async def _refresh():
                if download.finalizing or download.stopped or download.ui_seq != seq:
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
            if download.finalizing or download.stopped or download.ui_seq != seq:
                return
            if download.progress_message is None:
                return
            await safe_edit(
                download.progress_message,
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup,
            )

        asyncio.create_task(_edit_progress())

    return progress


# —— 按钮回调注册，协议前缀与路由见 bot/callbacks.py ——


async def _handle_stop_batch_cb(callback: CallbackQuery) -> None:
    await handle_stop_batch(callback, (callback.data or "").split(" ", 1)[1])


async def _handle_stop_single_cb(callback: CallbackQuery) -> None:
    await handle_stop_single(callback, int((callback.data or "").split()[-1]))


callbacks.on(callbacks.STOP_BATCH)(_handle_stop_batch_cb)
callbacks.on(callbacks.STOP)(_handle_stop_single_cb)



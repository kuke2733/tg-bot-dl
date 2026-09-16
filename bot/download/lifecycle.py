"""任务终结与驻留生命周期：停止、失败、长周期重排、冷驻留、磁盘满。

这里决定一个任务何时算「真正结束」：只有用户手动停止或源消息消失；
其余一切自动中断都保留断点与 queue.json 记录并按原因驻留。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery

from bot.app import BASE_FOLDER
from bot.download import cleanup, persist as queue_persist, state
from bot.download import transfer
from bot.download.batches import finish_batch_item, refresh_batch, wait_for_batch_cleanup
from bot.download.render import delete_message_later, safe_edit, stop_keyboard
from bot.download.state import (
    active_batches,
    active_downloads,
    downloads,
    emit_download_event,
    _event_info,
    find_download_by_id,
    held_in_batch,
    hold_download,
    holds,
    mark_download_stopped,
    pop_stop,
    rename_targets,
    resolve_batch_item,
    unhold,
    untrack_unique,
    unregister_rename_target,
    queue_download,
    insert_paused_download,
    is_user_stopped,
)
from bot.download.transfer import DownloadExhausted, prepare_temp_file, temp_path_for
from bot.download.types import Batch, Download, HoldReason
from bot.util import humanReadableSize, humanReadableTime

# 短周期重试耗尽后的长周期自动重试：间隔递增，最多 len(LONG_RETRY_DELAYS) 轮
LONG_RETRY_DELAYS = (5 * 60, 15 * 60, 30 * 60, 60 * 60)
STOPPED_TEXT = "已停止并删除"


def set_paused(value: bool) -> int:
    state.paused = value
    if value:
        targets = [download for download in list(active_downloads) if not is_user_stopped(download)]
        if not state.pause_resume_ids:
            state.pause_resume_ids = [download.id for download in targets]
        for download in targets:
            download.pausing = True
            download.reset_speed()
        return 0
    state.pause_resume_ids = []
    for download in list(active_downloads) + list(downloads):
        download.pausing = False
    return release_disk_holds()


def release_disk_holds() -> int:
    """磁盘满驻留的任务随 /resume 重新排队，从断点继续；返回释放的数量。"""
    from bot.download import state

    targets = [d for d, why in state.holds.values() if why == HoldReason.DISK]
    if not targets:
        return 0
    count = 0
    for download in targets:
        unhold(download)
        if is_user_stopped(download):
            asyncio.create_task(abort_held_download(download))
            continue
        requeue_held(download)
        count += 1
        logging.info("磁盘空间恢复，重新排队：%s", download.filename)
    if count:
        logging.info("磁盘空间恢复，%d 个驻留任务重新排队", count)
    return count


def requeue_held(download: Download) -> None:
    """把驻留任务重新入队，断点还在磁盘上，run 循环 1 秒内接手。"""
    unhold(download)
    download.will_requeue = False
    download.started = 0.0
    download.last_update = 0.0
    download.reset_speed()
    download.ui_seq += 1
    queue_download(download)


async def requeue_paused_download(download: Download, item) -> None:
    """暂停时把进行中的任务退回队列，保留 .temp 断点。"""
    download.will_requeue = True
    download.pausing = False
    download.task = None
    download.started = 0.0
    download.last_update = 0.0
    download.reset_speed()
    download.finalizing = False
    download.ui_seq += 1
    save_path = str(Path(BASE_FOLDER) / download.filename)
    download.resume_from = prepare_temp_file(temp_path_for(save_path))
    if download.received < download.resume_from:
        download.received = download.resume_from
    if item is not None:
        item.status = "waiting"
        item.received = download.received
        item.resume_from = download.resume_from
        item.speed = 0.0
        item.eta = None
    insert_paused_download(download)
    logging.info(
        "下载已暂停并保留断点：%s（%s）",
        download.filename,
        humanReadableSize(download.resume_from) if download.resume_from else "0",
    )
    if download.batch is not None:
        await refresh_batch(download.batch, force=True)
        return
    if download.progress_message is None:
        return
    if download.expected_size or download.size:
        total = download.expected_size or download.size
        received = min(download.received or download.resume_from or 0, total)
        body = f"{humanReadableSize(received)}/{humanReadableSize(total)} 已暂停"
    elif download.received or download.resume_from:
        body = f"已暂停，已下载 {humanReadableSize(download.received or download.resume_from)}"
    else:
        body = "已暂停"
    await safe_edit(
        download.progress_message,
        f"`{download.filename}`：\n__{body}__",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=stop_keyboard(download.id),
        important=True,
    )


async def finalize_single_stopped(download: Download, save_path: str) -> None:
    mark_download_stopped(download)
    pop_stop(download.id)
    cleanup.cleanup_partial_download(save_path, download.filename)
    await safe_edit(download.progress_message, STOPPED_TEXT, parse_mode=ParseMode.MARKDOWN, important=True)
    if download.quiet:
        asyncio.create_task(delete_message_later(download.progress_message))
    emit_download_event("stopped", _event_info(download))


def detach_for_cancel(download: Download) -> None:
    """从排队/驻留里立刻摘掉并删持久化，不碰进度消息（留给后台慢慢改）。"""
    mark_download_stopped(download)
    try:
        downloads.remove(download)
    except ValueError:
        pass
    if download.retry_task is not None and not download.retry_task.done():
        download.retry_task.cancel()
    unhold(download)
    download.hold_aborted = True
    pop_stop(download.id)
    try:
        queue_persist.remove_task(queue_persist.task_record(download)["key"])
    except Exception:
        logging.exception("移除持久化任务失败：%s", download.filename)
    unregister_rename_target(download)
    untrack_unique(download.unique_id)


async def finalize_detached_stopped(download: Download) -> None:
    """后台收尾：清断点、改进度文案。调用前须已 detach_for_cancel。"""
    save_path = str(Path(BASE_FOLDER) / download.filename)
    cleanup.cleanup_partial_download(save_path, download.filename)
    await safe_edit(download.progress_message, STOPPED_TEXT, parse_mode=ParseMode.MARKDOWN, important=True)
    if download.quiet:
        asyncio.create_task(delete_message_later(download.progress_message))
    emit_download_event("stopped", _event_info(download))


async def finalize_queued_stopped(download: Download) -> None:
    """取消还没开始传输的排队任务：立即收尾，不等它出队。"""
    detach_for_cancel(download)
    await finalize_detached_stopped(download)


async def handle_stopped_download(download: Download, item, save_path: str) -> None:
    if item:
        item.status = "deleted"
    if download.batch:
        pop_stop(download.id)
        cleanup.cleanup_partial_download(save_path, download.filename)
        await finish_batch_item(download.batch)
        return
    await finalize_single_stopped(download, save_path)


async def handle_download_failure(download: Download, item, note: str = "") -> None:
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


async def abort_held_download(download: Download) -> None:
    """驻留任务按已停止收尾：清断点、删记录、消息交代。

    停止按钮、批次停止、等待唤醒都可能触发收尾；hold_aborted 保证只收一次。
    """
    if download.hold_aborted:
        return
    download.hold_aborted = True
    unhold(download)
    pop_stop(download.id)
    save_path = str(Path(BASE_FOLDER) / download.filename)
    try:
        queue_persist.remove_task(queue_persist.task_record(download)["key"])
    except Exception:
        logging.exception("移除持久化任务失败：%s", download.filename)
    if download.batch:
        cleanup.cleanup_partial_download(save_path, download.filename)
        return
    await finalize_single_stopped(download, save_path)


async def stop_held_download(download: Download) -> None:
    """停止驻留中的任务：终止定时等待并按已停止收尾。

    收尾由这里负责，不依赖被取消协程的 except 分支——任务尚未启动就被
    cancel() 时协程体根本不会执行。
    """
    mark_download_stopped(download)
    if download.retry_task is not None and not download.retry_task.done():
        download.retry_task.cancel()
    await abort_held_download(download)


async def handle_download_exhausted(download: Download, item, exc: DownloadExhausted) -> None:
    """短周期重试耗尽：保留 .temp 断点，安排下一轮长周期自动重试；轮次用尽转冷驻留。"""
    save_path = str(Path(BASE_FOLDER) / download.filename)
    if is_user_stopped(download):
        await handle_stopped_download(download, item, save_path)
        return
    if download.retry_round >= len(LONG_RETRY_DELAYS):
        # 轮次用尽不判死：转入冷驻留。断点与记录保留，连接恢复后由会话监控唤醒重试
        download.will_requeue = True
        hold_download(download, HoldReason.COLD)
        if item:
            item.status = "cold"
            item.started = 0.0
        logging.warning(
            "下载 %s 自动重试 %d 轮未成功，转入冷驻留，断点保留，连接恢复后自动继续",
            download.filename,
            download.retry_round,
        )
        if download.batch:
            await refresh_batch(download.batch, force=True)
        else:
            await safe_edit(
                download.progress_message,
                f"`{download.filename}`：\n__❄️ {len(LONG_RETRY_DELAYS)} 次自动重试未成功，"
                "已保留断点、暂停重试；连接恢复后会自动继续。__",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=stop_keyboard(download.id),
                important=True,
            )
        return
    download.will_requeue = True
    download.retry_round += 1
    delay = max(LONG_RETRY_DELAYS[download.retry_round - 1], int(exc.wait or 0))
    resumed = prepare_temp_file(temp_path_for(save_path))
    logging.warning(
        "下载 %s 短周期重试耗尽（%s，已下载 %s），%s 后进行第 %d/%d 次自动重试",
        download.filename,
        type(exc).__name__,
        humanReadableSize(resumed),
        humanReadableTime(int(delay)),
        download.retry_round,
        len(LONG_RETRY_DELAYS),
    )
    if item:
        item.status = "waiting"
        item.started = 0.0
    if download.batch:
        await refresh_batch(download.batch, force=True)
    else:
        await safe_edit(
            download.progress_message,
            f"`{download.filename}`：\n__⏳ 第 {download.retry_round}/{len(LONG_RETRY_DELAYS)} 次自动重试将在 "
            f"{humanReadableTime(int(delay))} 后开始，已下载 {humanReadableSize(resumed)}，断点已保留。__",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=stop_keyboard(download.id),
            important=True,
        )
    hold_download(download, HoldReason.RETRY)
    download.retry_task = asyncio.create_task(_long_retry_wait(download, delay))


async def refetch_source_message(download: Download):
    """重取源消息拿新引用。

    网络等原因取不到时沿用内存里的原引用，也就是返回原消息；
    调用成功但消息为空/无媒体，说明源消息已被删除，返回 None。
    """
    chat = download.from_message.chat if download.from_message is not None else None
    if chat is None:
        return download.from_message
    try:
        fetched = await download.client.get_messages(chat.id, [download.id])
    except Exception:
        logging.warning("刷新源消息失败，沿用原引用：%s", download.filename, exc_info=True)
        return download.from_message
    refreshed = fetched[0] if fetched else None
    if refreshed is not None and not getattr(refreshed, "empty", False) and refreshed.media:
        return refreshed
    return None


def _source_gone(source) -> bool:
    return source is None or getattr(source, "empty", False) or not source.media


async def fail_source_deleted(download: Download) -> None:
    """源消息已被删除，按失败收尾：清断点、删记录、消息交代。"""
    logging.warning("源消息不存在或已删除：%s", download.filename)
    unhold(download)
    download.will_requeue = False
    try:
        queue_persist.remove_task(queue_persist.task_record(download)["key"])
    except Exception:
        logging.exception("移除持久化任务失败：%s", download.filename)
    cleanup.cleanup_partial_download(str(Path(BASE_FOLDER) / download.filename), download.filename)
    await handle_download_failure(download, resolve_batch_item(download), note="源消息已删除")


async def _long_retry_wait(download: Download, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        # 等待期间被用户停止：stop_held_download 发起的取消由这里收尾
        await abort_held_download(download)
        return
    unhold(download)
    download.retry_task = None
    if is_user_stopped(download):
        await abort_held_download(download)
        return
    source = await refetch_source_message(download)
    if _source_gone(source):
        await fail_source_deleted(download)
        return
    download.from_message = source
    requeue_held(download)
    logging.info("下载 %s 进入第 %d 次自动重试", download.filename, download.retry_round)


async def wake_cold_holds() -> int:
    """连接恢复，也就是会话健康检查通过后，唤醒冷驻留任务：确认源消息还在，再重新排队续传。

    返回唤醒数量；唤醒后的任务若再一次短周期耗尽，会直接回到冷驻留等下次唤醒。
    """
    targets = [(d, why) for d, why in holds.values() if why == HoldReason.COLD]
    if not targets:
        return 0
    woke = 0
    for download, _why in targets:
        if is_user_stopped(download):
            await abort_held_download(download)
            continue
        source = await refetch_source_message(download)
        if _source_gone(source):
            await fail_source_deleted(download)
            continue
        download.from_message = source
        requeue_held(download)
        woke += 1
    if woke:
        logging.info("连接恢复，唤醒 %d 个冷驻留任务继续下载", woke)
    return woke


async def handle_disk_full(download: Download, item) -> None:
    """磁盘写满：保留断点、暂停队列并通知管理员，等 /resume 恢复。"""
    save_path = str(Path(BASE_FOLDER) / download.filename)
    if is_user_stopped(download):
        await handle_stopped_download(download, item, save_path)
        return
    download.will_requeue = True
    hold_download(download, HoldReason.DISK)
    set_paused(True)
    resumed = prepare_temp_file(temp_path_for(save_path))
    logging.warning(
        "磁盘已满：%s 已下载 %s，保留断点并暂停队列，等 /resume 恢复",
        download.filename,
        humanReadableSize(resumed),
    )
    if item:
        item.status = "waiting"
        item.started = 0.0
    if download.batch:
        await refresh_batch(download.batch, force=True)
    else:
        await safe_edit(
            download.progress_message,
            "__💾 磁盘已满，断点已保留、队列已暂停；清理空间后发 /resume 继续。__",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=stop_keyboard(download.id),
            important=True,
        )
    try:
        # 局部导入避免 notify→listener→manager 的模块环
        from bot import notify

        await notify.notify_disk_full(download.filename)
    except Exception:
        logging.warning("磁盘满通知发送失败", exc_info=True)


async def stop_batch_now(target: Batch) -> None:
    """停止一个批次并清理其文件；只负责停止本身，不回复回调。"""
    target.stopped = True
    target.pending_unique.clear()
    seen: set[int] = set()
    for item in list(downloads) + list(active_downloads) + list(rename_targets.values()):
        if item.batch and item.batch.id == target.id and item.id not in seen:
            seen.add(item.id)
            mark_download_stopped(item)
    for waiter in held_in_batch(target.id):
        # 驻留中的批次成员也要一并停止并清除记录
        await stop_held_download(waiter)
    for batch_item in target.items:
        if batch_item.status in {"waiting", "downloading"}:
            batch_item.status = "stopped"
            batch_item.speed = 0.0
            batch_item.eta = None
    await refresh_batch(target, force=True)

    if not await wait_for_batch_cleanup(target.id):
        logging.warning("批次 %s 等待超时，强制清理", target.id)

    deleted, remaining = cleanup.cleanup_batch_files(target)
    logging.info("批次 %s 清理完成：删除 %d 个文件，剩余 %d 个", target.id, deleted, remaining)
    if remaining and target.directory:
        cleanup.schedule_cleanup_retry(target.directory, target.folder)
    for batch_item in target.items:
        batch_item.status = "deleted"
    await safe_edit(target.message, STOPPED_TEXT, parse_mode=ParseMode.MARKDOWN, important=True)
    if target.quiet and target.message is not None:
        asyncio.create_task(delete_message_later(target.message))
    if active_batches.pop(target.id, None) is not None:
        queue_persist.remove_batch(target.id)


async def _stop_batch_bg(target: Batch) -> None:
    try:
        await stop_batch_now(target)
    except Exception:
        logging.exception("停止批次失败：%s", target.id)


def schedule_stop_batch(target: Batch) -> None:
    target.stopped = True
    asyncio.create_task(_stop_batch_bg(target))


async def handle_stop_batch(callback: CallbackQuery, batch_id: str) -> None:
    target = active_batches.get(batch_id)
    if target is None or target.stopped:
        await callback.answer("该批次已完成或不存在")
        return

    await callback.answer("正在停止...")
    schedule_stop_batch(target)


async def handle_stop_single(callback: CallbackQuery, download_id: int) -> None:
    target = find_download_by_id(download_id)
    if target is None:
        await callback.answer("任务已结束")
        return
    if target.stopped:
        await callback.answer("已停止")
        return
    await callback.answer("正在停止...")
    if target.id in holds:
        # 驻留中：终止等待并按停止收尾
        await stop_held_download(target)
        return
    mark_download_stopped(target)
    if target.batch is None and target.task is None:
        # 排队中还没开始传输：出队顺序可能排在几个大任务之后，立即收尾，别让消息干等
        await finalize_queued_stopped(target)

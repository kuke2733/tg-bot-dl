"""网页面板用的队列快照与控制操作（无 Telegram callback）。"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from bot.download import lifecycle, state
from bot.download.lifecycle import (
    detach_for_cancel,
    finalize_detached_stopped,
    finalize_queued_stopped,
    stop_batch_now,
    stop_held_download,
)
from bot.download.queueview import QUEUE_MAX_ROWS
from bot.download.state import (
    active_batches,
    active_downloads,
    downloads,
    find_download_by_id,
    holds,
    iter_holds,
    mark_download_stopped,
    stop,
)
from bot.download.types import Batch, Download


def _file_item(download: Download, phase: str, hold_reason: str | None = None) -> dict:
    total = int(download.expected_size or download.size or 0)
    received = int(download.received or download.resume_from or 0)
    if total:
        received = min(received, total)
    speed = float(download.speed or 0.0)
    eta = download.eta
    if eta is None and total and received < total and speed > 0:
        eta = int((total - received) / speed)
    return {
        "id": download.id,
        "kind": "hold" if hold_reason else "file",
        "phase": phase,
        "name": Path(download.filename).name,
        "received": received,
        "total": total,
        "speed": speed,
        "eta": eta,
        "hold_reason": hold_reason,
        "batch_id": None,
        "done": None,
        "count": None,
    }


def _batch_item(batch: Batch) -> dict:
    count = len(batch.items) or batch.total
    downloading = [item for item in batch.items if item.status == "downloading"]
    received = sum(item.received for item in downloading) if downloading else 0
    total = sum(item.total for item in downloading) if downloading else 0
    speed = 0.0
    eta = None
    phase = "downloading" if downloading else "queued"
    # 批次速度：用正在下的条目估算
    now = time.time()
    for item in downloading:
        if item.started and item.total:
            session = max(item.received - (item.resume_from or 0), 0)
            elapsed = max(now - item.started, 1)
            speed += session / elapsed
    if total and received < total and speed > 0:
        eta = int((total - received) / speed)
    return {
        "id": batch.id,
        "kind": "batch",
        "phase": phase,
        "name": batch.folder,
        "received": received,
        "total": total,
        "speed": speed,
        "eta": eta,
        "hold_reason": None,
        "batch_id": batch.id,
        "done": batch.done,
        "count": count,
    }


def snapshot() -> dict:
    items: list[dict] = []

    for download in active_downloads:
        if download.batch is not None or download.stopped or download.id in stop:
            continue
        items.append(_file_item(download, "downloading"))

    for batch in active_batches.values():
        if batch.stopped:
            continue
        items.append(_batch_item(batch))

    for download in downloads:
        if download.batch is not None or download.stopped or download.id in stop:
            continue
        items.append(_file_item(download, "queued"))

    for download, reason in iter_holds():
        if download.batch is not None or download.stopped or download.id in stop:
            continue
        items.append(_file_item(download, "hold", hold_reason=str(reason)))

    truncated = max(0, len(items) - QUEUE_MAX_ROWS)
    return {
        "ok": True,
        "available": True,
        "paused": bool(state.paused),
        "updated_at": time.time(),
        "total": len(items),
        "truncated": truncated,
        "items": items[:QUEUE_MAX_ROWS],
    }


async def stop_download(download_id: int) -> dict:
    target = find_download_by_id(int(download_id))
    if target is None:
        return {"ok": False, "error": "not_found", **snapshot()}
    if target.stopped:
        return {"ok": True, "message": "already_stopped", **snapshot()}
    if target.id in holds:
        await stop_held_download(target)
    else:
        mark_download_stopped(target)
        if target.batch is None and target.task is None:
            await finalize_queued_stopped(target)
    return {"ok": True, **snapshot()}


async def stop_batch(batch_id: str) -> dict:
    target = active_batches.get(str(batch_id))
    if target is None or target.stopped:
        return {"ok": False, "error": "not_found", **snapshot()}
    target.stopped = True
    asyncio.create_task(_stop_batch_bg(target))
    return {"ok": True, **snapshot()}


async def _stop_batch_bg(target: Batch) -> None:
    try:
        await stop_batch_now(target)
    except Exception:
        logging.exception("面板停止批次失败：%s", target.id)


async def cancel_all() -> dict:
    pending_finalize: list[Download] = []

    for batch in list(active_batches.values()):
        batch.stopped = True
        asyncio.create_task(_stop_batch_bg(batch))

    for download in list(downloads) + list(active_downloads):
        if download.batch is not None:
            continue
        if download.task is None:
            detach_for_cancel(download)
            pending_finalize.append(download)
        else:
            mark_download_stopped(download)

    for download, _why in list(iter_holds()):
        if download.batch is not None:
            continue
        detach_for_cancel(download)
        pending_finalize.append(download)

    if pending_finalize:
        asyncio.create_task(_finalize_cancel_all_bg(pending_finalize))
    return {"ok": True, **snapshot()}


async def _finalize_cancel_all_bg(items: list[Download]) -> None:
    for download in items:
        try:
            await finalize_detached_stopped(download)
        except Exception:
            logging.exception("面板全部取消收尾失败：%s", getattr(download, "filename", "?"))


def set_paused(value: bool) -> dict:
    lifecycle.set_paused(bool(value))
    return {"ok": True, **snapshot()}

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
    schedule_stop_batch,
    stop_held_download,
)
from bot.download.speed import eta_seconds
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
    if phase == "paused":
        speed = 0.0
        eta = None
    elif phase == "downloading":
        speed = download.meter.speed_at(time.time())
        eta = eta_seconds(received, total, speed)
    elif eta is None:
        eta = eta_seconds(received, total, speed)
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
    paused = bool(state.paused)
    progress_items = downloading
    if paused and not progress_items:
        progress_items = [item for item in batch.items if item.received or item.total]
    received = sum(item.received for item in progress_items) if progress_items else 0
    total = sum(item.total for item in progress_items) if progress_items else 0
    speed = 0.0
    eta = None
    if paused:
        phase = "paused"
    elif downloading:
        phase = "downloading"
        now = time.time()
        for item in downloading:
            live = find_download_by_id(item.download_id)
            if live is not None:
                speed += live.meter.speed_at(now)
            else:
                speed += float(item.speed or 0.0)
        eta = eta_seconds(received, total, speed)
    else:
        phase = "queued"
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


def _visible_file(download: Download) -> bool:
    return download.batch is None and not download.stopped and download.id not in stop


def _append_batches(items: list[dict]) -> None:
    for batch in active_batches.values():
        if not batch.stopped:
            items.append(_batch_item(batch))


def _paused_files() -> list[Download]:
    by_id: dict[int, Download] = {}
    for download in list(active_downloads) + list(downloads):
        if _visible_file(download):
            by_id.setdefault(download.id, download)
    ordered: list[Download] = []
    for download_id in state.pause_resume_ids:
        download = by_id.pop(download_id, None)
        if download is not None:
            ordered.append(download)
    for download in downloads:
        leftover = by_id.pop(download.id, None)
        if leftover is not None:
            ordered.append(leftover)
    ordered.extend(by_id.values())
    return ordered


def snapshot() -> dict:
    items: list[dict] = []
    seen_ids: set[int] = set()
    queue_paused = bool(state.paused)

    def add_file(download: Download, phase: str) -> None:
        if not _visible_file(download) or download.id in seen_ids:
            return
        seen_ids.add(download.id)
        items.append(_file_item(download, phase))

    if queue_paused:
        _append_batches(items)
        for download in _paused_files():
            add_file(download, "paused")
    else:
        for download in active_downloads:
            add_file(download, "paused" if download.pausing else "downloading")
        _append_batches(items)
        for download in downloads:
            add_file(download, "queued")

    for download, reason in iter_holds():
        if download.id in seen_ids or not _visible_file(download):
            continue
        seen_ids.add(download.id)
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
    schedule_stop_batch(target)
    return {"ok": True, **snapshot()}


async def cancel_all() -> dict:
    pending_finalize: list[Download] = []

    for batch in list(active_batches.values()):
        schedule_stop_batch(batch)

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

"""下载子系统的共享可变注册表与轻量操作。

所有跨模块共享的内存状态集中在这里：排队/在途任务、批次、改名目标、
驻留表、停止标记、查重在飞集合、事件回调列表。只放「数据 + 一两行的
读写操作」，不含调度与消息编排（那些在 manager/batches/lifecycle）。

约定：
- 容器（列表/字典）在原对象上原地修改，from-import 共享安全；
- 标量（paused/running 等）一律通过 `state.属性` 访问，禁止 from-import。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from bot.app import BASE_FOLDER
from bot.download import persist as queue_persist
from bot.download import store
from bot.download.types import Batch, BatchItem, Download, HoldReason

# ---- 排队与在途 ----
downloads: list[Download] = []
active_downloads: list[Download] = []
stop: list[int] = []
# /pause 暂停出队（进行中的继续），/resume 恢复
paused = False

# ---- 批次 ----
active_batches: dict[str, Batch] = {}

# ---- 改名目标：进度消息 / 原消息 id -> 仍可改名的下载任务 ----
rename_targets: dict[int, Download] = {}

# ---- 驻留任务表：key=任务 id，value=(任务, 原因)。原因见 HoldReason；----
# 驻留 = 暂不参与调度，但断点与 queue.json 记录保留，进程重启照常恢复
holds: dict[int, tuple[Download, HoldReason]] = {}

# ---- 查重 ----
in_flight_unique_ids: set[str] = set()
pending_unique: dict[str, Download] = {}
pending_hash: dict[str, HashPrompt] = {}


@dataclass
class HashPrompt:
    download: Download
    path: str
    sha256: str
    success_text: str
    prompt_message: object


# ---- 下载事件回调（如频道监听的摘要），签名 (kind, info) ----
download_event_listeners: list = []

# 停止后等待传输协程自行退出的时间；断线重试等场景卡住不动就强制取消
STOP_CANCEL_GRACE = 10.0


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


def is_unique_duplicate(unique_id: str) -> bool:
    if not unique_id:
        return False
    if unique_id in in_flight_unique_ids:
        return True
    return store.find_by_unique_id(unique_id) is not None


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


def resolve_batch_item(download: Download) -> BatchItem | None:
    if download.batch_item is not None:
        return download.batch_item
    if download.batch is not None:
        return download.batch.item_for(download.id)
    return None


def find_download_by_id(download_id: int) -> Download | None:
    for item in list(rename_targets.values()):
        if item.id == download_id:
            return item
    for item in downloads:
        if item.id == download_id:
            return item
    entry = holds.get(download_id)
    if entry is not None:
        return entry[0]
    return None


def pop_stop(download_id: int) -> None:
    try:
        stop.remove(download_id)
    except ValueError:
        pass


def hold_download(download: Download, reason: HoldReason) -> None:
    """任务进入驻留：暂不参与调度，断点与 queue.json 记录保留。"""
    holds[download.id] = (download, reason)


def unhold(download: Download) -> None:
    holds.pop(download.id, None)


def held_reason(download: Download) -> HoldReason | None:
    entry = holds.get(download.id)
    return entry[1] if entry else None


def iter_holds(reason: HoldReason | None = None):
    """按原因过滤驻留中的任务；reason=None 返回全部。"""
    for download, why in list(holds.values()):
        if reason is None or why == reason:
            yield download, why


def held_in_batch(batch_id: str) -> list[Download]:
    return [d for d, _ in iter_holds() if d.batch and d.batch.id == batch_id]


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

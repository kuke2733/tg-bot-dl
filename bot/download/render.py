"""下载进度的纯文本渲染与消息编辑辅助，manager 与 queueview 共用。

只依赖 types 与 util，不感知调度逻辑；改文案、改进度条只动这里。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from textwrap import dedent
from time import time
from typing import Any, NamedTuple

from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.download.types import STATUS_LABEL, STATUS_MARK, Batch, BatchItem
from bot.util import humanReadableSize, humanReadableTime

# 同 chat 串行轮流 edit；FloodWait 后暂停该会话进度刷新
edit_cooldowns: dict[int, float] = {}
MIN_EDIT_INTERVAL = 1.5
MAX_IMPORTANT_COOL_WAIT = 90.0


class _Pending(NamedTuple):
    message: Any
    text: str
    kwargs: dict
    important: bool
    future: asyncio.Future | None


class _EditQueue:
    __slots__ = ("pending", "order", "worker", "last_at", "lock")

    def __init__(self) -> None:
        self.pending: dict[int, _Pending] = {}
        self.order: deque[int] = deque()
        self.worker: asyncio.Task | None = None
        self.last_at = 0.0
        self.lock = asyncio.Lock()


_queues: dict[int, _EditQueue] = {}


def _queue(chat_id: int) -> _EditQueue:
    q = _queues.get(chat_id)
    if q is None:
        q = _EditQueue()
        _queues[chat_id] = q
    return q


def _finish(fut: asyncio.Future | None, ok: bool) -> None:
    if fut is not None and not fut.done():
        fut.set_result(ok)


def _drop_all(q: _EditQueue) -> None:
    for item in q.pending.values():
        _finish(item.future, False)
    q.pending.clear()
    q.order.clear()
    q.worker = None


def _take_next(q: _EditQueue) -> _Pending | None:
    for msg_id, item in list(q.pending.items()):
        if item.important:
            q.pending.pop(msg_id, None)
            try:
                q.order.remove(msg_id)
            except ValueError:
                pass
            return item
    while q.order:
        item = q.pending.pop(q.order.popleft(), None)
        if item is not None:
            return item
    return None


async def _do_edit(
    message, text: str, *, chat_id: int | None, q: _EditQueue | None = None, **kwargs
) -> bool:
    try:
        await message.edit(text=text, **kwargs)
        if chat_id is not None and q is not None:
            q.last_at = time()
            edit_cooldowns.pop(chat_id, None)
        return True
    except FloodWait as exc:
        wait = int(getattr(exc, "value", 0) or 0)
        if chat_id is not None:
            edit_cooldowns[chat_id] = time() + wait + 1
        logging.warning("编辑消息触发限流 %s 秒，期间暂停该会话的进度刷新", wait)
        return False
    except Exception as exc:
        logging.debug("更新下载消息失败：%s: %s", type(exc).__name__, exc)
        return False


async def _pump(chat_id: int) -> None:
    q = _queue(chat_id)
    try:
        while True:
            async with q.lock:
                item = _take_next(q)
                if item is None:
                    q.worker = None
                    return

            cool = edit_cooldowns.get(chat_id, 0) - time()
            if cool > 0:
                if not item.important:
                    logging.debug("编辑冷却中，跳过进度刷新")
                    _finish(item.future, False)
                    continue
                if cool > MAX_IMPORTANT_COOL_WAIT:
                    logging.warning("编辑冷却仍有 %.0f 秒，放弃本次重要更新", cool)
                    _finish(item.future, False)
                    continue
                await asyncio.sleep(cool + 0.5)

            gap = MIN_EDIT_INTERVAL - (time() - q.last_at)
            if gap > 0:
                await asyncio.sleep(gap)

            ok = await _do_edit(item.message, item.text, chat_id=chat_id, q=q, **item.kwargs)
            _finish(item.future, ok)
    except asyncio.CancelledError:
        async with q.lock:
            _drop_all(q)
        raise
    except Exception:
        logging.exception("会话编辑队列异常 chat_id=%s", chat_id)
        async with q.lock:
            _drop_all(q)


async def safe_edit(message, text: str, *, important: bool = False, **kwargs) -> bool:
    """编辑消息。同 chat 轮流串行；进度可合并，important 插队并等待结果。"""
    chat_id = message.chat.id if message.chat else None
    if chat_id is None:
        return await _do_edit(message, text, chat_id=None, **kwargs)

    if not important and edit_cooldowns.get(chat_id, 0) > time():
        logging.debug("编辑冷却中，跳过进度刷新")
        return False

    q = _queue(chat_id)
    fut = asyncio.get_running_loop().create_future() if important else None
    msg_id = message.id

    async with q.lock:
        old = q.pending.get(msg_id)
        if old is not None and old.important and not important:
            return False
        if old is not None:
            _finish(old.future, False)
        if msg_id not in q.pending:
            q.order.append(msg_id)
        q.pending[msg_id] = _Pending(message, text, kwargs, important, fut)
        if q.worker is None or q.worker.done():
            q.worker = asyncio.create_task(_pump(chat_id))

    return bool(await fut) if fut is not None else True


async def delete_message_later(message, delay: float = 10.0) -> None:
    """安静模式的临时进度消息：展示一小段时间后删除，结果已进频道摘要。"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as exc:
        logging.debug("删除临时消息失败：%s: %s", type(exc).__name__, exc)


def batch_keyboard(batch: Batch) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if batch.pending_unique and not batch.stopped:
        rows.append(
            [
                InlineKeyboardButton("跳过重复", callback_data=f"bdupx {batch.id}"),
                InlineKeyboardButton("仍要下载", callback_data=f"bdupc {batch.id}"),
            ]
        )
    has_downloads = any(item.status in {"waiting", "downloading", "cold"} for item in batch.items)
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


def item_speed_line(item: BatchItem) -> str:
    if item.status != "downloading" or not item.started or not item.received:
        return ""
    elapsed = max(time() - item.started, 1)
    session_bytes = max(item.received - (item.resume_from or 0), 0)
    avg_speed = session_bytes / elapsed
    if avg_speed <= 0:
        return ""
    if item.total and item.received < item.total:
        tte = int((item.total - item.received) / avg_speed)
        return f"{humanReadableSize(avg_speed)}/s，预计还需 {humanReadableTime(tte)}"
    return f"{humanReadableSize(avg_speed)}/s"


def render_item(item: BatchItem) -> str:
    mark = STATUS_MARK.get(item.status, "•")
    name = f"`{item.name}`"
    if item.status in STATUS_LABEL:
        label = STATUS_LABEL[item.status]
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


def success_text_for(download, actual_size: int, time_took: str, speed: str) -> str:
    return dedent(
        f"""
        文件 `{download.filename}` 已下载完成。
        共 {humanReadableSize(actual_size)}，用时 {time_took}，平均速度 __{speed}/s__
        """
    )

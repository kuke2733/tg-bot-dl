"""下载进度的纯文本渲染与消息编辑辅助，manager 与 queueview 共用。

只依赖 types 与 util，不感知调度逻辑；改文案、改进度条只动这里。
"""
from __future__ import annotations

import asyncio
import logging
from textwrap import dedent
from time import time

from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.download.types import STATUS_LABEL, STATUS_MARK, Batch, BatchItem
from bot.util import humanReadableSize, humanReadableTime

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

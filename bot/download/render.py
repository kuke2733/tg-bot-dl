"""下载进度文案与键盘；safe_edit 从 tg_io 再导出供下载子模块使用。"""
from __future__ import annotations

import asyncio
import logging
from textwrap import dedent

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.download.types import STATUS_LABEL, STATUS_MARK, Batch, BatchItem
from bot.tg_io import MIN_EDIT_INTERVAL, PROGRESS_INTERVAL, safe_edit
from bot.util import humanReadableSize, humanReadableTime

__all__ = [
    "MIN_EDIT_INTERVAL",
    "PROGRESS_INTERVAL",
    "safe_edit",
    "delete_message_later",
    "batch_keyboard",
    "stop_keyboard",
    "progress_bar",
    "item_speed_line",
    "live_speed_line",
    "render_item",
    "render_batch",
    "success_text_for",
]


async def delete_message_later(message, delay: float = 10.0) -> None:
    if message is None:
        return
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


def live_speed_line(speed: float, eta: int | None, total: int = 0, received: int = 0) -> str:
    if speed <= 0:
        return "即将完成" if total and received >= total else ""
    text = f"{humanReadableSize(speed)}/s"
    if total and received < total:
        if eta is None:
            eta = int((total - received) / speed)
        return f"{text}，预计还需 {humanReadableTime(eta)}"
    if total and received >= total:
        return f"{text}，即将完成"
    return text


def item_speed_line(item: BatchItem) -> str:
    if item.status != "downloading":
        return ""
    return live_speed_line(item.speed or 0.0, item.eta, item.total, item.received)


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

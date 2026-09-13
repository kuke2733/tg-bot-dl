from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.download import manager
from bot.download.manager import (
    active_batches,
    active_downloads,
    downloads,
    handle_stop_single,
    safe_edit,
    stop,
    stop_batch_now,
)
from bot.download.types import Batch
from bot.util import clip_button_text, humanReadableSize

QUEUE_MAX_ROWS = 50


def _download_queue_line(download) -> str:
    name = Path(download.filename).name
    icon = "⬇️" if download.started else "⏳"
    size = download.expected_size or download.size
    size_text = f"（{humanReadableSize(size)}）" if size else ""
    return f"{icon} `{clip_button_text(name, 60)}`{size_text}"


def _batch_queue_line(batch: Batch) -> str:
    total = len(batch.items) or batch.total
    icon = "⬇️" if any(item.status == "downloading" for item in batch.items) else "⏳"
    return f"{icon} 📁 `{clip_button_text(batch.folder, 60)}`（完成 {batch.done}/{total}）"


def render_queue() -> tuple[str, InlineKeyboardMarkup | None]:
    running = [
        download
        for download in active_downloads
        if download.batch is None and not download.stopped and download.id not in stop
    ]
    batches = [batch for batch in active_batches.values() if not batch.stopped]
    queued = [
        download
        for download in downloads
        if download.batch is None and not download.stopped and download.id not in stop
    ]

    rows: list[tuple[str, InlineKeyboardButton]] = []
    for download in running:
        name = Path(download.filename).name
        rows.append(
            (
                _download_queue_line(download),
                InlineKeyboardButton(
                    f"⏹ {clip_button_text(name)}", callback_data=f"qstop {download.id}"
                ),
            )
        )
    for batch in batches:
        rows.append(
            (
                _batch_queue_line(batch),
                InlineKeyboardButton(
                    f"⏹ {clip_button_text(batch.folder)}", callback_data=f"qstopb {batch.id}"
                ),
            )
        )
    for download in queued:
        name = Path(download.filename).name
        rows.append(
            (
                _download_queue_line(download),
                InlineKeyboardButton(
                    f"⏹ {clip_button_text(name)}", callback_data=f"qstop {download.id}"
                ),
            )
        )

    if not rows:
        if manager.paused:
            return "⏸ 下载队列已暂停，发 /resume 恢复。", None
        return "当前没有排队或进行中的下载任务。", None

    shown = rows[:QUEUE_MAX_ROWS]
    lines = [f"📋 下载队列（共 {len(rows)} 个任务）" + ("  ⏸ 已暂停" if manager.paused else "")]
    lines += [line for line, _ in shown]
    if len(rows) > QUEUE_MAX_ROWS:
        lines.append(f"…还有 {len(rows) - QUEUE_MAX_ROWS} 个任务未显示")
    keyboard = [[button] for _, button in shown]
    keyboard.append([InlineKeyboardButton("🔄 刷新", callback_data="qref")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def refresh_queue_message(message: Message) -> None:
    text, markup = render_queue()
    await safe_edit(message, text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup, important=True)


async def _stop_batch_in_background(target: Batch) -> None:
    try:
        await stop_batch_now(target)
    except Exception:
        logging.exception("停止批次失败：%s", target.id)


async def handle_queue_stop(callback: CallbackQuery, download_id: int) -> None:
    await handle_stop_single(callback, download_id)
    if callback.message is not None:
        await refresh_queue_message(callback.message)


async def handle_queue_stop_batch(callback: CallbackQuery, batch_id: str) -> None:
    target = active_batches.get(batch_id)
    if target is None or target.stopped:
        await callback.answer("该批次已结束")
    else:
        await callback.answer("正在停止...")
        asyncio.create_task(_stop_batch_in_background(target))
    if callback.message is not None:
        await refresh_queue_message(callback.message)


async def handle_queue_refresh(callback: CallbackQuery) -> None:
    await callback.answer("已刷新")
    if callback.message is not None:
        await refresh_queue_message(callback.message)


async def handle_queue_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    if data.startswith("qstopb "):
        await handle_queue_stop_batch(callback, data.split(" ", 1)[1])
    elif data.startswith("qstop "):
        await handle_queue_stop(callback, int(data.split()[-1]))
    elif data == "qref":
        await handle_queue_refresh(callback)

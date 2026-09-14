from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from bot import callbacks
from bot.app import app
from bot.download import lifecycle, state
from bot.download.lifecycle import (
    finalize_queued_stopped,
    handle_stop_single,
    stop_batch_now,
    stop_held_download,
)
from bot.download.render import delete_message_later, safe_edit
from bot.download.state import (
    active_batches,
    active_downloads,
    downloads,
    iter_holds,
    mark_download_stopped,
    stop,
)
from bot.download.types import Batch
from bot.util import clip_button_text, humanReadableSize

QUEUE_MAX_ROWS = 50
# 定位消息存留的秒数：足够点引用跳转，随后自动删除
PROGRESS_POINTER_LIFETIME = 15.0
PROGRESS_POINTER_TEXT = "⬆️ 上面是这条任务的下载进度，点引用跳转。"


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


def _hold_queue_line(download, kind: str) -> str:
    name = Path(download.filename).name
    size = download.expected_size or download.size
    size_text = f"（{humanReadableSize(size)}）" if size else ""
    if kind == "retry":
        return (
            f"⏳ `{clip_button_text(name, 60)}`{size_text} "
            f"第 {download.retry_round}/{len(lifecycle.LONG_RETRY_DELAYS)} 次自动重试等待中"
        )
    if kind == "disk":
        return f"💾 `{clip_button_text(name, 60)}`{size_text} 磁盘已满，发 /resume 恢复"
    return f"❄️ `{clip_button_text(name, 60)}`{size_text} 等待网络恢复"


def _goto_button(target: Message | None) -> InlineKeyboardButton | None:
    """进度消息的定位按钮；拿不到所在会话就不给按钮。"""
    chat_id = target.chat.id if target is not None and target.chat else None
    if chat_id is None:
        return None
    return InlineKeyboardButton("📍 进度", callback_data=f"qgoto {chat_id} {target.id}")


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

    rows: list[tuple[str, InlineKeyboardButton | None, InlineKeyboardButton | None]] = []
    for download in running:
        name = Path(download.filename).name
        rows.append(
            (
                _download_queue_line(download),
                InlineKeyboardButton(
                    f"⏹ {clip_button_text(name)}", callback_data=f"qstop {download.id}"
                ),
                _goto_button(download.progress_message),
            )
        )
    for batch in batches:
        rows.append(
            (
                _batch_queue_line(batch),
                InlineKeyboardButton(
                    f"⏹ {clip_button_text(batch.folder)}", callback_data=f"qstopb {batch.id}"
                ),
                _goto_button(batch.message),
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
                _goto_button(download.progress_message),
            )
        )
    for download, reason in iter_holds():
        if download.batch is not None or download.stopped or download.id in stop:
            continue
        rows.append(
            (
                _hold_queue_line(download, reason),
                InlineKeyboardButton(
                    f"⏹ {clip_button_text(Path(download.filename).name)}",
                    callback_data=f"qstop {download.id}",
                ),
                _goto_button(download.progress_message),
            )
        )

    if not rows:
        if state.paused:
            return "⏸ 下载队列已暂停，发 /resume 恢复。", None
        return "当前没有排队或进行中的下载任务。", None

    shown = rows[:QUEUE_MAX_ROWS]
    lines = [f"📋 下载队列（共 {len(rows)} 个任务）" + ("  ⏸ 已暂停" if state.paused else "")]
    lines += [line for line, _, _ in shown]
    if len(rows) > QUEUE_MAX_ROWS:
        lines.append(f"…还有 {len(rows) - QUEUE_MAX_ROWS} 个任务未显示")
    keyboard = [[cancel, goto] if goto else [cancel] for _, cancel, goto in shown]
    keyboard.append([InlineKeyboardButton("🔄 刷新", callback_data="qref"),
                     InlineKeyboardButton("🧹 全部取消", callback_data="qcancelall")])
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
        # 先打上停止标记再刷新面板：后台任务还没跑到置位那一步时，
        # 这一帧渲染仍会把该批次画出来，看起来就像面板没刷新
        target.stopped = True
        asyncio.create_task(_stop_batch_in_background(target))
    if callback.message is not None:
        await refresh_queue_message(callback.message)


async def handle_queue_refresh(callback: CallbackQuery) -> None:
    await callback.answer("已刷新")
    if callback.message is not None:
        await refresh_queue_message(callback.message)


async def handle_queue_goto(callback: CallbackQuery, chat_id: int, message_id: int) -> None:
    """在进度消息下方发一条带引用的定位消息，点引用即可跳转，几秒后自动删除。"""
    try:
        pointer = await app.send_message(
            chat_id,
            PROGRESS_POINTER_TEXT,
            reply_parameters=ReplyParameters(message_id=message_id),
            parse_mode=ParseMode.DISABLED,
        )
    except Exception:
        logging.debug("发送进度定位消息失败", exc_info=True)
        await callback.answer("进度消息已失效，可能已被删除")
        return
    await callback.answer("已发出定位消息，点它的引用跳转")
    asyncio.create_task(delete_message_later(pointer, PROGRESS_POINTER_LIFETIME))


async def handle_cancel_all(callback: CallbackQuery) -> None:
    """一键取消：停止并清空所有排队与下载中的任务，含批次。"""
    for batch in list(active_batches.values()):
        batch.stopped = True  # 面板即时隐藏该批次；文件清理在后台完成
        asyncio.create_task(_stop_batch_in_background(batch))
    for download in list(downloads) + list(active_downloads):
        if download.batch is not None:
            continue  # 批次内任务由 stop_batch_now 统一收尾
        mark_download_stopped(download)
        if download.task is None:
            # 排队中还没开始传输：立即收尾，不等出队
            await finalize_queued_stopped(download)
    for download, _why in list(iter_holds()):
        if download.batch is not None:
            continue  # 批次内驻留任务由 stop_batch_now 统一收尾
        await stop_held_download(download)
    await callback.answer("已全部取消")
    if callback.message is not None:
        await refresh_queue_message(callback.message)


async def handle_queue_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    if data.startswith("qstopb "):
        await handle_queue_stop_batch(callback, data.split(" ", 1)[1])
    elif data.startswith("qstop "):
        await handle_queue_stop(callback, int(data.split()[-1]))
    elif data.startswith("qgoto "):
        _, chat_id, message_id = data.split()
        await handle_queue_goto(callback, int(chat_id), int(message_id))
    elif data == "qcancelall":
        await handle_cancel_all(callback)
    elif data == "qref":
        await handle_queue_refresh(callback)


# —— 按钮回调注册，协议前缀与路由见 bot/callbacks.py ——
callbacks.on(callbacks.Q_STOP_BATCH)(handle_queue_callback)
callbacks.on(callbacks.Q_STOP)(handle_queue_callback)
callbacks.on(callbacks.Q_GOTO)(handle_queue_callback)
callbacks.on(callbacks.Q_REFRESH)(handle_queue_callback)
callbacks.on(callbacks.Q_CANCEL_ALL)(handle_queue_callback)

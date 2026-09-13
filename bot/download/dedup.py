"""重复文件的交互处理：唯一 ID 拦截与内容哈希询问的弹窗与决策。

入队前的唯一 ID 检查在 handler 里调用，见 state.is_unique_duplicate；
这里负责下载前/后的两次「取消还是继续」交互。
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot import callbacks
from bot.app import BASE_FOLDER
from bot.download import store
from bot.download.batches import finish_batch_item, refresh_batch
from bot.download.names import replace_filename, unique_filename
from bot.download.render import delete_message_later, safe_edit
from bot.download.state import (
    HashPrompt,
    active_batches,
    pending_hash,
    pending_unique,
    queue_download,
    resolve_batch_item,
)
from bot.download.types import Download
from bot.util import humanReadableSize

UNIQUE_DUP_TEXT = "这个文件下载过，取消还是继续？"
HASH_DUP_TEXT = "和历史下载的文件内容相同，保留还是删除？"


def new_token() -> str:
    return secrets.token_hex(4)


def register_unique_prompt(download: Download) -> str:
    token = new_token()
    pending_unique[token] = download
    return token


def unique_duplicate_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("取消下载", callback_data=f"dupx {token}"),
                InlineKeyboardButton("继续下载", callback_data=f"dupc {token}"),
            ]
        ]
    )


def hash_duplicate_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("保留", callback_data=f"hkeep {token}"),
                InlineKeyboardButton("删除", callback_data=f"hdel {token}"),
            ]
        ]
    )


def mark_batch_unique_duplicate(download: Download) -> None:
    if download.batch_item:
        download.batch_item.status = "duplicate"
    if download.batch is not None:
        download.batch.pending_unique.append(download)


def ensure_unique_download_name(download: Download) -> None:
    rel = download.filename.replace("\\", "/")
    abs_path = Path(BASE_FOLDER) / rel
    used: set[str] = set()
    if download.batch:
        for sibling in download.batch.items:
            if sibling.download_id != download.id:
                used.add(sibling.name.lower())
    name = unique_filename(abs_path.name, str(abs_path.parent), used)
    download.filename = replace_filename(rel, name)
    if download.batch_item:
        download.batch_item.name = name


async def prompt_hash_duplicate(
    download: Download,
    path: str,
    sha256: str,
    success_text: str,
) -> None:
    token = new_token()
    item = resolve_batch_item(download)
    if download.batch:
        if item:
            item.status = "content_duplicate"
            item.name = Path(download.filename).name
        await finish_batch_item(download.batch)
        prompt_message = await download.batch.message.reply(
            f"`{Path(download.filename).name}`\n{HASH_DUP_TEXT}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=hash_duplicate_keyboard(token),
        )
    else:
        prompt_message = download.progress_message
        await safe_edit(
            prompt_message,
            HASH_DUP_TEXT,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=hash_duplicate_keyboard(token),
            important=True,
        )
    pending_hash[token] = HashPrompt(
        download=download,
        path=path,
        sha256=sha256,
        success_text=success_text,
        prompt_message=prompt_message,
    )


async def continue_unique_download(download: Download) -> None:
    if download.unique_id:
        store.delete_by_unique_id(download.unique_id)
    download.skip_hash_check = True
    ensure_unique_download_name(download)
    if download.batch_item:
        download.batch_item.status = "waiting"
    queue_download(download)
    if download.batch:
        return
    size = download.expected_size or download.size
    size_text = f"（{humanReadableSize(size)}）" if size else ""
    await safe_edit(
        download.progress_message,
        f"文件 `{download.filename}` 已加入下载队列{size_text}。",
        parse_mode=ParseMode.MARKDOWN,
    )


async def skip_unique_download(download: Download) -> None:
    if download.batch_item:
        download.batch_item.status = "skipped"
    if download.batch is None:
        await safe_edit(
            download.progress_message,
            "已取消下载",
            parse_mode=ParseMode.MARKDOWN,
            important=True,
        )


async def handle_unique_decision(callback: CallbackQuery, continue_download: bool) -> None:
    token = (callback.data or "").split(" ", 1)[-1]
    download = pending_unique.pop(token, None)
    if download is None:
        await callback.answer("已处理")
        return
    if continue_download:
        await callback.answer("继续下载")
        await continue_unique_download(download)
        return
    await callback.answer("已取消")
    await skip_unique_download(download)


async def handle_batch_unique_decision(callback: CallbackQuery, continue_download: bool) -> None:
    token = (callback.data or "").split(" ", 1)[-1]
    batch = active_batches.get(token)
    if batch is None:
        await callback.answer("该批次已完成或不存在")
        return
    pending = list(batch.pending_unique)
    batch.pending_unique.clear()
    if not pending:
        await callback.answer("没有待处理的重复文件")
        await refresh_batch(batch, force=True)
        return
    if continue_download:
        await callback.answer("继续下载重复文件")
        for download in pending:
            await continue_unique_download(download)
    else:
        await callback.answer("已跳过重复文件")
        for download in pending:
            await skip_unique_download(download)
        await finish_batch_item(batch)
        return
    await refresh_batch(batch, force=True)


async def handle_hash_decision(callback: CallbackQuery, keep: bool) -> None:
    token = (callback.data or "").split(" ", 1)[-1]
    prompt = pending_hash.pop(token, None)
    if prompt is None:
        await callback.answer("已处理")
        return
    download = prompt.download
    item = resolve_batch_item(download)
    name = Path(download.filename).name
    if keep:
        await callback.answer("已保留")
        if item:
            item.status = "done"
            item.name = name
        text = prompt.success_text if download.batch is None else f"`{name}` 已保留"
    else:
        await callback.answer("已删除")
        try:
            Path(prompt.path).unlink(missing_ok=True)
        except OSError:
            logging.warning("删除重复文件失败：%s", prompt.path, exc_info=True)
        if item:
            item.status = "deleted"
            item.name = name
        text = "已删除重复文件" if download.batch is None else f"`{name}` 已删除"
    if download.batch:
        await refresh_batch(download.batch, force=True)
    await safe_edit(prompt.prompt_message, text, parse_mode=ParseMode.MARKDOWN, important=True)


# —— 按钮回调注册，协议前缀与路由见 bot/callbacks.py ——


async def _dup_continue_cb(callback: CallbackQuery) -> None:
    await handle_unique_decision(callback, True)


async def _dup_skip_cb(callback: CallbackQuery) -> None:
    await handle_unique_decision(callback, False)


async def _batch_dup_continue_cb(callback: CallbackQuery) -> None:
    await handle_batch_unique_decision(callback, True)


async def _batch_dup_skip_cb(callback: CallbackQuery) -> None:
    await handle_batch_unique_decision(callback, False)


async def _hash_keep_cb(callback: CallbackQuery) -> None:
    await handle_hash_decision(callback, True)


async def _hash_delete_cb(callback: CallbackQuery) -> None:
    await handle_hash_decision(callback, False)


callbacks.on(callbacks.DUP_CONTINUE)(_dup_continue_cb)
callbacks.on(callbacks.DUP_SKIP)(_dup_skip_cb)
callbacks.on(callbacks.BATCH_DUP_CONTINUE)(_batch_dup_continue_cb)
callbacks.on(callbacks.BATCH_DUP_SKIP)(_batch_dup_skip_cb)
callbacks.on(callbacks.HASH_KEEP)(_hash_keep_cb)
callbacks.on(callbacks.HASH_DELETE)(_hash_delete_cb)

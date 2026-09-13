"""批次/任务进度消息的编排：刷新、单条收尾、批次收尾、开始下载提示。

依赖 state 与 render，不感知下载协议。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from textwrap import dedent
from time import time

from pyrogram.enums import ParseMode

from bot.download import persist as queue_persist
from bot.download.render import (
    batch_keyboard,
    delete_message_later,
    render_batch,
    safe_edit,
    stop_keyboard,
)
from bot.download.state import active_batches, downloads, rename_targets
from bot.download.types import Batch, BatchItem, Download
from bot.util import humanReadableSize

BATCH_CLEANUP_TIMEOUT = 15.0
FILE_HANDLE_RELEASE_WAIT = 1.5
BATCH_CLEANUP_CHECK_INTERVAL = 0.3


async def refresh_batch(batch: Batch, force: bool = False) -> None:
    now = time()
    if not force and batch.last_update and now - batch.last_update < 1:
        return
    batch.last_update = now
    active = batch.finished < (len(batch.items) or batch.total) and not batch.stopped
    await safe_edit(
        batch.message,
        render_batch(batch),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=batch_keyboard(batch) if active else None,
    )


async def finish_batch_item(batch: Batch) -> None:
    if batch.stopped:
        return
    await refresh_batch(batch, force=True)
    if batch.finished < (len(batch.items) or batch.total):
        return

    directory = Path(batch.directory) if batch.directory else None
    if directory and directory.is_dir() and batch.done == 0:
        try:
            if not list(directory.iterdir()):
                directory.rmdir()
                logging.warning("已删除空的分组文件夹：%s", batch.folder)
        except OSError:
            logging.debug("无法删除空文件夹：%s", directory, exc_info=True)
    if active_batches.pop(batch.id, None) is not None:
        queue_persist.remove_batch(batch.id)
    if batch.quiet and batch.message is not None:
        asyncio.create_task(delete_message_later(batch.message))


async def wait_for_batch_cleanup(batch_id: str, timeout: float = BATCH_CLEANUP_TIMEOUT) -> bool:
    start_time = time()
    while (time() - start_time) < timeout:
        queued = any(d.batch and d.batch.id == batch_id for d in downloads)
        active = any(d.batch and d.batch.id == batch_id for d in rename_targets.values())
        if not queued and not active:
            await asyncio.sleep(FILE_HANDLE_RELEASE_WAIT)
            return True
        await asyncio.sleep(BATCH_CLEANUP_CHECK_INTERVAL)
    return False


async def start_download_progress(download: Download, item: BatchItem | None) -> None:
    if download.batch is not None:
        if item:
            item.status = "downloading"
            item.name = Path(download.filename).name
            if not item.started:
                item.started = time()
        await refresh_batch(download.batch, force=True)
        return

    if download.expected_size:
        body = f"0/{humanReadableSize(download.expected_size)} 0.00%"
    else:
        body = "下载中..."
    text = dedent(
        f"""
        `{download.filename}`：
        __{body}__
        """
    )
    await safe_edit(
        download.progress_message,
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=stop_keyboard(download.id),
    )

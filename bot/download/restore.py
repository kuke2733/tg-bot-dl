"""进程重启后恢复 queue.json：重建批次与任务并入队。

瞬时失败保留落盘；仅文件已存在或源消息确认消失时丢弃。
"""
from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path

from pyrogram.enums import ParseMode
from pyrogram.types import ReplyParameters

from bot.app import BASE_FOLDER, app, user
from bot.download import cleanup, persist as queue_persist
from bot.download.state import active_batches, queue_download
from bot.download.types import Batch, BatchItem, Download
from bot.tg_io import Kind, safe_edit, safe_send


class RestoreOutcome(str, Enum):
    QUEUED = "queued"
    DROP = "drop"
    DEFER = "defer"


async def send_restore_message(chat_id: int, text: str, reply_to: int | None):
    kwargs = {}
    if reply_to is not None:
        kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to)
    sent = await safe_send(
        chat_id,
        text,
        kind=Kind.RESTORE,
        important=True,
        parse_mode=ParseMode.MARKDOWN,
        **kwargs,
    )
    if sent is not None or reply_to is None:
        return sent
    logging.debug("带引用发送恢复消息失败，改用纯文本重试")
    return await safe_send(
        chat_id,
        text,
        kind=Kind.RESTORE,
        important=True,
        parse_mode=ParseMode.MARKDOWN,
    )


async def restore_saved_queue() -> None:
    batches, tasks = queue_persist.load()
    if not tasks and not batches:
        return
    logging.info(
        "发现上次未完成的下载任务：%d 个任务 / %d 个批次，开始恢复",
        len(tasks),
        len(batches),
    )
    rebuilt: dict[str, Batch] = {}

    batch_first_source: dict[str, tuple[int, int]] = {}
    for record in tasks.values():
        batch_id = record.get("batch_id")
        if batch_id and record.get("chat_id") is not None and record.get("message_id"):
            batch_first_source.setdefault(
                str(batch_id), (record["chat_id"], record["message_id"])
            )

    for batch_id, record in batches.items():
        chat_id = record.get("chat_id")
        if chat_id is None:
            continue
        try:
            if record.get("message_id"):
                fetched = await app.get_messages(chat_id, [record["message_id"]])
                old = fetched[0] if fetched else None
                if old is not None:
                    await safe_edit(
                        old,
                        f"文件夹 `{record['folder']}` 的任务已中断，机器人重启后重新排队，请看新的进度消息。",
                        kind=Kind.RESTORE,
                        parse_mode=ParseMode.MARKDOWN,
                    )
        except Exception:
            logging.debug("标注旧批次消息失败", exc_info=True)
        reply_to = None
        source = batch_first_source.get(batch_id)
        if source and source[0] == chat_id:
            reply_to = source[1]
        new_message = await send_restore_message(
            chat_id,
            f"🔄 恢复文件夹任务 `{record['folder']}`，下载进度会在这条消息里更新。",
            reply_to,
        )
        batch = Batch(
            id=batch_id,
            folder=record["folder"],
            total=0,
            message=new_message,
            directory=record.get("directory") or "",
        )
        if new_message is None:
            logging.warning("发送批次恢复消息失败，仍保留批次记录：%s", record["folder"])
        active_batches[batch.id] = batch
        queue_persist.add_batch(queue_persist.batch_record(batch))
        rebuilt[batch_id] = batch

    restored = 0
    kept = 0
    discarded = 0
    kept_keys: set[str] = set()
    for key, record in tasks.items():
        try:
            outcome = await _restore_task(key, record, rebuilt)
        except Exception:
            logging.exception("恢复下载任务失败：%s", record.get("filename"))
            outcome = RestoreOutcome.DEFER
        if outcome is RestoreOutcome.QUEUED:
            restored += 1
        elif outcome is RestoreOutcome.DROP:
            discarded += 1
            queue_persist.remove_task(key)
        else:
            kept += 1
            kept_keys.add(key)
            logging.warning("恢复暂缓，保留落盘待下次启动：%s", record.get("filename"))

    for batch_id, batch in list(rebuilt.items()):
        if batch.items:
            continue
        still_pending = any(
            str(t.get("batch_id")) == batch_id and k in kept_keys
            for k, t in tasks.items()
        )
        if still_pending:
            continue
        if active_batches.pop(batch_id, None) is not None:
            queue_persist.remove_batch(batch_id)
        if batch.message is not None:
            await safe_edit(
                batch.message,
                f"文件夹 `{batch.folder}` 没有可恢复的任务（源消息可能已删除）。",
                kind=Kind.RESTORE,
                parse_mode=ParseMode.MARKDOWN,
                important=True,
            )
    logging.info(
        "下载任务恢复完成：入队 %d / 保留 %d / 丢弃 %d（共 %d）",
        restored,
        kept,
        discarded,
        len(tasks),
    )


async def _restore_task(
    key: str, record: dict, rebuilt: dict[str, Batch]
) -> RestoreOutcome:
    filename = record.get("filename") or ""
    if not filename:
        return RestoreOutcome.DROP
    save_path = str(Path(BASE_FOLDER) / filename)
    if (Path(BASE_FOLDER) / filename).exists():
        logging.info("恢复跳过：文件已存在，可能中断前刚完成：%s", filename)
        return RestoreOutcome.DROP
    client = user if record.get("client") == "user" else app
    if client is None:
        logging.warning("恢复暂缓：需要用户账号但未配置：%s", filename)
        return RestoreOutcome.DEFER
    chat_id = record.get("chat_id")
    message_id = record.get("message_id")
    if chat_id is None or not message_id:
        logging.warning("恢复丢弃：缺少源消息位置：%s", filename)
        cleanup.cleanup_partial_download(save_path, filename)
        return RestoreOutcome.DROP
    try:
        fetched = await client.get_messages(chat_id, [message_id])
    except Exception:
        logging.exception("恢复任务取源消息失败（保留落盘）：%s", filename)
        return RestoreOutcome.DEFER
    source = fetched[0] if fetched else None
    if source is None or getattr(source, "empty", False) or not source.media:
        logging.warning("恢复丢弃：源消息不存在或已删除：%s", filename)
        cleanup.cleanup_partial_download(save_path, filename)
        return RestoreOutcome.DROP

    batch = rebuilt.get(record.get("batch_id")) if record.get("batch_id") else None
    batch_item = None
    if batch is not None:
        batch.total += 1
        batch_item = BatchItem(
            download_id=source.id, name=Path(filename).name, status="waiting"
        )
        batch.items.append(batch_item)

    download = Download(
        client=client,
        id=source.id,
        filename=filename,
        from_message=source,
        progress_message=batch.message if batch else None,
        expected_size=record.get("expected_size") or 0,
        size=record.get("expected_size") or 0,
        batch=batch,
        batch_item=batch_item,
        pending_rename=record.get("pending_rename"),
        unique_id=record.get("unique_id") or "",
    )
    if batch is None:
        old_progress = record.get("old_progress") or {}
        notice_chat = old_progress.get("chat_id")
        if notice_chat is None:
            logging.warning("恢复暂缓：缺少进度消息位置：%s", filename)
            return RestoreOutcome.DEFER
        reply_to = message_id if chat_id == notice_chat else None
        download.progress_message = await send_restore_message(
            notice_chat,
            f"🔄 恢复下载任务 `{filename}`。",
            reply_to,
        )
        if download.progress_message is None:
            logging.warning("发送单任务恢复消息失败，仍无提示入队：%s", filename)
    queue_download(download)
    return RestoreOutcome.QUEUED

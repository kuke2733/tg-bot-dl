"""进程重启后的队列恢复：读取 queue.json，重建批次与任务并重新入队。

配合 `.temp` 断点实现跨重启续传；源消息被删等无法恢复的任务顺带清理断点。
"""
from __future__ import annotations

import logging
from pathlib import Path

from pyrogram.enums import ParseMode

from bot.app import BASE_FOLDER, app
from bot.download import cleanup, persist as queue_persist
from bot.download.render import safe_edit
from bot.download.state import active_batches, queue_download
from bot.download.types import Batch, BatchItem, Download


async def send_restore_message(chat_id: int, text: str, reply_to: int | None):
    """发恢复提示；源消息和提示在同一个会话时带上引用。引用发送失败就退回纯文本，不中断恢复。"""
    try:
        return await app.send_message(
            chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_to_message_id=reply_to
        )
    except Exception:
        if reply_to is None:
            return None
    logging.debug("带引用发送恢复消息失败，改用纯文本重试", exc_info=True)
    try:
        return await app.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        return None


async def restore_saved_queue() -> None:
    """进程重启后恢复上次未完成的下载任务（配合 .temp 断点续传）。"""
    batches, tasks = queue_persist.load()
    if not tasks and not batches:
        return
    logging.warning(
        "发现上次未完成的下载任务：%d 个任务 / %d 个批次，开始恢复", len(tasks), len(batches)
    )
    rebuilt: dict[str, Batch] = {}

    # 每个批次取第一个源消息，恢复消息带上对原媒体消息的引用，方便跳回原帖
    batch_first_source: dict[str, tuple[int, int]] = {}
    for record in tasks.values():
        batch_id = record.get("batch_id")
        if batch_id and record.get("chat_id") is not None and record.get("message_id"):
            batch_first_source.setdefault(str(batch_id), (record["chat_id"], record["message_id"]))

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
        if new_message is None:
            logging.warning("发送批次恢复消息失败：%s", record["folder"])
            continue
        batch = Batch(
            id=batch_id,
            folder=record["folder"],
            total=0,
            message=new_message,
            directory=record.get("directory") or "",
        )
        active_batches[batch.id] = batch
        queue_persist.add_batch(queue_persist.batch_record(batch))
        rebuilt[batch_id] = batch

    restored = 0
    for key, record in tasks.items():
        try:
            ok = await _restore_task(key, record, rebuilt)
        except Exception:
            logging.exception("恢复下载任务失败：%s", record.get("filename"))
            ok = False
        if ok:
            restored += 1
        else:
            queue_persist.remove_task(key)
    for batch_id, batch in list(rebuilt.items()):
        if not batch.items:
            if active_batches.pop(batch_id, None) is not None:
                queue_persist.remove_batch(batch_id)
            await safe_edit(
                batch.message,
                f"文件夹 `{batch.folder}` 没有可恢复的任务（源消息可能已删除）。",
                parse_mode=ParseMode.MARKDOWN,
            )
    logging.warning("下载任务恢复完成：%d/%d", restored, len(tasks))


async def _restore_task(key: str, record: dict, rebuilt: dict[str, Batch]) -> bool:
    filename = record.get("filename") or ""
    if not filename:
        return False
    save_path = str(Path(BASE_FOLDER) / filename)
    if (Path(BASE_FOLDER) / filename).exists():
        logging.warning("恢复跳过：文件已存在（可能中断前刚完成）：%s", filename)
        return False
    from bot.app import user

    client = user if record.get("client") == "user" else app
    if client is None:
        logging.warning("恢复跳过：需要用户账号但未配置：%s", filename)
        cleanup.cleanup_partial_download(save_path, filename)
        return False
    chat_id = record.get("chat_id")
    message_id = record.get("message_id")
    if chat_id is None or not message_id:
        logging.warning("恢复跳过：缺少源消息位置：%s", filename)
        cleanup.cleanup_partial_download(save_path, filename)
        return False
    try:
        fetched = await client.get_messages(chat_id, [message_id])
    except Exception:
        logging.exception("恢复任务取源消息失败：%s", filename)
        cleanup.cleanup_partial_download(save_path, filename)
        return False
    source = fetched[0] if fetched else None
    if source is None or getattr(source, "empty", False) or not source.media:
        logging.warning("恢复跳过：源消息不存在或已删除：%s", filename)
        cleanup.cleanup_partial_download(save_path, filename)
        return False

    batch = rebuilt.get(record.get("batch_id")) if record.get("batch_id") else None
    batch_item = None
    if batch is not None:
        batch.total += 1
        batch_item = BatchItem(download_id=source.id, name=Path(filename).name, status="waiting")
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
            logging.warning("恢复跳过：缺少进度消息位置：%s", filename)
            return False
        # 源媒体消息和恢复提示在同一个会话（私聊下载）时带上引用；频道任务的源在频道里，不能跨会话引用
        reply_to = message_id if chat_id == notice_chat else None
        download.progress_message = await send_restore_message(
            notice_chat,
            f"🔄 恢复下载任务 `{filename}`。",
            reply_to,
        )
        if download.progress_message is None:
            logging.warning("发送单任务恢复消息失败：%s", filename)
            return False
    queue_download(download)
    return True

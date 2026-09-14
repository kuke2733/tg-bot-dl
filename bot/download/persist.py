from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from bot.app import CONFIG_FOLDER, app, user
from bot.download.types import Download

# 未完成下载任务的持久化文件：入队即写入，任务结束即移除，
# 进程被硬杀，比如面板停止或断电，后最多丢最后一秒内的入队。
QUEUE_PATH = Path(CONFIG_FOLDER) / "queue.json"

_lock = threading.Lock()
_state: dict = {"batches": {}, "tasks": {}}


def load() -> tuple[dict, dict]:
    """启动时读取持久化队列，返回 (batches, tasks)。文件损坏时尝试 .bak，再不行从空开始。"""
    global _state
    with _lock:
        paths = [QUEUE_PATH, QUEUE_PATH.with_suffix(".json.bak")]
        for path in paths:
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
                _state = {
                    "batches": dict(data.get("batches") or {}),
                    "tasks": dict(data.get("tasks") or {}),
                }
                if path != QUEUE_PATH:
                    logging.warning("主队列文件不可用，已从备份恢复：%s", path.name)
                    try:
                        _save_locked()
                    except Exception:
                        logging.exception("写回主队列文件失败")
                return dict(_state["batches"]), dict(_state["tasks"])
            except FileNotFoundError:
                continue
            except Exception:
                logging.exception("读取持久化队列失败：%s", path)
                if path == QUEUE_PATH:
                    try:
                        QUEUE_PATH.replace(QUEUE_PATH.with_suffix(".json.bak"))
                    except OSError:
                        pass
        _state = {"batches": {}, "tasks": {}}
        return dict(_state["batches"]), dict(_state["tasks"])


def _save_locked() -> None:
    tmp = QUEUE_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(_state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(QUEUE_PATH)
    except OSError:
        logging.exception("写入持久化队列失败")


def save() -> None:
    with _lock:
        _save_locked()


def add_task(record: dict) -> None:
    with _lock:
        _state["tasks"][record["key"]] = record
        _save_locked()


def remove_task(key: str) -> None:
    with _lock:
        if _state["tasks"].pop(key, None) is not None:
            _save_locked()


def add_batch(record: dict) -> None:
    with _lock:
        _state["batches"][record["id"]] = record
        _save_locked()


def remove_batch(batch_id: str) -> None:
    with _lock:
        if _state["batches"].pop(batch_id, None) is not None:
            _save_locked()


def task_record(download: Download) -> dict:
    """把下载任务转成可持久化的记录。key 用「源消息 chat_id:message_id」。"""
    chat_id = None
    if download.from_message is not None and download.from_message.chat is not None:
        chat_id = download.from_message.chat.id
    old_progress = None
    progress = download.progress_message
    if progress is not None and progress.chat is not None:
        old_progress = {"chat_id": progress.chat.id, "message_id": progress.id}
    return {
        "key": f"{chat_id}:{download.id}",
        "chat_id": chat_id,
        "message_id": download.id,
        "client": "user" if download.client is user else "app",
        "filename": download.filename,
        "expected_size": download.expected_size or 0,
        "unique_id": download.unique_id or "",
        "pending_rename": download.pending_rename,
        "batch_id": download.batch.id if download.batch else None,
        "old_progress": old_progress,
    }


def batch_record(batch) -> dict:
    chat_id = None
    message_id = None
    if batch.message is not None:
        message_id = batch.message.id
        if batch.message.chat is not None:
            chat_id = batch.message.chat.id
    return {
        "id": batch.id,
        "folder": batch.folder,
        "directory": batch.directory,
        "chat_id": chat_id,
        "message_id": message_id,
    }

"""Bot 出站消息门禁：FloodWait 落盘惩罚、全局串行、分层节拍。

业务侧发/改/回应用 safe_send / safe_reply / safe_edit，勿直接调 Telegram。
下载通道 upload.GetFile 不走这里。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from time import time
from typing import Any

from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import Message, ReplyParameters

from bot.app import CONFIG_FOLDER, app

FLOOD_PATH = Path(CONFIG_FOLDER) / "flood_until"
GLOBAL_INTERVAL = 0.5
# 群聊改消息大约 20 次/分钟；2 秒一次大文件会叠 FloodWait，5 秒约 12 次/分钟。
PROGRESS_INTERVAL = 5.0
RESTORE_INTERVAL = 1.25
MAX_IMPORTANT_WAIT = 90.0
MIN_EDIT_INTERVAL = PROGRESS_INTERVAL  # 兼容旧名

_flood_until = 0.0
_flood_lock = threading.Lock()
_flood_loaded = False
_flood_notice_sent = False
_flood_skip_logged = False
_last_edit_text: dict[tuple[int, int], str] = {}
_RECENT_LIMIT = 40
_recent: deque[str] = deque(maxlen=_RECENT_LIMIT)

_last_api = _last_progress = _last_restore = 0.0
_worker: asyncio.Task | None = None
_lock = asyncio.Lock()


class Kind(str, Enum):
    NORMAL = "normal"
    PROGRESS = "progress"
    RESTORE = "restore"


@dataclass
class _Job:
    op: str  # send | edit
    kind: Kind
    important: bool
    future: asyncio.Future | None = None
    chat_id: int | None = None
    text: str = ""
    kwargs: dict = field(default_factory=dict)
    message: Any = None
    msg_key: tuple[int, int] | None = None


_queue: deque[_Job] = deque()
_progress: dict[tuple[int, int], _Job] = {}
_progress_order: deque[tuple[int, int]] = deque()


def _describe_job(job: _Job) -> str:
    preview = " ".join(str(job.text or "").split())
    if len(preview) > 80:
        preview = preview[:79] + "…"
    chat = job.chat_id
    msg: int | str = "-"
    if job.msg_key is not None:
        chat = job.msg_key[0] if chat is None else chat
        msg = job.msg_key[1]
    elif job.message is not None:
        if chat is None:
            chat = getattr(getattr(job.message, "chat", None), "id", None)
        msg = getattr(job.message, "id", "-") or "-"
    return (
        f"{job.op}/{job.kind.value} chat={chat} msg={msg} "
        f"{'重要' if job.important else '普通'} 「{preview}」"
    )


def _note_outbound(job: _Job, result: str) -> None:
    _recent.append(f"{datetime.now().strftime('%H:%M:%S')} {result} {_describe_job(job)}")


def _recent_block() -> str:
    if not _recent:
        return "  近期出站：无"
    return "  近期出站：\n" + "\n".join(f"    {item}" for item in _recent)


def _load_flood() -> None:
    global _flood_until, _flood_loaded
    if _flood_loaded:
        return
    _flood_loaded = True
    try:
        raw = FLOOD_PATH.read_text(encoding="utf-8").strip()
        _flood_until = float(raw) if raw else 0.0
    except FileNotFoundError:
        _flood_until = 0.0
    except Exception:
        logging.warning("读取限流截止时间失败：%s", FLOOD_PATH, exc_info=True)
        _flood_until = 0.0
    left = _flood_until - time()
    if left > 0:
        logging.warning(
            "仍在消息限流惩罚期，截止 %s（约 %.0f 秒）",
            datetime.fromtimestamp(_flood_until).strftime("%Y-%m-%d %H:%M:%S"),
            left,
        )


def flood_remaining() -> float:
    _load_flood()
    return max(0.0, _flood_until - time())


def in_flood_penalty() -> bool:
    return flood_remaining() > 0


def _persist_flood(until: float) -> None:
    global _flood_until
    with _flood_lock:
        _flood_until = max(_flood_until, until)
        try:
            FLOOD_PATH.write_text(str(_flood_until), encoding="utf-8")
        except OSError:
            logging.warning("写入限流截止时间失败：%s", FLOOD_PATH, exc_info=True)


def record_flood_wait(wait: int | float, job: _Job | None = None, *, rpc: str = "") -> float:
    global _flood_notice_sent
    _load_flood()
    until = max(_flood_until, time() + max(0, int(wait or 0)) + 1)
    _persist_flood(until)
    when = datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S")
    trigger = f"\n  触发：{_describe_job(job)}" if job is not None else ""
    rpc_text = f" {rpc}" if rpc else ""
    logging.warning(
        "消息限流 %s 秒%s，惩罚至 %s（待发%d 进度待改%d）%s\n%s",
        int(wait or 0),
        rpc_text,
        when,
        len(_queue),
        len(_progress),
        trigger,
        _recent_block(),
    )
    if not _flood_notice_sent:
        _flood_notice_sent = True
        try:
            asyncio.get_running_loop().create_task(_notify_flood(until))
        except RuntimeError:
            pass
    return until


async def _notify_flood(until: float) -> None:
    try:
        from bot.util import admin_chat

        admin = await admin_chat()
        if admin is None:
            return
        when = datetime.fromtimestamp(until).strftime("%H:%M")
        if flood_remaining() > MAX_IMPORTANT_WAIT:
            logging.warning("消息限流中，下载仍继续，约至 %s 恢复提示", when)
            return
        await safe_send(
            admin,
            f"消息发送被限流，下载仍继续。约至 {when} 前进度提示可能暂停。",
            important=True,
        )
    except Exception:
        logging.debug("限流通知失败", exc_info=True)


def _done(fut: asyncio.Future | None, value: Any) -> None:
    if fut is not None and not fut.done():
        fut.set_result(value)


def _fail(job: _Job) -> Any:
    return False if job.op == "edit" else None


async def _pace(kind: Kind, *, important: bool = False) -> None:
    global _last_api, _last_progress, _last_restore
    now = time()
    gap = GLOBAL_INTERVAL - (now - _last_api)
    if not important:
        if kind == Kind.PROGRESS:
            gap = max(gap, PROGRESS_INTERVAL - (now - _last_progress))
        elif kind == Kind.RESTORE:
            gap = max(gap, RESTORE_INTERVAL - (now - _last_restore))
    if gap > 0:
        await asyncio.sleep(gap)


def _touch(kind: Kind) -> None:
    global _last_api, _last_progress, _last_restore
    now = time()
    _last_api = now
    if kind == Kind.PROGRESS:
        _last_progress = now
    elif kind == Kind.RESTORE:
        _last_restore = now


async def _take() -> _Job | None:
    async with _lock:
        for i, job in enumerate(_queue):
            if job.important:
                del _queue[i]
                return job
        if _queue:
            return _queue.popleft()
        while _progress_order:
            key = _progress_order.popleft()
            job = _progress.pop(key, None)
            if job is not None:
                return job
        return None


async def _run(job: _Job) -> Any:
    global _flood_skip_logged
    left = flood_remaining()
    if left <= 0:
        _flood_skip_logged = False
    if left > 0:
        if not job.important:
            if not _flood_skip_logged:
                _flood_skip_logged = True
                logging.info("限流惩罚中，跳过普通出站：%s", _describe_job(job))
            return _fail(job)
        if left > MAX_IMPORTANT_WAIT:
            logging.warning(
                "限流惩罚仍有 %.0f 秒，放弃本次重要消息：%s",
                left,
                _describe_job(job),
            )
            return _fail(job)
        await asyncio.sleep(left + 0.3)

    if job.op == "edit" and job.msg_key and _last_edit_text.get(job.msg_key) == job.text:
        return True

    await _pace(job.kind, important=job.important)
    try:
        if job.op == "send":
            result = await app.send_message(job.chat_id, job.text, **job.kwargs)
        else:
            try:
                await job.message.edit(text=job.text, **job.kwargs)
            except MessageNotModified:
                pass
            if job.msg_key:
                _last_edit_text[job.msg_key] = job.text
            result = True
        _touch(job.kind)
        _note_outbound(job, "ok")
        return result
    except FloodWait as exc:
        wait = getattr(exc, "value", 0) or 0
        rpc = getattr(exc, "ID", "") or type(exc).__name__
        _note_outbound(job, f"FloodWait {wait}s")
        record_flood_wait(wait, job, rpc=rpc)
        return _fail(job)
    except Exception as exc:
        _note_outbound(job, type(exc).__name__)
        logging.debug("出站消息失败（%s）：%s: %s  %s", job.op, type(exc).__name__, exc, _describe_job(job))
        return _fail(job)


async def _pump() -> None:
    global _worker
    try:
        while True:
            job = await _take()
            if job is None:
                async with _lock:
                    if _queue or _progress:
                        pass
                    else:
                        _worker = None
                        return
                await asyncio.sleep(0)
                continue
            _done(job.future, await _run(job))
    except asyncio.CancelledError:
        async with _lock:
            while _queue:
                j = _queue.popleft()
                _done(j.future, _fail(j))
            for j in _progress.values():
                _done(j.future, False)
            _progress.clear()
            _progress_order.clear()
            _worker = None
        raise
    except Exception:
        logging.exception("出站消息队列异常")
        async with _lock:
            while _queue:
                j = _queue.popleft()
                _done(j.future, _fail(j))
            for j in list(_progress.values()):
                _done(j.future, False)
            _progress.clear()
            _progress_order.clear()
            _worker = None


def _drop_progress(key: tuple[int, int] | None) -> None:
    """丢掉同消息上尚未发出的进度编辑，避免盖住完成/停止文案。"""
    if key is None:
        return
    old = _progress.pop(key, None)
    if old is not None:
        _done(old.future, False)


async def _enqueue(job: _Job) -> Any:
    global _worker
    _load_flood()
    if not job.important and flood_remaining() > 0:
        return _fail(job)
    if job.important:
        job.future = asyncio.get_running_loop().create_future()

    async with _lock:
        if (
            job.op == "edit"
            and job.kind == Kind.PROGRESS
            and not job.important
            and job.msg_key is not None
        ):
            old = _progress.get(job.msg_key)
            if old is not None:
                _done(old.future, False)
            else:
                _progress_order.append(job.msg_key)
            _progress[job.msg_key] = job
        else:
            _drop_progress(job.msg_key)
            _queue.append(job)
        if _worker is None or _worker.done():
            _worker = asyncio.create_task(_pump())

    if job.future is not None:
        return await job.future
    return True if job.op == "edit" else None


async def safe_send(
    chat_id: int,
    text: str,
    *,
    kind: Kind = Kind.NORMAL,
    important: bool = True,
    **kwargs,
) -> Message | None:
    result = await _enqueue(
        _Job("send", kind, important, chat_id=chat_id, text=text, kwargs=kwargs)
    )
    return result if isinstance(result, Message) else None


async def safe_reply(
    message: Message,
    text: str,
    *,
    kind: Kind = Kind.NORMAL,
    important: bool = True,
    **kwargs,
) -> Message | None:
    if not message.chat:
        return None
    if "reply_to_message_id" not in kwargs and "reply_parameters" not in kwargs:
        kwargs["reply_parameters"] = ReplyParameters(message_id=message.id)
    return await safe_send(message.chat.id, text, kind=kind, important=important, **kwargs)


async def safe_edit(
    message,
    text: str,
    *,
    kind: Kind = Kind.PROGRESS,
    important: bool = False,
    **kwargs,
) -> bool:
    if message is None:
        return False
    chat = getattr(message, "chat", None)
    chat_id = chat.id if chat else None
    msg_id = getattr(message, "id", None)
    key = (chat_id, msg_id) if chat_id is not None and msg_id is not None else None
    return bool(
        await _enqueue(
            _Job(
                "edit",
                kind,
                important,
                message=message,
                text=text,
                kwargs=kwargs,
                msg_key=key,
            )
        )
    )

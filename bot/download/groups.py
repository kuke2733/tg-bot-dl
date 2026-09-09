from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from pyrogram.types import Message


GROUP_FLUSH_DELAY = 1.2


@dataclass
class PendingGroup:
    messages: list[Message] = field(default_factory=list)
    token: object | None = None
    notice: Message | None = None
    task: asyncio.Task | None = None


async def collect_media_group(client, buffered: list[Message]) -> list[Message]:
    by_id = {item.id: item for item in buffered if item}
    first = buffered[0] if buffered else None
    if first is None or first.chat is None:
        return [item for item in by_id.values() if item and item.media]
    try:
        grouped = await client.get_media_group(first.chat.id, first.id)
        ordered = [item for item in grouped if item and item.media] if grouped else []
        if ordered:
            return ordered
        for item in grouped or []:
            by_id.setdefault(item.id, item)
    except Exception as error:
        logging.warning("获取整组文件失败，使用已收到的消息：%s", error)
    return [item for item in by_id.values() if item and item.media]


class MediaGroupCollector:
    def __init__(self) -> None:
        self._pending: dict[int | str, PendingGroup] = {}
        self._lock = asyncio.Lock()

    async def add(self, message: Message, client, on_ready) -> bool:
        group_id = getattr(message, "media_group_id", None)
        if not group_id:
            return False

        async with self._lock:
            pending = self._pending.get(group_id)
            if pending is None:
                notice = await client.send_message(
                    message.chat.id,
                    "收到一组文件，正在整理...",
                    reply_to_message_id=message.id,
                )
                pending = PendingGroup(messages=[message], notice=notice)
                pending.token = object()
                pending.task = asyncio.create_task(
                    self._flush(group_id, pending.token, client, on_ready)
                )
                self._pending[group_id] = pending
                return True
            if any(item.id == message.id for item in pending.messages):
                return True
            pending.messages.append(message)
            pending.token = object()
            if pending.task:
                pending.task.cancel()
            pending.task = asyncio.create_task(
                self._flush(group_id, pending.token, client, on_ready)
            )
            return True

    async def _flush(self, group_id, token, client, on_ready) -> None:
        try:
            await asyncio.sleep(GROUP_FLUSH_DELAY)
        except asyncio.CancelledError:
            return
        async with self._lock:
            pending = self._pending.get(group_id)
            if not pending or pending.token is not token:
                return
            self._pending.pop(group_id, None)
            buffered = list(pending.messages)
            notice = pending.notice
        messages = await collect_media_group(client, buffered)
        await on_ready(messages, notice)

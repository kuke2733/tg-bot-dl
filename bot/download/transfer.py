from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from pyrogram import StopTransmission
from pyrogram.client import Client
from pyrogram.errors import (
    FileReferenceEmpty,
    FileReferenceExpired,
    FileReferenceInvalid,
    FloodWait,
)
from pyrogram.file_id import FileId
from pyrogram.types import Message

from bot.download.names import message_media

CHUNK_SIZE = 1024 * 1024
MAX_ATTEMPTS = 6
MAX_FLOOD_RETRIES = 10
MAX_BACKOFF_SECONDS = 30

RetryCallback = Callable[[int, BaseException, int], Awaitable[None] | None]

_FILE_REF_ERRORS = (
    FileReferenceExpired,
    FileReferenceInvalid,
    FileReferenceEmpty,
)


def temp_path_for(save_path: str) -> str:
    return f"{save_path}.temp"


def align_down(size: int) -> int:
    return (size // CHUNK_SIZE) * CHUNK_SIZE


def prepare_temp_file(temp_path: str) -> int:
    path = Path(temp_path)
    if not path.is_file():
        return 0
    size = path.stat().st_size
    aligned = align_down(size)
    if aligned < size:
        with path.open("rb+") as file:
            file.truncate(aligned)
        logging.debug("截断未对齐分片：%s %d -> %d", temp_path, size, aligned)
    return aligned


async def refresh_message(client: Client, message: Message) -> Message:
    chat = message.chat
    if chat is None:
        raise ValueError("无法刷新消息：缺少 chat")
    refreshed = await client.get_messages(chat.id, message.id)
    if refreshed is None or getattr(refreshed, "empty", False):
        raise ValueError(f"无法刷新消息：chat={chat.id} id={message.id}")
    return refreshed


def resolve_file_id(message: Message) -> tuple[FileId, int]:
    media, _ = message_media(message)
    if media is None:
        raise ValueError("消息没有可下载媒体")
    file_id_str = getattr(media, "file_id", None)
    if not file_id_str:
        raise ValueError("媒体缺少 file_id")
    size = int(getattr(media, "file_size", 0) or 0)
    return FileId.decode(file_id_str), size


async def _download_once(
    client: Client,
    message: Message,
    temp_path: str,
    *,
    file_size: int,
    progress,
    progress_args: tuple,
) -> None:
    file_id, media_size = resolve_file_id(message)
    total_size = file_size or media_size

    Path(temp_path).parent.mkdir(parents=True, exist_ok=True)
    existing = prepare_temp_file(temp_path)

    if total_size and existing >= total_size:
        return

    offset_chunks = existing // CHUNK_SIZE
    mode = "ab" if existing else "wb"

    with open(temp_path, mode) as file:
        async for chunk in client.get_file(
            file_id,
            total_size,
            0,
            offset_chunks,
            progress,
            progress_args,
        ):
            if not chunk:
                break
            file.write(chunk)


def _backoff_seconds(attempt: int) -> float:
    return min(2 ** (attempt - 1), MAX_BACKOFF_SECONDS)


async def download_with_resume(
    client: Client,
    message: Message,
    save_path: str,
    *,
    progress=None,
    progress_args: tuple = (),
    file_size: int = 0,
    on_retry: RetryCallback | None = None,
) -> tuple[str | None, Message]:
    """下载到 save_path；失败保留 .temp 续传。停止时返回 (None, message)。"""
    temp_path = temp_path_for(save_path)
    attempt = 0
    flood_retries = 0

    while attempt < MAX_ATTEMPTS:
        attempt += 1
        try:
            await _download_once(
                client,
                message,
                temp_path,
                file_size=file_size,
                progress=progress,
                progress_args=progress_args,
            )
            break
        except StopTransmission:
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except OSError:
                logging.debug("停止后清理临时文件失败：%s", temp_path, exc_info=True)
            return None, message
        except FloodWait as exc:
            flood_retries += 1
            if flood_retries > MAX_FLOOD_RETRIES:
                raise
            wait = int(getattr(exc, "value", 0) or 0) + 1
            logging.warning("下载触发限流，等待 %s 秒后续传：%s", wait, save_path)
            if on_retry:
                result = on_retry(attempt, exc, prepare_temp_file(temp_path))
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(wait)
            attempt -= 1  # FloodWait 不计入普通失败次数
            continue
        except _FILE_REF_ERRORS as exc:
            logging.warning("file_reference 失效，刷新消息后续传：%s", save_path)
            try:
                message = await refresh_message(client, message)
            except Exception:
                logging.exception("刷新消息失败：%s", save_path)
                if attempt >= MAX_ATTEMPTS:
                    raise exc
                await asyncio.sleep(_backoff_seconds(attempt))
                continue
            if on_retry:
                result = on_retry(attempt, exc, prepare_temp_file(temp_path))
                if asyncio.iscoroutine(result):
                    await result
            continue
        except Exception as exc:
            resumed = prepare_temp_file(temp_path)
            if attempt >= MAX_ATTEMPTS:
                logging.exception(
                    "下载失败且已达最大重试次数（%d）：%s，已续传起点 %d",
                    MAX_ATTEMPTS,
                    save_path,
                    resumed,
                )
                raise
            logging.warning(
                "下载中断，将从 %d 字节续传（第 %d/%d 次）：%s：%s",
                resumed,
                attempt,
                MAX_ATTEMPTS,
                save_path,
                exc,
            )
            if on_retry:
                result = on_retry(attempt, exc, resumed)
                if asyncio.iscoroutine(result):
                    await result
            await asyncio.sleep(_backoff_seconds(attempt))
    else:
        raise RuntimeError(f"下载未完成：{save_path}")

    Path(save_path).unlink(missing_ok=True)
    os.replace(temp_path, save_path)
    return save_path, message

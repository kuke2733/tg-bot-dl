from __future__ import annotations

import asyncio
import errno
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


class PauseTransmission(Exception):
    """队列暂停：中断当前传输，保留 .temp 断点。"""


class DownloadExhausted(RuntimeError):
    """短周期重试耗尽；.temp 已保留，由上层安排长周期自动重试。"""

    def __init__(self, resumed: int = 0, wait: int = 0, disk_full: bool = False):
        super().__init__(f"下载重试耗尽（已下载 {resumed} 字节）")
        self.resumed = resumed
        # FloodWait 带来的建议等待秒数；普通耗尽为 0
        self.wait = wait
        self.disk_full = disk_full


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
    pause_check=None,
) -> None:
    file_id, media_size = resolve_file_id(message)
    total_size = file_size or media_size

    Path(temp_path).parent.mkdir(parents=True, exist_ok=True)
    existing = prepare_temp_file(temp_path)

    # 断点比源文件还大说明临时文件损坏：归零重新下载，避免坏断点被当成品
    if total_size and existing > total_size:
        logging.warning(
            "断点临时文件比源文件还大，归零重新下载：%s（%d > %d 字节）",
            temp_path, existing, total_size,
        )
        with open(temp_path, "wb"):
            pass
        existing = 0

    if total_size and existing >= total_size:
        return

    if pause_check and pause_check():
        raise PauseTransmission

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
            if pause_check and pause_check():
                raise PauseTransmission


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
    pause_check=None,
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
                pause_check=pause_check,
            )
            break
        except PauseTransmission:
            return None, message
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
                raise DownloadExhausted(
                    prepare_temp_file(temp_path), wait=int(getattr(exc, "value", 0) or 0) + 1
                ) from exc
            wait = int(getattr(exc, "value", 0) or 0) + 1
            rpc = getattr(exc, "ID", "") or type(exc).__name__
            logging.warning(
                "下载通道限流 %s 秒（%s，第 %d/%d 次），等待后续传：%s",
                wait,
                rpc,
                flood_retries,
                MAX_FLOOD_RETRIES,
                save_path,
            )
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
                    raise DownloadExhausted(prepare_temp_file(temp_path)) from exc
                await asyncio.sleep(_backoff_seconds(attempt))
                continue
            if on_retry:
                result = on_retry(attempt, exc, prepare_temp_file(temp_path))
                if asyncio.iscoroutine(result):
                    await result
            continue
        except Exception as exc:
            resumed = prepare_temp_file(temp_path)
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                logging.error("磁盘已满：%s，断点保留在 %d 字节", save_path, resumed)
                raise DownloadExhausted(resumed, disk_full=True) from exc
            if attempt >= MAX_ATTEMPTS:
                logging.warning(
                    "下载失败且已达最大重试次数（%d）：%s，断点保留在 %d 字节，交由长周期重试",
                    MAX_ATTEMPTS,
                    save_path,
                    resumed,
                )
                raise DownloadExhausted(resumed) from exc
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
        raise DownloadExhausted(prepare_temp_file(temp_path))

    Path(save_path).unlink(missing_ok=True)
    os.replace(temp_path, save_path)
    return save_path, message

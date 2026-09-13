from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from bot.app import BASE_FOLDER
from bot.download.types import Batch

# 残留文件后台重试清理的间隔和总时长
CLEANUP_RETRY_INTERVAL = 2.0
CLEANUP_RETRY_TIMEOUT = 300.0


def cleanup_directory(directory: Path, folder: str) -> tuple[int, int]:
    deleted = 0
    remaining = 0
    try:
        for file_path in directory.iterdir():
            if not file_path.is_file():
                continue
            try:
                file_path.unlink()
                deleted += 1
                logging.warning("已删除文件：%s", file_path.name)
            except OSError as e:
                remaining += 1
                logging.warning("无法删除文件 %s: %s", file_path.name, e)

        if remaining == 0:
            try:
                directory.rmdir()
                logging.warning("已删除分组文件夹：%s", folder)
            except OSError as e:
                logging.warning("无法删除文件夹 %s: %s", folder, e)
                remaining = len(list(directory.iterdir()))
        else:
            logging.warning("文件夹 %s 仍有 %d 个文件无法删除", folder, remaining)
    except OSError as e:
        logging.error("清理文件夹失败 %s: %s", folder, e)
    return deleted, remaining


def cleanup_batch_files(batch: Batch) -> tuple[int, int]:
    directory = Path(batch.directory) if batch.directory else None
    if not directory or not directory.is_dir():
        return 0, 0
    if directory.resolve() == Path(BASE_FOLDER).resolve():
        return 0, 0
    return cleanup_directory(directory, batch.folder)


def schedule_cleanup_retry(directory: str, folder: str) -> None:
    """残留文件夹的后台重试清理：等句柄释放后把残留删干净。"""

    async def retry() -> None:
        path = Path(directory)
        deadline = time() + CLEANUP_RETRY_TIMEOUT
        while time() < deadline:
            await asyncio.sleep(CLEANUP_RETRY_INTERVAL)
            if not path.is_dir():
                return
            cleanup_directory(path, folder)
            if not path.is_dir():
                logging.warning("重试清理完成，已删除残留文件夹：%s", folder)
                return
        logging.warning("文件夹 %s 清理重试超时，请手动删除：%s", folder, directory)

    asyncio.create_task(retry())


def schedule_file_cleanup_retry(save_path: str) -> None:
    """单个残留下载文件（含 .temp）的后台重试清理。"""

    async def retry() -> None:
        file_path = Path(save_path)
        temp_path = Path(save_path + ".temp")
        deadline = time() + CLEANUP_RETRY_TIMEOUT
        while time() < deadline:
            await asyncio.sleep(CLEANUP_RETRY_INTERVAL)
            try:
                if file_path.exists():
                    file_path.unlink()
                    logging.warning("已删除残留下载文件：%s", file_path.name)
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                continue
            if not file_path.exists() and not temp_path.exists():
                return
        logging.warning("残留文件 %s 清理重试超时，请手动删除", save_path)

    asyncio.create_task(retry())


def cleanup_partial_download(save_path: str, filename: str) -> None:
    try:
        file_path = Path(save_path)
        if file_path.exists():
            file_path.unlink()
            logging.warning("已删除部分下载文件：%s", filename)
        temp_path = Path(save_path + ".temp")
        if temp_path.exists():
            temp_path.unlink()
    except OSError:
        logging.warning("下载文件暂时无法删除（句柄未释放），稍后自动重试：%s", filename)
        schedule_file_cleanup_retry(save_path)

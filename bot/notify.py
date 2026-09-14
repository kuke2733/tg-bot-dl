from __future__ import annotations

import logging
import time
from pathlib import Path

from pyrogram.enums import ParseMode

from bot.app import CONFIG_FOLDER, app
from bot.util import admin_chat
from bot.version import APP_NAME, VERSION

# 上次运行记录的版本号：与当前版本不一致就给管理员发一条更新提示
LAST_VERSION_PATH = Path(CONFIG_FOLDER) / "last_version"

# 磁盘满通知的节流：并发任务接连撞上时只发一条
DISK_FULL_NOTIFY_INTERVAL = 60.0
_last_disk_full_notify = 0.0


def _save(version: str) -> None:
    try:
        LAST_VERSION_PATH.write_text(version, encoding="utf-8")
    except OSError:
        logging.warning("记录版本号失败：%s", LAST_VERSION_PATH, exc_info=True)


async def notify_version_update() -> None:
    try:
        last = LAST_VERSION_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        last = ""
    except OSError:
        logging.warning("读取上次版本号失败：%s", LAST_VERSION_PATH, exc_info=True)
        return
    if last == VERSION:
        return
    if not last:
        # 首次运行，没有历史版本记录：只记录，不算更新
        logging.info("首次运行，记录版本号 %s", VERSION)
        _save(VERSION)
        return
    admin = await admin_chat()
    if admin is None:
        logging.warning("版本更新提示未发送：管理员会话不可用，下次启动会重试")
        return
    try:
        await app.send_message(
            admin,
            f"🆕 {APP_NAME} 已更新：{last} → {VERSION}\n发 /help 可查看当前命令列表。",
            parse_mode=ParseMode.DISABLED,
        )
    except Exception:
        logging.warning("发送版本更新提示失败，下次启动会重试", exc_info=True)
        return
    logging.info("已通知管理员程序更新：%s -> %s", last, VERSION)
    _save(VERSION)


async def notify_disk_full(filename: str) -> None:
    global _last_disk_full_notify
    now = time.monotonic()
    if now - _last_disk_full_notify < DISK_FULL_NOTIFY_INTERVAL:
        return
    _last_disk_full_notify = now
    admin = await admin_chat()
    if admin is None:
        logging.warning("磁盘已满但管理员会话不可用，无法通知")
        return
    try:
        await app.send_message(
            admin,
            f"💾 磁盘已满：{filename} 的下载已暂停并保留断点。清理空间后发 /resume 继续。",
            parse_mode=ParseMode.DISABLED,
        )
    except Exception:
        logging.warning("发送磁盘满通知失败", exc_info=True)

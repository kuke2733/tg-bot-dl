import asyncio
import logging

from pyrogram import idle
from pyrogram.errors import FloodWait

from bot.app import app, user
from bot import commands, listener, notify
from bot.download import lifecycle, restore
from bot.download import manager as download_manager

# 会话健康检查：连续失败达到阈值就重启连接。
# 只重启连接、不动进程，下载队列和任务都还在内存里，传输会自动断点续传。
SESSION_CHECK_INTERVAL = 60
SESSION_CHECK_TIMEOUT = 30
SESSION_FAIL_THRESHOLD = 3


async def restart_clients():
    logging.warning("Telegram 会话疑似假死，正在重启连接，进程和下载队列保持不动...")
    await app.stop()
    await app.start()
    if user:
        try:
            await user.stop()
        except Exception:
            logging.debug("停止用户客户端失败", exc_info=True)
        await user.start()
    logging.warning("Telegram 连接已恢复，下载任务会自动续传")


async def session_monitor():
    failures = 0
    while True:
        await asyncio.sleep(SESSION_CHECK_INTERVAL)
        try:
            await asyncio.wait_for(app.get_me(), timeout=SESSION_CHECK_TIMEOUT)
            if user:
                # 用户账号承担收帖和下载，会话假死同样要发现并重启
                await asyncio.wait_for(user.get_me(), timeout=SESSION_CHECK_TIMEOUT)
            failures = 0
            # 连接正常就唤醒冷驻留任务，自动重试轮次用尽后保留断点等在这里
            try:
                await lifecycle.wake_cold_holds()
            except Exception:
                logging.exception("唤醒冷驻留下载任务失败")
            continue
        except FloodWait:
            failures = 0  # 能收到限流响应，说明连接本身是通的
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            logging.warning("会话健康检查失败（%d/%d）", failures, SESSION_FAIL_THRESHOLD)
        if failures >= SESSION_FAIL_THRESHOLD:
            try:
                await restart_clients()
            except Exception:
                logging.exception("自动重启连接失败，下一轮继续尝试")
                continue
            failures = 0


async def main():
    logging.warning("Registering commands...")
    commands.register(app)
    logging.warning("Starting bot...")
    await app.start()
    await commands.set_menu(app)
    if user:
        logging.warning("Starting normal user")
        await user.start()
        listener.register_user_channel_handler(user)
    logging.warning("Starting download manager...")
    manager = asyncio.create_task(download_manager.run())
    try:
        await restore.restore_saved_queue()
    except Exception:
        logging.exception("恢复上次下载队列失败")
    try:
        await notify.notify_version_update()
    except Exception:
        logging.exception("版本更新提示流程失败")
    monitor = asyncio.create_task(session_monitor())
    me = await app.get_me()
    logging.warning("Bot started! I'm @%s", me.username)
    await idle()
    logging.warning("Stopping download manager...")
    monitor.cancel()
    manager.cancel()
    logging.warning("Stopping bot...")
    await app.stop()
    if user:
        logging.warning("Stopping user...")
        await user.stop()
    logging.warning("All systems stopped!")
    return 0


if __name__ == "__main__":
    event_loop = asyncio.get_event_loop()
    event_loop.run_until_complete(main())

import asyncio
import logging

from pyrogram import idle

from bot.app import app, user
from bot import commands
from bot.download import manager as download_manager


async def main():
    logging.warning("Registering commands...")
    commands.register(app)
    logging.warning("Starting bot...")
    await app.start()
    await commands.set_menu(app)
    if user:
        logging.warning("Starting normal user")
        await user.start()
    logging.warning("Starting download manager...")
    manager = asyncio.create_task(download_manager.run())
    me = await app.get_me()
    logging.warning("Bot started! I'm @%s", me.username)
    await idle()
    logging.warning("Stopping download manager...")
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

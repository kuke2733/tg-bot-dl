from typing import Coroutine
from datetime import timedelta

from pyrogram import Client
from pyrogram.types import Message

from bot.app import ADMINS

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB


def humanReadableSize(size: float) -> str:
    symbol, divider = "B", 1
    if size >= GIB:
        symbol, divider = "GiB", GIB
    elif size >= MIB:
        symbol, divider = "MiB", MIB
    elif size >= KIB:
        symbol, divider = "KiB", KIB
    readableSize = size / divider
    return f"{readableSize:.1f} {symbol}"

def humanReadableTime(s: int) -> str:
    time = timedelta(seconds=s)
    hours, remaining = divmod(time.seconds, 3600)
    minutes, seconds = divmod(remaining, 60)
    
    parts = []
    if time.days > 0:
        parts.append(f"{time.days}天")
    if hours > 0:
        parts.append(f"{hours}小时")
    if minutes > 0:
        parts.append(f"{minutes}分")
    if seconds > 0 or not parts:  # Show seconds if theres no other parts
        parts.append(f"{seconds}秒")

    return " ".join(parts)

def is_admin(message: Message) -> bool:
    user = message.from_user
    chat = message.chat
    names = []
    ids = []
    if user:
        if user.username:
            names.append("@" + user.username)
        ids.append(str(user.id))
    if chat:
        if chat.username:
            names.append("@" + chat.username)
        ids.append(str(chat.id))
    return any(name in ADMINS for name in names) or any(i in ADMINS for i in ids)


def checkAdmins(func: Coroutine) -> Coroutine:
    async def wrapper(app: Client, message: Message):
        if not is_admin(message):
            await message.reply("你不是管理员，不能使用这个机器人。")
            return
        return await func(app, message)

    return wrapper


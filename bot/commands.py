import logging
import re
from textwrap import dedent
from urllib.parse import urlparse

from pyrogram.client import Client
from pyrogram.enums import ParseMode
from pyrogram.filters import command, document, media, text
from pyrogram.handlers.callback_query_handler import CallbackQueryHandler
from pyrogram.handlers.message_handler import MessageHandler
from pyrogram.types import BotCommand, Message

from bot.app import DL_FOLDER, user
from bot import folder, sysinfo
from bot.download import handler as download_handler
from bot.download import manager as download_manager
from bot.util import checkAdmins

LINK_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
LINK_HELP = (
    "链接格式不对。请使用消息链接，例如：\n"
    "`https://t.me/c/1234567890/123`（私有频道/群组）\n"
    "`https://t.me/频道用户名/123`（公开频道/群组）"
)

bot_help = """
把文件发给我，就会下载到这台电脑。

【怎么改名】（不用写后缀，自动补）
1. 转发时写上名字（先到名字、再到文件）
2. 回复下载进度消息，发新名字
3. 发文件时，在说明里写名字
4. /add <链接> <名字>
一组文件时，以上改名只作用于文件夹名。

【相册】
一次发多个文件会放进同一个文件夹；单个文件不建文件夹。

【其它】
下载中可点「停止」。禁止转发的内容用 /add 下。

【命令】
"""


def parse_message_link(raw: str):
    """解析 Telegram 消息链接，返回 (chat, message_id)。chat 为 int 或用户名。"""
    text = (raw or "").strip().strip("<>")
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host not in LINK_HOSTS:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None

    # https://t.me/s/username/123 → 网页预览链接
    if parts[0] == "s":
        parts = parts[1:]

    # 私有频道/群组：/c/<internal_id>/[topic_id/]<message_id>
    if parts[0] == "c":
        if len(parts) < 3 or not parts[1].isdigit() or not parts[-1].isdigit():
            return None
        return int(f"-100{parts[1]}"), int(parts[-1])

    # 邀请链接无法定位具体消息
    if parts[0] in ("joinchat",) or parts[0].startswith("+"):
        return None

    # 公开频道/群组：/<username>/[topic_id/]<message_id>
    if len(parts) < 2 or not parts[-1].isdigit():
        return None
    username = parts[0].lstrip("@")
    if not re.fullmatch(r"[A-Za-z][\w\d]{3,}", username):
        return None
    return username, int(parts[-1])


def register(app: Client):
    addCommand(app, start, "start")
    addCommand(app, botHelp, "help")
    addCommand(app, usage, "usage")
    addCommand(app, useFolder, "use")
    addCommand(app, leaveFolder, "leave")
    addCommand(app, getFolder, "get")
    addCommand(app, addByLink, "add")
    app.add_handler(
        MessageHandler(checkAdmins(download_handler.addFile), document | media)
    )
    app.add_handler(
        MessageHandler(checkAdmins(download_handler.renameFromText), text)
    )
    app.add_handler(CallbackQueryHandler(download_manager.stopDownload))


async def set_menu(app: Client):
    await app.set_bot_commands(
        [
            BotCommand("start", "开始使用"),
            BotCommand("help", "查看帮助"),
            BotCommand("usage", "查看磁盘空间"),
            BotCommand("use", "设置下载子目录"),
            BotCommand("get", "查看当前目录"),
            BotCommand("leave", "回到根目录"),
            BotCommand("add", "通过链接下载文件"),
        ]
    )


def addCommand(app, func, cmd):
    global bot_help
    bot_help += f"/{cmd} - {dedent(func.__doc__ or '暂无说明').strip()}\n"
    app.add_handler(MessageHandler(checkAdmins(func), command(cmd)))


async def start(_, message: Message):
    """开始使用"""
    await message.reply(
        dedent("""
        你好！
        把文件发给我，我会下载到这台电脑里。
        需要帮助的话，发送 /help
    """)
    )


async def botHelp(_, message: Message):
    """查看帮助"""
    global bot_help
    await message.reply(bot_help)


async def addByLink(_, message: Message):
    """用消息链接下载（可加名字：/add <链接> <名字>）"""
    if not user:
        await message.reply("还没有配置用户账号，没法读取这类消息。")
        return
    messageParts = (message.text or "").split()
    if len(messageParts) < 2:
        await message.reply(
            "请发一条消息链接给我。\n"
            "示例：`/add https://t.me/c/1234567890/123`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    parsed = parse_message_link(messageParts[1])
    if not parsed:
        await message.reply(LINK_HELP, parse_mode=ParseMode.MARKDOWN)
        return

    chat, message_id = parsed
    try:
        messages = await user.get_messages(chat, [message_id])
    except Exception:
        logging.exception("通过用户账号读取消息失败 chat=%s message_id=%s", chat, message_id)
        await message.reply("用当前用户账号找不到这条消息。请确认这个账号已经加入对应频道或群组。")
        return
    if not messages or not messages[0] or not messages[0].media:
        await message.reply("这条链接对应的消息里没有文件。")
        return
    await download_handler.addFileFromUser(messages[0], message)


async def usage(_, message: Message):
    """查看磁盘空间"""
    usage = sysinfo.diskUsage(DL_FOLDER)
    await message.reply(
        dedent(f"""
            当前存储位置总容量 __{usage.capacity}__
            其中已用 __{usage.used}__，剩余 __{usage.free}__。
        """),
        parse_mode=ParseMode.MARKDOWN,
    )


async def useFolder(_, message: Message):
    """设置下载子目录：/use <路径>"""
    args = message.text.split()
    userSetPath = " ".join(args[1:]).strip()
    if not userSetPath:
        await message.reply("还没有告诉我文件要放到哪个目录。")
        return
    path = userSetPath.replace("../", "").replace("/..", "")
    if userSetPath != path:
        await message.reply(f"注意：实际目录是 `{path}`，不是 `{' '.join(args[1:])}`")
    folder.set(path)
    await message.reply("好的，现在把文件发给我，我会保存到这个目录。")


async def leaveFolder(_, message: Message):
    """回到根目录"""
    folder.reset()
    await message.reply("已经回到根目录了。")


async def getFolder(_, message: Message):
    """查看当前下载目录"""
    path = folder.getPath()
    await message.reply(f"当前目录是 `{path}`")

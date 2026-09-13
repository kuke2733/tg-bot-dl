import logging
import re
from textwrap import dedent
from urllib.parse import urlparse

from pyrogram.client import Client
from pyrogram.enums import ParseMode
from pyrogram.filters import command, document, media, text
from pyrogram.handlers.callback_query_handler import CallbackQueryHandler
from pyrogram.handlers.message_handler import MessageHandler
from pyrogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.app import DL_FOLDER, user
from bot import folder, listener, sysinfo
from bot.download import handler as download_handler
from bot.download import manager as download_manager
from bot.download import queueview
from bot.filebrowser import listFiles
from bot.util import checkAdmins, is_admin_user

LINK_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
LINK_HELP = (
    "链接格式不对。请使用消息链接，例如：\n"
    "`https://t.me/c/1234567890/123`（私有频道/群组）\n"
    "`https://t.me/频道用户名/123`（公开频道/群组）"
)

bot_help = """
把文件发给我，就会下载到这台电脑里。

【怎么改名】（不用写后缀，自动补）
1. 转发时写上名字（先到名字、再到文件）
2. 回复下载进度消息，发新名字
3. 发文件时，在说明里写名字
4. /add <链接> <名字>
一组文件时，以上改名只作用于文件夹名。

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
    addCommand(app, showQueue, "queue")
    addCommand(app, listFiles, "files")
    addCommand(app, pauseQueue, "pause")
    addCommand(app, resumeQueue, "resume")
    addCommand(app, listener.listen, "listen")
    addCommand(app, listener.listening, "listening")
    app.add_handler(MessageHandler(onIncomingMessage), group=-100)
    # 频道监听要先于 addFile 的媒体处理器注册：同一组内先匹配先执行，
    # 频道帖由监听接管，避免 addFile 往频道里回「你不是管理员」。
    listener.register(app)
    app.add_handler(
        MessageHandler(checkAdmins(download_handler.addFile), document | media)
    )
    app.add_handler(
        MessageHandler(checkAdmins(download_handler.renameFromText), text)
    )
    app.add_handler(CallbackQueryHandler(download_manager.handle_callback))


UNAVAILABLE_HINTS = (
    "temporarily suspended",
    "group has been temporarily",
    "channel can't be displayed",
    "message is not available",
    "this message is unavailable",
)


async def onIncomingMessage(_, message: Message):
    media = getattr(message.media, "value", message.media) if message.media else None
    logging.debug(
        "消息 chat=%s from=%s id=%s media=%s text=%r",
        getattr(message.chat, "id", None),
        getattr(message.from_user, "id", None),
        message.id,
        media,
        (message.text or message.caption or "")[:80],
    )
    if media:
        return

    body = f"{message.text or ''} {message.caption or ''}".lower()
    if not any(hint in body for hint in UNAVAILABLE_HINTS):
        return
    try:
        await message.reply("当前消息存在问题，请隐藏发送者名称后重新发送")
    except Exception:
        logging.debug("回复失败", exc_info=True)


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
            BotCommand("queue", "查看下载队列"),
            BotCommand("files", "管理已下载文件"),
            BotCommand("pause", "暂停接收新任务"),
            BotCommand("resume", "恢复下载队列"),
            BotCommand("listen", "监听频道自动下载"),
            BotCommand("listening", "查看监听的频道"),
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
    """),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📋 下载队列", callback_data="menu queue"),
                 InlineKeyboardButton("📂 文件管理", callback_data="menu files")],
                [InlineKeyboardButton("📡 监听频道", callback_data="menu listening"),
                 InlineKeyboardButton("💾 磁盘空间", callback_data="menu usage")],
                [InlineKeyboardButton("❓ 帮助", callback_data="menu help")],
            ]
        ),
    )


async def handle_menu_callback(callback: CallbackQuery) -> None:
    """/start 菜单按钮：以机器人回复的方式打开对应页面。"""
    mapping = {
        "queue": showQueue,
        "files": listFiles,
        "listening": listener.listening,
        "usage": usage,
        "help": botHelp,
    }
    action = (callback.data or "").split(" ", 1)[-1]
    fn = mapping.get(action)
    if fn is None:
        await callback.answer()
        return
    if not is_admin_user(callback.from_user):
        await callback.answer("你不是管理员", show_alert=True)
        return
    await callback.answer()
    await fn(None, callback.message)


async def botHelp(_, message: Message):
    """查看帮助"""
    global bot_help
    await message.reply(bot_help)


async def addByLink(_, message: Message):
    """用消息链接下载（可加名字：/add <链接> <名字>）"""
    if not user:
        await message.reply("还没有配置用户账号，没法读取这类消息。")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(
            "请发一条消息链接给我。\n"
            "示例：`/add https://t.me/c/1234567890/123`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    parsed = parse_message_link(parts[1])
    if not parsed:
        await message.reply(LINK_HELP, parse_mode=ParseMode.MARKDOWN)
        return

    await download_handler.addFromLink(message, *parsed)


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
    await message.reply(f"已切换到 `{path}`，直接发文件即可。")


async def leaveFolder(_, message: Message):
    """回到根目录"""
    folder.reset()
    await message.reply("已经回到根目录了。")


async def getFolder(_, message: Message):
    """查看当前下载目录"""
    path = folder.getPath()
    await message.reply(f"当前目录是 `{path}`")


async def showQueue(_, message: Message):
    """查看下载队列，可取消任务"""
    text, markup = queueview.render_queue()
    await message.reply(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)


async def pauseQueue(_, message: Message):
    """暂停接收新的下载任务"""
    download_manager.set_paused(True)
    await message.reply("已暂停：排队中的任务不会开始，进行中的会继续下完。发 /resume 恢复。")


async def resumeQueue(_, message: Message):
    """恢复下载队列"""
    download_manager.set_paused(False)
    await message.reply("已恢复，排队中的任务会继续下载。")

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from pathlib import Path

from pyrogram import filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.handlers.message_handler import MessageHandler
from pyrogram.types import Message

from bot.app import ADMINS, BASE_FOLDER, CONFIG_FOLDER, app, user
from bot.download import manager as download_manager
from bot.download.groups import MediaGroupCollector
from bot.download.handler import enqueue_messages
from bot.download.names import media_file_size, sanitize_folder_name
from bot.util import humanReadableSize

# Bot 客户端（MTProto）最多下载 20 MiB；更大的文件必须用用户账号拉取
BOT_DOWNLOAD_LIMIT = 20 * 1024 * 1024
# 每个频道的摘要消息接近该长度就另起新消息，旧的留在会话里
DIGEST_MAX_CHARS = 3500
# 摘要编辑节流：几秒内的多条事件合并成一次编辑
DIGEST_FLUSH_DELAY = 2.0

DEFAULT_TYPES = ["video", "photo", "document", "audio", "animation"]
DEFAULT_AD_KEYWORDS = [
    "推广", "广告", "加微", "微信", "vx", "whatsapp", "购买", "下单", "接单",
    "兼职", "有偿", "福利", "充值", "代理", "招商", "秒杀", "优惠券", "全网最低",
    "上车", "进群", "内部群", "签约", "招募",
]
INVITE_LINK_RE = re.compile(r"t\.me/(?:joinchat/|\+)", re.I)
URL_RE = re.compile(r"https?://", re.I)
EXPLICIT_AD_MARKS = ("#广告", "【广告】", "[广告]", "（广告）", "(广告)")

LISTEN_PATH = Path(CONFIG_FOLDER) / "listening.json"

_lock = threading.Lock()
_config: dict = {"defaults": {}, "chats": {}}
_admin_chat_id: int | None = None
digests: dict[int, "Digest"] = {}
groups = MediaGroupCollector()


class Digest:
    """一个频道一条的累计摘要消息，接近长度上限就另起新消息。"""

    def __init__(self, title: str) -> None:
        self.title = title
        self.message = None
        self.lines: list[str] = []
        self.task: asyncio.Task | None = None

    def render(self) -> str:
        return f"📡 {self.title}（自动下载）\n" + "\n".join(self.lines)


def load_config() -> None:
    global _config
    with _lock:
        try:
            data = json.loads(LISTEN_PATH.read_text(encoding="utf-8")) if LISTEN_PATH.exists() else {}
        except Exception:
            logging.exception("读取监听配置失败，使用默认值")
            data = {}
        defaults = {
            "types": list(DEFAULT_TYPES),
            "min_size_mb": 0,
            "ad_keywords": list(DEFAULT_AD_KEYWORDS),
        }
        defaults.update(data.get("defaults") or {})
        _config = {"defaults": defaults, "chats": data.get("chats") or {}}


def save_config() -> None:
    with _lock:
        tmp = LISTEN_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_config, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(LISTEN_PATH)


def _entry_for(chat_id: int) -> dict | None:
    return _config.get("chats", {}).get(str(chat_id))


def _effective(entry: dict, key: str):
    value = entry.get(key)
    if value:
        return value
    return _config["defaults"].get(key)


# ---------- 管理员会话与摘要 ----------

async def _admin_chat() -> int | None:
    global _admin_chat_id
    if _admin_chat_id is not None:
        return _admin_chat_id
    for token in ADMINS:
        token = (token or "").strip()
        if not token:
            continue
        try:
            target = int(token) if token.isdigit() else (token if token.startswith("@") else f"@{token}")
            chat = await app.get_chat(target)
            _admin_chat_id = chat.id
            logging.warning("频道监听通知发送到管理员会话：%s", chat.id)
            return _admin_chat_id
        except Exception:
            logging.warning("解析管理员会话失败：%s", token)
    return None


def digest_add(chat_id: int, title: str, line: str) -> None:
    d = digests.get(chat_id)
    if d is None:
        d = digests[chat_id] = Digest(title)
    if d.lines and len(d.render()) + len(line) + 1 > DIGEST_MAX_CHARS:
        # 另起新消息；旧消息保留在会话里作为历史
        d.lines = []
        d.message = None
    d.lines.append(line)
    if d.task is None or d.task.done():
        d.task = asyncio.create_task(_flush_digest(d))


async def _flush_digest(d: Digest) -> None:
    await asyncio.sleep(DIGEST_FLUSH_DELAY)
    if not d.lines:
        return
    text = d.render()
    try:
        if d.message is None:
            admin = await _admin_chat()
            if admin is None:
                logging.warning("频道摘要无法发送：管理员会话不可用")
                return
            d.message = await app.send_message(admin, text, parse_mode=ParseMode.DISABLED)
        else:
            await d.message.edit(text, parse_mode=ParseMode.DISABLED)
    except Exception as exc:
        logging.debug("更新频道摘要失败：%s: %s", type(exc).__name__, exc)


# ---------- 广告过滤 ----------

def looks_like_ad(message: Message, keywords: list[str]) -> str | None:
    """命中返回原因，未命中返回 None。多层条件组合判定，降低误杀。"""
    text = message.caption or message.text or ""
    if not text:
        return None
    if any(mark in text for mark in EXPLICIT_AD_MARKS):
        return "带广告标记"
    lower = text.lower()
    markers = [kw for kw in keywords if kw and kw.lower() in lower]
    invites = len(INVITE_LINK_RE.findall(text))
    links = len(URL_RE.findall(text))
    if invites and markers:
        return "含邀请链接和推广词"
    if len(markers) >= 3:
        return f"推广词命中 {len(markers)} 个"
    if links >= 3 and markers:
        return "多个外链 + 推广词"
    return None


# ---------- 频道帖处理 ----------

async def _resolve_group(messages: list[Message], chat_id: int, title: str):
    """超过机器人 20MiB 限制时改用用户账号重新取媒体。返回 (client, messages)。"""
    if all(media_file_size(m) <= BOT_DOWNLOAD_LIMIT for m in messages):
        return app, messages
    if user is None:
        digest_add(chat_id, title, "⏭️ 跳过：文件超过机器人 20 MiB 限制，且未配置用户账号")
        return None, None
    try:
        first = messages[0]
        if len(messages) > 1:
            fetched = await user.get_media_group(chat_id, first.id)
        else:
            fetched = await user.get_messages(chat_id, [first.id])
        fetched = [m for m in (fetched or []) if m is not None and m.media]
        if fetched:
            return user, fetched
    except Exception:
        logging.exception("用用户账号读取频道媒体失败：%s", title)
    digest_add(chat_id, title, "⏭️ 跳过：用户账号无法读取该频道的媒体")
    return None, None


async def _enqueue_channel_media(
    items: list[Message], chat_id: int, title: str, base_dir: str, admin: int
) -> None:
    client, resolved = await _resolve_group(items, chat_id, title)
    if client is None:
        return
    await enqueue_messages(
        resolved, client, base=base_dir, quiet=True, notice_chat_id=admin
    )


async def on_channel_post(_, message: Message) -> None:
    try:
        await _handle_post(message)
    except Exception:
        logging.exception("处理频道帖子失败：%s", getattr(message, "id", None))


async def _handle_post(message: Message) -> None:
    chat = message.chat
    if chat is None or not message.media:
        return
    entry = _entry_for(chat.id)
    if entry is None:
        return
    title = entry.get("title") or chat.title or chat.username or str(chat.id)
    kind = getattr(message.media, "value", None)
    size = media_file_size(message)

    types = _effective(entry, "types") or DEFAULT_TYPES
    if kind not in types:
        digest_add(chat.id, title, f"⏭️ 跳过：类型 {kind} 不在白名单")
        logging.info("频道监听跳过（类型）：%s %s", title, kind)
        return
    min_mb = float(_effective(entry, "min_size_mb") or 0)
    if size and size < min_mb * 1024 * 1024:
        digest_add(chat.id, title, f"⏭️ 跳过：大小 {humanReadableSize(size)} 低于下限 {min_mb:g} MiB")
        logging.info("频道监听跳过（大小）：%s %s", title, kind)
        return
    reason = looks_like_ad(message, _effective(entry, "ad_keywords") or DEFAULT_AD_KEYWORDS)
    if reason:
        digest_add(chat.id, title, f"⏭️ 跳过疑似广告（{reason}）")
        logging.warning("频道监听跳过疑似广告：%s %s", title, reason)
        return

    admin = await _admin_chat()
    if admin is None:
        logging.warning("频道监听未生效：无法解析管理员会话，请检查 ADMINS 配置")
        return
    folder_name = entry.get("folder") or sanitize_folder_name(title) or f"channel_{chat.id}"
    base_dir = str(Path(BASE_FOLDER) / folder_name)
    try:
        Path(base_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.exception("创建频道文件夹失败：%s", base_dir)
        return

    def make_on_ready(chat_id=chat.id, title=title, base_dir=base_dir, admin=admin):
        async def on_ready(items, notice) -> None:
            try:
                items = [m for m in items if m is not None and m.media]
                if not items:
                    return
                await _enqueue_channel_media(items, chat_id, title, base_dir, admin)
            except Exception:
                logging.exception("处理频道相册失败：%s", title)
        return on_ready

    consumed = await groups.add(message, app, make_on_ready(), quiet=True)
    if not consumed:
        await _enqueue_channel_media([message], chat.id, title, base_dir, admin)


# ---------- 下载事件进摘要 ----------

def on_download_event(kind: str, info: dict) -> None:
    if not info.get("quiet"):
        return
    chat_id = info.get("chat_id")
    if chat_id is None:
        return
    entry = _entry_for(chat_id)
    if entry is None:
        return
    title = entry.get("title") or str(chat_id)
    filename = info.get("filename") or "未知文件"
    if kind == "done":
        size_text = f"（{humanReadableSize(info['size'])}）" if info.get("size") else ""
        line = f"✅ {filename}{size_text}完成"
    elif kind == "failed":
        note = f"（{info['note']}）" if info.get("note") else ""
        line = f"❌ {filename} 下载失败{note}"
    elif kind == "stopped":
        line = f"⏹ {filename} 已停止"
    elif kind == "skipped":
        line = f"⏭️ {filename}：{info.get('reason') or '跳过'}"
    elif kind == "duplicate_keep":
        line = f"⚠️ {filename} 与历史内容相同，已保留"
    else:
        return
    digest_add(chat_id, title, line)


# ---------- 命令 ----------

async def listen(_, message: Message):
    """监听频道，新帖自动下载：/listen <链接或@用户名>"""
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(
            "用法：`/listen <频道链接或@用户名>`\n"
            "机器人需要已加入该频道；下载超过 20MiB 的文件需要用户账号也加入。",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        chat = await app.get_chat(parts[1].strip())
    except Exception:
        logging.exception("解析频道失败：%s", parts[1])
        await message.reply("找不到这个频道：确认链接或用户名正确，且机器人已加入该频道。")
        return
    if chat.type != ChatType.CHANNEL:
        await message.reply("目前只支持监听频道（channel），群组暂不支持。")
        return
    title = chat.title or chat.username or str(chat.id)
    folder_name = sanitize_folder_name(title) or f"channel_{chat.id}"
    with _lock:
        _config["chats"][str(chat.id)] = {"title": title, "folder": folder_name}
        save_config()
    try:
        Path(BASE_FOLDER, folder_name).mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.exception("创建频道文件夹失败：%s", folder_name)
    await message.reply(
        f"已开始监听 `{title}`：新帖子自动下载到文件夹 `{folder_name}/`。\n"
        "超过 20MiB 的文件会用用户账号下载，请确保用户账号已加入该频道。",
        parse_mode=ParseMode.MARKDOWN,
    )


async def unlisten(_, message: Message):
    """停止监听频道：/unlisten <链接或@用户名>"""
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply("用法：`/unlisten <频道链接或@用户名>`", parse_mode=ParseMode.MARKDOWN)
        return
    target = parts[1].strip()
    removed = None
    try:
        chat = await app.get_chat(target)
        removed = _config.get("chats", {}).pop(str(chat.id), None)
    except Exception:
        logging.debug("unlisten 解析频道失败：%s", target, exc_info=True)
    if removed is None:
        token = target.lstrip("@")
        for key, entry in list(_config.get("chats", {}).items()):
            if token in (key, entry.get("folder") or "", entry.get("title") or ""):
                removed = _config["chats"].pop(key)
                break
    if removed is None:
        await message.reply("这个频道不在监听列表里。")
        return
    save_config()
    await message.reply(
        f"已停止监听 `{removed.get('title') or removed.get('folder')}`。",
        parse_mode=ParseMode.MARKDOWN,
    )


async def listening(_, message: Message):
    """查看正在监听的频道"""
    chats = _config.get("chats", {})
    if not chats:
        await message.reply("还没有监听任何频道，用 /listen 添加。")
        return
    lines = ["📡 正在监听的频道："]
    for entry in chats.values():
        lines.append(f"• {entry.get('title') or entry.get('folder')} → `{entry.get('folder')}/`")
    await message.reply("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def register(client) -> None:
    load_config()
    # 必须注册在 addFile 的媒体处理器之前：同一组内先匹配的先执行，
    # 频道帖由这里接管，避免 addFile 往频道里回「你不是管理员」。
    client.add_handler(MessageHandler(on_channel_post, filters.channel))
    download_manager.download_event_listeners.append(on_download_event)
    logging.warning("频道监听已就绪：%d 个频道", len(_config.get("chats", {})))

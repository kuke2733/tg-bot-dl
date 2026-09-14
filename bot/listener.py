from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from functools import lru_cache
from pathlib import Path

from pyrogram import filters
from pyrogram.enums import ChatType, ParseMode
from pyrogram.handlers.message_handler import MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import callbacks
from bot.app import ADMINS, BASE_FOLDER, CONFIG_FOLDER, app, user
from bot.download.groups import MediaGroupCollector
from bot.download.handler import enqueue_messages
from bot.download.state import download_event_listeners as _dl_event_listeners
from bot.tg_io import Kind, safe_edit, safe_reply, safe_send
from bot.download.names import media_file_size, message_media, sanitize_folder_name
from bot import util
from bot.util import clip_button_text, humanReadableSize

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

# 广告正则规则，转换自 AzurLab/Tg-Ad-RegEx（MIT）并做过调优，直接在这里或
# config/listening.json 编辑即可。主体词×修饰词组合命中才算广告，比单关键词误杀率低。
DEFAULT_AD_REGEX = {
    "combo_part1": "推广|引流|拉[人群裙]|[群裙]发|小群|[吸增加]粉|水军|活跃|(wz|僵尸|精准)粉|(批量小|非小|[买卖]|微信|QQ|支付宝|钉钉|公[众主]|陌陌|探探|红[书薯]|抖音|soul|灵魂)号|好友提取|企业微信|电[商销]|实名一条龙|股民|[博菠][彩菜]|盘口|亚博|棋牌|狗推|人事|兼职|招([人聘募]|代理)|[黑灰]产|件套|(黑|银行|信用)卡|对公|[账公个私开]户|卡[商王]|假钞|代[收付发]|洗[钱米]|漂白|资金|转账|usdt|[回收出](大量?)u|u商|套现|换汇|贷款|网贷|送货|跑分|社工|数据库|查[询人车房户档]|开房|定位|监听|人[鬼轨]|(苹果|iOS|企业)签名|火种|蓝标|(代)注册|莆田|手表|高仿|[一1][比对][一1]|水鬼|[赚搞][钱米]|租房|房源|暗网|共济会|军火|偷渡|暗杀|走私|毒品",
    "combo_part2": "售|卖|收|详[情询]|面交|需要|有意|咨询|合作|[点加看找]我|点头像|dd|滴滴|联系|(?<![公隐])私(?![人服])|[v薇]信|[加\\+][vq薇]|真[人实]|实名|认证|筛选|解封|担保|专[业供]|精准|实力|高端|正规|独家|全球|全国同台|供应|大量|批发|[现有][货🔥]|货源|直出|安排|(?<![这那])个人|团队|工作室|[卡咔]接|[无免]押|押金|点位|[0-9冰]点|高价|免费(测试|试用)|出水|全新|原装|[一二三]手",
    "independent": "^(dd|滴滴|签到)$|小哥哥.*在(不在|吗)|需要.*联系|[v薇]信|^加v|水路.*往返",
    "suffix": "bat|cab|cmd|com|cpl|lzh|scr|uue|vbs|exe|msi|jar|lnk|ps1|hta|vbe|wsf|js",
    "filename": "诈骗|菲律宾|柬埔寨|金边|缅甸|老挝|越南|印[度尼]|迪拜|自动跳转|视 *频|免 *费|下 *载|跳 *转|点 *击|打 *开|观 *看|片 *源|流畅|[精极] *品|进 *群|啪|开房|土 *豪|无 *套|内 *射|肛 *交|灌 *肠|鲍 *鱼|尤 *物|母 *狗|骚|嫩|SM|[幼呦] *童|母 *子|人 *兽|学 *生|监控|记录|拍下|现场|[全过]程|经过|详情|公安|警方|开枪|击毙|案件|证据|残忍|虐待|血腥|曝光|流出|公布|[男女]子|美女|夫妇|身份|身亡|死者|名单"
}

INVITE_LINK_RE = re.compile(r"t\.me/(?:joinchat/|\+)", re.I)
URL_RE = re.compile(r"https?://", re.I)
EXPLICIT_AD_MARKS = ("#广告", "【广告】", "[广告]", "（广告）", "(广告)")

LISTEN_PATH = Path(CONFIG_FOLDER) / "listening.json"

_lock = threading.Lock()
_config: dict = {"defaults": {}, "chats": {}}
digests: dict[int, "Digest"] = {}
groups = MediaGroupCollector()
# 每个频道的历史回填任务，删除监听频道文件夹时用于中止回填
_backfill_tasks: dict[int, asyncio.Task] = {}


class Digest:
    """一个频道一条的累计摘要消息，接近长度上限就另起新消息。"""

    def __init__(self, title: str) -> None:
        self.title = title
        self.message = None
        self.lines: list[str] = []
        self.task: asyncio.Task | None = None

    def render(self) -> str:
        return f"📡 {self.title} · 自动下载\n" + "\n".join(self.lines)


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
            # 历史回填条数：不填/0 = 全部，-1 = 关闭回填，N = 最近 N 条
            "history_limit": 0,
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
            admin = await util.admin_chat()
            if admin is None:
                logging.warning("频道摘要无法发送：管理员会话不可用")
                return
            d.message = await safe_send(
                admin, text, important=True, parse_mode=ParseMode.DISABLED
            )
        else:
            await safe_edit(
                d.message, text, kind=Kind.NORMAL, important=True, parse_mode=ParseMode.DISABLED
            )
    except Exception as exc:
        logging.debug("更新频道摘要失败：%s: %s", type(exc).__name__, exc)


# ---------- 广告过滤 ----------

def _media_file_name(message: Message) -> str:
    media, _ = message_media(message)
    return getattr(media, "file_name", "") or ""


@lru_cache(maxsize=8)
def _compiled_ad_regex(combo_part1: str, combo_part2: str, independent: str, suffix: str, filename: str):
    combo = None
    if combo_part1 and combo_part2:
        combo = re.compile(
            f"(?i:({combo_part1}).*({combo_part2})|({combo_part2}).*({combo_part1})|({independent}))"
        )
    suffix_re = re.compile(f"(?i:\\.({suffix})$)") if suffix else None
    name_re = re.compile(f"(?i:{filename})") if filename else None
    return combo, suffix_re, name_re


def looks_like_ad(message: Message, keywords: list[str], rules: dict | None = None) -> str | None:
    """命中返回原因，未命中返回 None。多层条件组合判定，降低误杀。

    判定顺序：显式广告标记 → 上游组合规则，主体词×修饰词 → 可疑文件 → 关键词计数。
    """
    rules = rules or DEFAULT_AD_REGEX
    fname = _media_file_name(message)
    combo_re, suffix_re, name_re = _compiled_ad_regex(
        rules.get("combo_part1", ""),
        rules.get("combo_part2", ""),
        rules.get("independent", ""),
        rules.get("suffix", ""),
        rules.get("filename", ""),
    )
    if combo_re is not None:
        text0 = message.caption or message.text or ""
        m = combo_re.search(text0) if text0 else None
        if m:
            hit = m.group(0)[:40].replace("\n", " ")
            return "命中广告组合规则：" + hit
    if suffix_re is not None and fname and suffix_re.search(fname):
        return "可疑文件后缀：" + fname[:60]
    if name_re is not None and fname and name_re.search(fname):
        return "可疑文件名：" + fname[:60]

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


# 私有频道的 t.me/c/<id>/<msg> 消息链接：转成数字 id 再解析，get_chat 会有网络兜底
PRIVATE_CHANNEL_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?t(?:elegram)?\.(?:me|org|dog)/c/(\d+)(?:/\d+)?/?$", re.I
)


def parse_listen_target(raw: str) -> str | int:
    """/listen、/unlisten 的目标都接受：频道链接、邀请链接、@用户名、裸用户名，公开私有都行。"""
    text = (raw or "").strip().strip("<>")
    match = PRIVATE_CHANNEL_LINK_RE.match(text)
    if match:
        return int(f"-100{match.group(1)}")
    return text


# ---------- 频道帖处理 ----------

async def _enqueue_channel_media(
    items: list[Message], chat_id: int, title: str, base_dir: str, admin: int
) -> None:
    # 收到的消息来自用户会话，直接由用户账号下载，没有 20MiB 限制
    await enqueue_messages(
        items, user, base=base_dir, quiet=True, notice_chat_id=admin
    )


async def on_channel_post(_, message: Message) -> None:
    logging.debug(
        "用户账号收到频道更新：chat=%s id=%s media=%s",
        getattr(message.chat, "id", None),
        message.id,
        getattr(message.media, "value", None),
    )
    try:
        await _handle_post(message)
    except Exception:
        logging.exception("处理频道帖子失败：%s", getattr(message, "id", None))


async def silence_channel_post(_, message: Message) -> None:
    """机器人侧吞掉频道帖：防止 addFile 把频道新帖当普通文件下载、往频道里回消息。
    真正的监听收帖在用户账号侧，见 register_user_channel_handler。"""


async def _handle_post(message: Message) -> None:
    chat = message.chat
    if chat is None or not message.media:
        return
    entry = _entry_for(chat.id)
    if entry is None:
        logging.debug("频道帖不在监听列表，忽略：chat=%s id=%s", chat.id, message.id)
        return
    logging.info(
        "收到监听频道新帖：%s id=%s", entry.get("title") or chat.id, message.id
    )
    await _process_channel_post(message, entry)


async def _process_channel_post(message: Message, entry: dict) -> None:
    """过滤并入队一条频道媒体帖，实时新帖与历史回填共用。"""
    chat = message.chat
    chat_id = chat.id
    title = entry.get("title") or chat.title or chat.username or str(chat_id)
    kind = getattr(message.media, "value", None)
    size = media_file_size(message)

    types = _effective(entry, "types") or DEFAULT_TYPES
    if kind not in types:
        digest_add(chat.id, title, f"⏭️ 跳过：类型 {kind} 不在白名单")
        logging.info("频道监听跳过：类型过滤 %s %s", title, kind)
        return
    min_mb = float(_effective(entry, "min_size_mb") or 0)
    if size and size < min_mb * 1024 * 1024:
        digest_add(chat.id, title, f"⏭️ 跳过：大小 {humanReadableSize(size)} 低于下限 {min_mb:g} MiB")
        logging.info("频道监听跳过：大小过滤 %s %s", title, kind)
        return
    reason = looks_like_ad(
        message,
        _effective(entry, "ad_keywords") or DEFAULT_AD_KEYWORDS,
        _effective(entry, "ad_regex") or DEFAULT_AD_REGEX,
    )
    if reason:
        digest_add(chat.id, title, f"⏭️ 跳过疑似广告：{reason}")
        logging.info("频道监听跳过疑似广告：%s %s", title, reason)
        return

    admin = await util.admin_chat()
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

    def make_on_ready(chat_id=chat_id, title=title, base_dir=base_dir, admin=admin):
        async def on_ready(items, notice) -> None:
            try:
                items = [m for m in items if m is not None and m.media]
                if not items:
                    return
                await _enqueue_channel_media(items, chat_id, title, base_dir, admin)
            except Exception:
                logging.exception("处理频道相册失败：%s", title)
        return on_ready

    consumed = await groups.add(message, user, make_on_ready(), quiet=True)
    if not consumed:
        await _enqueue_channel_media([message], chat_id, title, base_dir, admin)


def cancel_backfill(chat_id: int) -> None:
    """取消该频道的历史回填任务，删除监听频道文件夹时调用。"""
    task = _backfill_tasks.pop(chat_id, None)
    if task is not None and not task.done():
        task.cancel()
        logging.info("已中止历史回填：%s", chat_id)


def cancel_backfill_by_folder(folder: str) -> None:
    for chat_id, entry in list(_config.get("chats", {}).items()):
        if entry.get("folder") == folder:
            cancel_backfill(int(chat_id))
            break


async def _backfill_history(chat_id: int, entry: dict, limit: int) -> None:
    """监听建立后回填频道的历史媒体帖，limit=0 全部、负数关闭，
    从最早的一条开始按顺序下载，走用户账号。"""
    if user is None or limit < 0:
        return
    title = entry.get("title") or str(chat_id)
    try:
        buffered = []
        async for message in user.get_chat_history(chat_id, limit=limit):
            if message and message.media:
                buffered.append(message)
    except Exception:
        logging.exception("回填历史消息失败：%s", chat_id)
        digest_add(chat_id, title, "⏭️ 历史回填失败：无法读取频道历史")
        return
    buffered.reverse()  # get_chat_history 从新到旧，倒过来按时间正序下载
    logging.info("历史回填：%s 扫描 %d 条消息，其中 %d 条媒体待处理", title, limit, len(buffered))
    digest_add(chat_id, title, f"📜 开始回填历史：{len(buffered)} 条媒体帖")
    for message in buffered:
        if _entry_for(chat_id) is None:
            # 中途被 /unlisten：停止回填
            logging.info("频道已停止监听，历史回填中止：%s", title)
            return
        try:
            await _process_channel_post(message, entry)
        except Exception:
            logging.exception("处理历史帖失败：%s", getattr(message, "id", None))
    digest_add(chat_id, title, "📜 历史回填完成")


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
    """监听频道，自动下载新帖并回填全部历史：/listen <链接> [历史条数]
    频道需用户账号加入；条数不填为全部。"""
    parts = (message.text or "").split()
    if len(parts) < 2:
        await safe_reply(message, 
            "用法：`/listen <频道链接或@用户名> [历史条数]`\n"
            "历史条数不填默认回填全部历史。",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    # 监听与下载都走用户账号：账号必须加入频道，公开私有一样，机器人无需加入
    if user is None:
        await safe_reply(message, "需先配置用户账号 PHONE_NUMBER。")
        return
    target = parse_listen_target(parts[1])

    async def resolve():
        try:
            return await user.get_chat(target)
        except Exception:
            logging.debug("用户账号解析频道失败：%s", target, exc_info=True)
            return None

    chat = await resolve()
    if chat is None:
        await safe_reply(message, "找不到这个频道，或用户账号未加入。")
        return
    if chat.type != ChatType.CHANNEL:
        await safe_reply(message, "群组暂不支持。")
        return
    try:
        # get_chat_history 是异步生成器；能迭代就说明账号在频道里，哪怕频道为空没有消息
        async for _ in user.get_chat_history(chat.id, limit=1):
            break
    except Exception:
        logging.warning("用户账号无法读取频道消息：%s", chat.id, exc_info=True)
        await safe_reply(message, "用户账号未加入该频道。")
        return
    title = chat.title or chat.username or str(chat.id)
    folder_name = sanitize_folder_name(title) or f"channel_{chat.id}"
    # 存一个可点击的频道链接，优先级：用户名链接 > 频道完整信息里的邀请链接，仅管理员可见
    # > 用户监听时自己提供的邀请链接；普通成员看不到频道的邀请链接，但他们手里有加入时用的那个
    if chat.username:
        link = f"https://t.me/{chat.username}"
    elif chat.invite_link:
        link = chat.invite_link
    elif isinstance(target, str) and INVITE_LINK_RE.search(target):
        link = target if target.startswith("http") else f"https://{target}"
    else:
        link = ""
    with _lock:
        _config["chats"][str(chat.id)] = {
            "title": title, "folder": folder_name, "link": link,
        }
    # save_config 自己会拿锁：在锁内调用会同线程重复加锁，直接死锁整个事件循环
    save_config()
    try:
        Path(BASE_FOLDER, folder_name).mkdir(parents=True, exist_ok=True)
    except OSError:
        logging.exception("创建频道文件夹失败：%s", folder_name)
    entry = _config["chats"][str(chat.id)]
    limit = _effective(entry, "history_limit")
    try:
        limit = int(limit if limit is not None else 0)
    except (TypeError, ValueError):
        limit = 0
    if len(parts) > 2 and parts[2].lstrip("-").isdigit():
        limit = int(parts[2])
    backfill_note = ""
    if limit < 0:
        backfill_note = "\n📜 历史回填：已关闭。"
    elif limit > 0:
        backfill_note = f"\n📜 历史回填：正在下载最近 {limit} 条消息。"
        task = asyncio.create_task(_backfill_history(chat.id, entry, limit=limit))
        _backfill_tasks[chat.id] = task
        task.add_done_callback(lambda t, cid=chat.id: _backfill_tasks.pop(cid, None))
    else:
        backfill_note = "\n📜 历史回填：正在下载频道全部历史。"
        task = asyncio.create_task(_backfill_history(chat.id, entry, limit=0))
        _backfill_tasks[chat.id] = task
        task.add_done_callback(lambda t, cid=chat.id: _backfill_tasks.pop(cid, None))
    reply_text = f"已开始监听 `{title}`：新帖子自动下载到文件夹 `{folder_name}/`。"
    if backfill_note:
        reply_text += backfill_note
    await safe_reply(message, reply_text, parse_mode=ParseMode.MARKDOWN)


async def _migrate_links() -> None:
    """给旧版监听条目补存频道链接，/listen 存链接是后来加的功能。
    统一用用户账号解析，收帖本就在用户会话，失败下次再试。"""
    changed = False
    if user is None:
        return
    for chat_id, entry in list(_config.get("chats", {}).items()):
        if entry.get("link"):
            continue
        try:
            chat = await user.get_chat(int(chat_id))
        except Exception:
            logging.debug("补频道链接失败：%s", chat_id, exc_info=True)
            continue
        if chat.username:
            entry["link"] = f"https://t.me/{chat.username}"
        elif chat.invite_link:
            entry["link"] = chat.invite_link
        else:
            continue
        changed = True
        logging.info("已补存频道链接：%s -> %s", entry.get("title"), entry["link"])
    if changed:
        save_config()


def _render_listening():
    """渲染 /listening 面板：文本列表，每频道一行按钮，跳转和取消监听。"""
    chats = _config.get("chats", {})
    if not chats:
        return "还没有监听任何频道，用 /listen 添加。", None
    lines = ["📡 正在监听的频道："]
    rows = []
    for chat_id, entry in chats.items():
        title = entry.get("title") or entry.get("folder") or chat_id
        lines.append(f"• {title} → `{entry.get('folder')}/`")
        row = []
        link = entry.get("link")
        if link:
            row.append(InlineKeyboardButton(clip_button_text(title, 20), url=link))
        cancel_text = (
            "🚫 取消监听" if link else f"🚫 取消监听 {clip_button_text(title, 16)}"
        )
        row.append(
            InlineKeyboardButton(cancel_text, callback_data=f"unlisten {chat_id}")
        )
        rows.append(row)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def listening(_, message: Message):
    """查看正在监听的频道"""
    await _migrate_links()
    text, markup = _render_listening()
    await safe_reply(message, text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)


async def handle_unlisten_callback(callback: CallbackQuery) -> None:
    """监听面板上的「取消监听」按钮：直接移除该频道并刷新面板。"""
    message = callback.message
    chat_id = (callback.data or "").split(" ", 1)[-1]
    entry = _config.get("chats", {}).pop(chat_id, None)
    if entry is None:
        await callback.answer("该频道已不在监听列表")
    else:
        save_config()
        title = entry.get("title") or entry.get("folder") or chat_id
        logging.info("已通过监听面板取消监听：%s", title)
        await callback.answer(f"已停止监听：{title}")
    if message is None:
        return
    text, markup = _render_listening()
    try:
        await safe_edit(message, text, kind=Kind.NORMAL, important=True, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        logging.debug("更新监听面板失败：%s: %s", type(exc).__name__, exc)


def register(client) -> None:
    load_config()
    # 机器人侧只"吞掉"频道帖：避免 addFile 把频道新帖当普通文件下载、往频道里回消息。
    # 真正的收帖在用户账号侧，见 register_user_channel_handler。
    client.add_handler(MessageHandler(silence_channel_post, filters.channel))
    _dl_event_listeners.append(on_download_event)
    logging.info("频道监听已就绪：%d 个频道，收帖走用户账号", len(_config.get("chats", {})))


def register_user_channel_handler(client) -> None:
    """用户账号侧的频道监听：唯一的收帖路径，机器人无需加入任何频道。"""
    client.add_handler(MessageHandler(on_channel_post, filters.channel))
    logging.info("用户账号频道监听已就绪")


# —— 按钮回调注册，协议前缀与路由见 bot/callbacks.py ——
callbacks.on(callbacks.UNLISTEN)(handle_unlisten_callback)

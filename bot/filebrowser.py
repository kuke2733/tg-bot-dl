from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from pyrogram.client import Client
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.app import BASE_FOLDER
from bot.download.manager import path_in_use
from bot.util import clip_button_text, humanReadableSize

# 一次最多展示的条目数量，避免消息和按钮超出 Telegram 限制
MAX_ENTRIES = 60
# 最多记住的浏览页面数量
MAX_VIEWS = 100
# 按钮文字最大长度
BUTTON_TEXT_LIMIT = 20

views: dict[int, FilesView] = {}


@dataclass
class FilesView:
    rel_dir: str = ""
    # 和消息里的按钮一一对应：(名称, 是否目录)
    entries: list[tuple[str, bool]] = field(default_factory=list)

    def abs_dir(self) -> Path:
        if not self.rel_dir:
            return Path(BASE_FOLDER)
        return Path(BASE_FOLDER) / self.rel_dir

    def display_path(self) -> str:
        return f"/{self.rel_dir}" if self.rel_dir else "/"


def normalize_rel_dir(rel_dir: str) -> str:
    """清理路径并逐级回退，确保最终目录真实存在。"""
    parts = [
        part
        for part in rel_dir.replace("\\", "/").split("/")
        if part and part not in (".", "..")
    ]
    rel_dir = "/".join(parts)
    while rel_dir and not (Path(BASE_FOLDER) / rel_dir).is_dir():
        rel_dir = rel_dir.rsplit("/", 1)[0] if "/" in rel_dir else ""
    return rel_dir


def list_entries(directory: Path) -> list[tuple[str, bool]]:
    try:
        children = list(directory.iterdir())
    except OSError as exc:
        logging.warning("读取目录失败：%s %s", directory, exc)
        return []
    entries: list[tuple[str, bool]] = []
    for child in children:
        try:
            entries.append((child.name, child.is_dir()))
        except OSError:
            continue
    entries.sort(key=lambda item: (not item[1], item[0].lower()))
    return entries


def save_view(message_id: int, view: FilesView) -> None:
    views.pop(message_id, None)
    views[message_id] = view
    while len(views) > MAX_VIEWS:
        views.pop(next(iter(views)))


def make_listing_view(rel_dir: str) -> tuple[FilesView, str, InlineKeyboardMarkup]:
    rel_dir = normalize_rel_dir(rel_dir)
    directory = Path(BASE_FOLDER) / rel_dir if rel_dir else Path(BASE_FOLDER)
    view = FilesView(rel_dir=rel_dir, entries=list_entries(directory))

    entries = view.entries
    shown = entries[:MAX_ENTRIES]
    dirs = sum(1 for _, is_dir in entries if is_dir)
    files = len(entries) - dirs

    lines = [f"📂 当前目录：{view.display_path()}"]
    if not entries:
        lines.append("这个目录是空的。")
    else:
        lines.append(f"共 {len(entries)} 项：{dirs} 个目录，{files} 个文件。")
        if len(entries) > MAX_ENTRIES:
            lines.append(f"只显示前 {MAX_ENTRIES} 项。")

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, (name, is_dir) in enumerate(shown):
        icon = "📁" if is_dir else "📄"
        action = "fdir" if is_dir else "ffile"
        row.append(
            InlineKeyboardButton(
                f"{icon} {clip_button_text(name, BUTTON_TEXT_LIMIT)}",
                callback_data=f"{action} {index}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav = []
    if view.rel_dir:
        nav.append(InlineKeyboardButton("⬅️ 上一级", callback_data="fup"))
    nav.append(InlineKeyboardButton("🔄 刷新", callback_data="fref"))
    rows.append(nav)
    return view, "\n".join(lines), InlineKeyboardMarkup(rows)


def build_file_detail(view: FilesView, index: int) -> tuple[str, InlineKeyboardMarkup] | None:
    if not (0 <= index < len(view.entries)) or view.entries[index][1]:
        return None
    name = view.entries[index][0]
    lines = [f"📄 {name}"]
    try:
        stat = (view.abs_dir() / name).stat()
        lines.append(f"大小：{humanReadableSize(stat.st_size)}")
        lines.append(
            "修改时间：" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
        )
    except OSError:
        lines.append("（读取文件信息失败）")
    lines.append(f"位置：{view.display_path()}")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 删除此文件", callback_data=f"fdel {index}")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="fref")],
        ]
    )
    return "\n".join(lines), keyboard


def build_delete_confirm(view: FilesView, index: int) -> tuple[str, InlineKeyboardMarkup] | None:
    if not (0 <= index < len(view.entries)) or view.entries[index][1]:
        return None
    name = view.entries[index][0]
    text = f"⚠️ 确认删除文件「{name}」？\n删除后无法恢复。"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 确认删除", callback_data=f"fdok {index}"),
                InlineKeyboardButton("❌ 取消", callback_data="fref"),
            ]
        ]
    )
    return text, keyboard


async def edit_view(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit(text, reply_markup=markup, parse_mode=ParseMode.DISABLED)
    except Exception as exc:
        logging.debug("更新文件管理消息失败：%s: %s", type(exc).__name__, exc)


async def show_listing(message: Message, rel_dir: str) -> None:
    view, text, markup = make_listing_view(rel_dir)
    save_view(message.id, view)
    await edit_view(message, text, markup)


async def delete_entry(
    callback: CallbackQuery, message: Message, view: FilesView, index: int
) -> None:
    if not (0 <= index < len(view.entries)) or view.entries[index][1]:
        await callback.answer("该文件已不存在")
        await show_listing(message, view.rel_dir)
        return
    name = view.entries[index][0]
    rel_path = f"{view.rel_dir}/{name}" if view.rel_dir else name
    if path_in_use(rel_path):
        await callback.answer("这个文件正在下载中，请先取消下载。")
        await show_listing(message, view.rel_dir)
        return
    target = Path(BASE_FOLDER) / rel_path
    try:
        target.unlink()
    except OSError as exc:
        logging.warning("删除文件失败：%s %s", target, exc)
        await callback.answer("删除失败，文件可能被其他程序占用。")
        await show_listing(message, view.rel_dir)
        return
    logging.warning("已通过 /files 删除文件：%s", rel_path)
    await callback.answer("已删除")
    await show_listing(message, view.rel_dir)


async def handle_files_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    message = callback.message
    if message is None:
        await callback.answer("这条消息已失效，请重新发送 /files")
        return
    view = views.get(message.id)
    if view is None:
        await callback.answer("状态已失效，请重新发送 /files")
        await edit_view(message, "状态已失效，请重新发送 /files。", None)
        return

    if data == "fup":
        await callback.answer()
        parent = view.rel_dir.rsplit("/", 1)[0] if "/" in view.rel_dir else ""
        await show_listing(message, parent)
        return

    if data == "fref":
        await callback.answer("已刷新")
        await show_listing(message, view.rel_dir)
        return

    action, _, arg = data.partition(" ")
    index = int(arg) if arg.isdigit() else -1

    if action == "fdir":
        if 0 <= index < len(view.entries) and view.entries[index][1]:
            name = view.entries[index][0]
            child = f"{view.rel_dir}/{name}" if view.rel_dir else name
            await callback.answer()
            await show_listing(message, child)
        else:
            await callback.answer("该目录已不存在")
            await show_listing(message, view.rel_dir)
        return

    if action == "ffile":
        detail = build_file_detail(view, index)
        if detail is None:
            await callback.answer("该文件已不存在")
            await show_listing(message, view.rel_dir)
            return
        await callback.answer()
        await edit_view(message, *detail)
        return

    if action == "fdel":
        confirm = build_delete_confirm(view, index)
        if confirm is None:
            await callback.answer("该文件已不存在")
            await show_listing(message, view.rel_dir)
            return
        await callback.answer()
        await edit_view(message, *confirm)
        return

    if action == "fdok":
        await delete_entry(callback, message, view, index)
        return

    await callback.answer()


async def listFiles(client: Client, message: Message):
    """浏览和删除已下载的文件"""
    view, text, markup = make_listing_view("")
    sent = await message.reply(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.DISABLED,
        disable_web_page_preview=True,
    )
    save_view(sent.id, view)

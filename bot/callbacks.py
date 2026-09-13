"""按钮回调协议：前缀常量与注册制路由。

各功能模块在导入时用 `on(前缀)` 注册自己的处理器，dispatch 按
前缀长度从长到短匹配（保证 "f" 这类单字前缀不会抢先吞掉长前缀）。
注册方与按钮生成方必须使用同一组常量，避免两端各写一份字符串。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pyrogram.types import CallbackQuery

# 回调数据前缀
Q_STOP_BATCH = "qstopb "
Q_STOP = "qstop "
Q_GOTO = "qgoto "
Q_REFRESH = "qref"
Q_CANCEL_ALL = "qcancelall"
STOP_BATCH = "stopb "
STOP = "stop "
DUP_CONTINUE = "dupc "
DUP_SKIP = "dupx "
BATCH_DUP_CONTINUE = "bdupc "
BATCH_DUP_SKIP = "bdupx "
HASH_KEEP = "hkeep "
HASH_DELETE = "hdel "
UNLISTEN = "unlisten "
MENU = "menu "
FILES = "f"

Handler = Callable[[CallbackQuery], Awaitable[None]]
_routes: list[tuple[str, Handler]] = []


def register(prefix: str, handler: Handler) -> None:
    _routes.append((prefix, handler))
    _routes.sort(key=lambda route: len(route[0]), reverse=True)


def on(prefix: str) -> Callable[[Handler], Handler]:
    def deco(handler: Handler) -> Handler:
        register(prefix, handler)
        return handler

    return deco


async def dispatch(_, callback: CallbackQuery) -> None:
    data = callback.data or ""
    for prefix, handler in _routes:
        if data.startswith(prefix):
            try:
                await handler(callback)
            except Exception:
                logging.exception("处理回调失败：%s", data)
                try:
                    await callback.answer("操作失败，请重试")
                except Exception:
                    logging.debug("回调应答失败", exc_info=True)
            return
    await callback.answer()

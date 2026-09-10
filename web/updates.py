from __future__ import annotations

import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from bot.version import VERSION

GITHUB_REPO = "kuke2733/tg-bot-dl"
CACHE_OK = 60 * 60
CACHE_ERR = 5 * 60
TIMEOUT = 6

_lock = threading.Lock()
_state = {
    "fetched_at": 0.0,
    "ok": False,
    "busy": False,
    "data": None,
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_version(text: str) -> tuple[int, ...]:
    cleaned = (text or "").strip().lstrip("vV")
    parts: list[int] = []
    for item in cleaned.split("."):
        digits = ""
        for char in item:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def _default() -> dict:
    return {
        "current": VERSION,
        "latest": "",
        "has_update": False,
        "url": f"https://github.com/{GITHUB_REPO}/releases/latest",
        "notes": "",
    }


def _normalize(data: dict | None) -> dict:
    result = _default()
    if data:
        result.update(data)
    result["current"] = VERSION
    latest = str(result.get("latest") or "").strip().lstrip("vV")
    result["latest"] = latest
    result["has_update"] = bool(latest) and parse_version(latest) > parse_version(VERSION)
    return result


def _fetch() -> dict:
    request = urllib.request.Request(
        f"https://github.com/{GITHUB_REPO}/releases/latest",
        headers={"User-Agent": "tg-bot-dl"},
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            location = response.headers.get("Location") or response.geturl()
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location") or ""

    match = re.search(r"/releases/tag/([^/?#]+)", location or "")
    if not match:
        raise RuntimeError(f"无法解析最新版本地址: {location}")
    tag = urllib.parse.unquote(match.group(1))
    latest = tag.lstrip("vV")
    url = location if str(location).startswith("http") else f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}"
    return _normalize({
        "latest": latest,
        "url": url,
        "notes": "",
    })


def _refresh() -> dict:
    try:
        data = _fetch()
    except Exception:
        logging.debug("检查 GitHub 新版本失败", exc_info=True)
        with _lock:
            previous = _state["data"]
            _state["fetched_at"] = time.time()
            _state["ok"] = False
            _state["busy"] = False
        return _normalize(previous)
    with _lock:
        _state["fetched_at"] = time.time()
        _state["ok"] = True
        _state["busy"] = False
        _state["data"] = data
    return data


def _start_refresh() -> None:
    with _lock:
        if _state["busy"]:
            return
        _state["busy"] = True
    threading.Thread(target=_refresh, name="update-check", daemon=True).start()


def info(wait: bool = False) -> dict:
    now = time.time()
    with _lock:
        data = _state["data"]
        age = now - _state["fetched_at"]
        ttl = CACHE_OK if _state["ok"] else CACHE_ERR
        fresh = data is not None and age < ttl
        busy = _state["busy"]
    if fresh:
        return _normalize(data)
    if wait:
        return _refresh()
    if not busy:
        _start_refresh()
    return _normalize(data)

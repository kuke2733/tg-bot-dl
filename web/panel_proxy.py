"""经本机 TCP 行 JSON 调用 Bot 面板控制口。"""
from __future__ import annotations

import json
import socket
from pathlib import Path

from web import settings

TIMEOUT = 2.0


def port_path() -> Path:
    _, config = settings.default_folders()
    return Path(config) / "panel.port"


def read_port() -> int | None:
    path = port_path()
    if not path.exists():
        return None
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def call(op: str, **extra) -> tuple[dict, int]:
    """发送一行请求，返回 (payload, http_status)。"""
    port = read_port()
    if port is None:
        return {"ok": False, "available": False, "error": "bot_offline", "items": []}, 503

    req = {"op": op, **extra}
    line = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT) as sock:
            sock.settimeout(TIMEOUT)
            sock.sendall(line)
            chunks: list[bytes] = []
            while True:
                piece = sock.recv(4096)
                if not piece:
                    break
                chunks.append(piece)
                if b"\n" in piece:
                    break
            raw = b"".join(chunks).split(b"\n", 1)[0]
    except OSError:
        return {"ok": False, "available": False, "error": "bot_offline", "items": []}, 503

    if not raw:
        return {"ok": False, "available": False, "error": "empty_response", "items": []}, 503
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "available": False, "error": "bad_response", "items": []}, 502
    if not isinstance(data, dict):
        return {"ok": False, "available": False, "error": "bad_response", "items": []}, 502
    data.setdefault("available", True)
    return data, 200

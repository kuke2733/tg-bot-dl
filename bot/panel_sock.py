"""本机 TCP socket：行 JSON 协议，供 Web 面板查询/控制下载队列。"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from bot.app import CONFIG_FOLDER
from bot.download import panel

PORT_PATH = Path(CONFIG_FOLDER) / "panel.port"
HOST = "127.0.0.1"
MAX_LINE = 64 * 1024

_server: asyncio.AbstractServer | None = None


def _write_port(port: int) -> None:
    try:
        PORT_PATH.write_text(str(port), encoding="utf-8")
    except Exception:
        logging.exception("写入面板端口失败：%s", PORT_PATH)


def _clear_port() -> None:
    try:
        if PORT_PATH.exists():
            PORT_PATH.unlink()
    except Exception:
        logging.debug("清理面板端口文件失败", exc_info=True)


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
        if not raw:
            return
        if len(raw) > MAX_LINE:
            response = {"ok": False, "error": "line_too_long"}
        else:
            try:
                req = json.loads(raw.decode("utf-8").strip() or "{}")
            except json.JSONDecodeError:
                response = {"ok": False, "error": "bad_json"}
            else:
                response = await _dispatch(req if isinstance(req, dict) else {})
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
    except asyncio.TimeoutError:
        try:
            writer.write(b'{"ok":false,"error":"timeout"}\n')
            await writer.drain()
        except Exception:
            pass
    except Exception:
        logging.exception("面板 socket 处理失败")
        try:
            writer.write(b'{"ok":false,"error":"internal"}\n')
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _dispatch(req: dict) -> dict:
    op = str(req.get("op") or "").strip()
    if op == "list":
        return panel.snapshot()
    if op == "pause":
        return panel.set_paused(True)
    if op == "resume":
        return panel.set_paused(False)
    if op == "cancel_all":
        return await panel.cancel_all()
    if op == "stop":
        try:
            download_id = int(req.get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_id", **panel.snapshot()}
        return await panel.stop_download(download_id)
    if op == "stop_batch":
        batch_id = req.get("id")
        if not batch_id:
            return {"ok": False, "error": "bad_id", **panel.snapshot()}
        return await panel.stop_batch(str(batch_id))
    return {"ok": False, "error": "unknown_op", **panel.snapshot()}


async def serve() -> None:
    """监听 127.0.0.1 空闲端口，直到任务取消。"""
    global _server
    _clear_port()
    server = await asyncio.start_server(_handle, host=HOST, port=0)
    _server = server
    sockets = server.sockets or []
    if not sockets:
        logging.error("面板 socket 启动失败：无监听套接字")
        return
    port = sockets[0].getsockname()[1]
    _write_port(port)
    logging.info("面板控制口已监听 %s:%s", HOST, port)
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        raise
    finally:
        _clear_port()
        _server = None
        logging.info("面板控制口已关闭")

import os
import signal
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session

from bot.version import VERSION
from web import settings, updates
from web import panel_proxy
from web.supervisor import supervisor

ROOT = Path(__file__).resolve().parent
app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)


def secret_key() -> str:
    _, config = settings.default_folders()
    path = Path(config) / "web.secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = os.urandom(24).hex()
    path.write_text(value, encoding="utf-8")
    return value


app.secret_key = secret_key()


def authorized() -> bool:
    password = (settings.load().get("WEB_PASSWORD") or "").strip()
    if not password:
        return True
    return session.get("auth") is True


@app.get("/")
def index():
    return render_template("index.html", version=VERSION)


@app.get("/api/status")
def api_status():
    snap = supervisor.snapshot()
    snap["authorized"] = authorized()
    snap["update"] = updates.info(wait=False)
    if not snap["authorized"]:
        snap["settings"] = {}
        snap["logs"] = ""
        snap["missing"] = []
    response = jsonify(snap)
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    return response


@app.get("/api/update")
def api_update():
    response = jsonify({
        "update": updates.info(wait=True),
        "in_docker": settings.running_in_docker(),
    })
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response


@app.post("/api/login")
def api_login():
    password = (settings.load().get("WEB_PASSWORD") or "").strip()
    given = (request.json or {}).get("password", "")
    if not password or given == password:
        session["auth"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "message": "密码不对"}), 401


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


def require_auth():
    if not authorized():
        return jsonify({"ok": False, "message": "请先登录"}), 401
    return None


@app.post("/api/settings")
def api_settings():
    denied = require_auth()
    if denied:
        return denied
    payload = request.json or {}
    values = {key: payload.get(key, "") for key, *_ in settings.FIELDS}
    if payload.get("DEBUG") in (True, "1", "true", "on"):
        values["DEBUG"] = "1"
    else:
        values["DEBUG"] = ""
    api_id = str(values.get("TELEGRAM_API_ID") or "").strip()
    if api_id and not api_id.isdigit():
        return jsonify({"ok": False, "message": "API ID 必须是数字"}), 400
    saved = settings.save(values)
    missing = settings.missing_required(saved)
    if missing:
        supervisor.stop()
        return jsonify({
            "ok": True,
            "applied": False,
            "message": "配置已保存，但还缺：" + "、".join(missing),
            **supervisor.snapshot(),
        })
    ok, message = supervisor.start(restart=True)
    snap = supervisor.snapshot()
    return jsonify({"ok": ok, "applied": ok, "message": message, **snap})


@app.post("/api/start")
def api_start():
    denied = require_auth()
    if denied:
        return denied
    ok, message = supervisor.start(restart=False)
    return jsonify({"ok": ok, "message": message, "authorized": True, **supervisor.snapshot()})


@app.post("/api/stop")
def api_stop():
    denied = require_auth()
    if denied:
        return denied
    ok, message = supervisor.stop()
    return jsonify({"ok": ok, "message": message, "authorized": True, **supervisor.snapshot()})


@app.post("/api/input")
def api_input():
    denied = require_auth()
    if denied:
        return denied
    text = (request.json or {}).get("text", "")
    ok, message = supervisor.send_input(text)
    return jsonify({"ok": ok, "message": message, "authorized": True, **supervisor.snapshot()})


def _downloads_response(payload: dict, status: int):
    response = jsonify(payload)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response, status


@app.get("/api/downloads")
def api_downloads():
    denied = require_auth()
    if denied:
        return denied
    payload, status = panel_proxy.call("list")
    return _downloads_response(payload, status)


@app.post("/api/downloads/pause")
def api_downloads_pause():
    denied = require_auth()
    if denied:
        return denied
    payload, status = panel_proxy.call("pause")
    return _downloads_response(payload, status)


@app.post("/api/downloads/resume")
def api_downloads_resume():
    denied = require_auth()
    if denied:
        return denied
    payload, status = panel_proxy.call("resume")
    return _downloads_response(payload, status)


@app.post("/api/downloads/cancel-all")
def api_downloads_cancel_all():
    denied = require_auth()
    if denied:
        return denied
    payload, status = panel_proxy.call("cancel_all")
    return _downloads_response(payload, status)


@app.post("/api/downloads/<int:download_id>/stop")
def api_downloads_stop(download_id: int):
    denied = require_auth()
    if denied:
        return denied
    payload, status = panel_proxy.call("stop", id=download_id)
    return _downloads_response(payload, status)


@app.post("/api/downloads/batch/<path:batch_id>/stop")
def api_downloads_stop_batch(batch_id: str):
    denied = require_auth()
    if denied:
        return denied
    payload, status = panel_proxy.call("stop_batch", id=batch_id)
    return _downloads_response(payload, status)


def getenv_host() -> str:
    return os.getenv("WEB_HOST", "0.0.0.0")


def _should_manage_bot(debug: bool) -> bool:
    # Flask 热重载父进程也会跑 main，机器人只交给真正提供服务的进程管理
    return (not debug) or os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def main():
    host = getenv_host()
    port = int(os.getenv("WEB_PORT", "8080"))

    data = settings.load()
    flask_debug = os.getenv("FLASK_DEBUG") or data.get("FLASK_DEBUG", "")
    debug = flask_debug.strip().lower() in ("1", "true", "yes", "on")
    manage_bot = _should_manage_bot(debug)

    if manage_bot and not settings.missing_required(data):
        supervisor.start()

    def handle_stop(*_args):
        if manage_bot:
            supervisor.stop()
        os._exit(0)

    signal.signal(signal.SIGINT, handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_stop)

    print(f"Open config page: http://127.0.0.1:{port}", flush=True)
    if debug:
        print("Running in DEBUG mode with hot reload enabled", flush=True)
    app.run(host=host, port=port, debug=debug, use_reloader=debug, threaded=True)

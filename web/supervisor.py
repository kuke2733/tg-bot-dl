import atexit
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen

import psutil

from bot.version import VERSION
from web import settings

PROMPTS = (
    ("waiting_code", "The confirmation code has been sent via"),
    ("waiting_code", "Enter confirmation code"),
    ("waiting_password", "Enter 2FA password"),
    ("waiting_password", "Password hint:"),
    ("waiting_password", "Two-step verification"),
)

_PY_LOG = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):([^:]*):(.*)$", re.I)
_WEB_LOG_LEVEL = {
    "DEBUG": logging.DEBUG,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

ROOT = Path(__file__).resolve().parent.parent
CREATE_NEW_PROCESS_GROUP = 0x00000200


class BotSupervisor:
    def __init__(self):
        self.proc: Popen | None = None
        self.logs = deque(maxlen=400)
        self.state = "stopped"
        self.waiting_prompt = ""
        self.bot_username = ""
        self.exit_code = None
        self.lock = threading.Lock()
        self.reader = None
        self.in_error_traceback = False

    def snapshot(self):
        data = settings.load()
        with self.lock:
            return {
                "state": self.state,
                "waiting_prompt": self.waiting_prompt,
                "bot_username": self.bot_username,
                "exit_code": self.exit_code,
                "logs": "\n".join(self.logs),
                "settings": data,
                "missing": settings.missing_required(data),
                "in_docker": settings.running_in_docker(),
                "has_password": bool((data.get("WEB_PASSWORD") or "").strip()),
                "version": VERSION,
            }

    def append(self, line: str):
        line = line.rstrip("\n")
        if not line:
            return

        level = "INFO"
        display = line
        line_lower = line.lower()
        py = _PY_LOG.match(line)

        if "traceback (most recent call last)" in line_lower:
            self.in_error_traceback = True
            level = "ERROR"
        elif py:
            # 用 Python 自带级别，去掉正文 INFO:root: 避免面板叠两层
            level = py.group(1).upper()
            if level == "CRITICAL":
                level = "ERROR"
            logger, message = py.group(2), py.group(3)
            display = message if logger in ("", "root") else f"{logger}: {message}"
            self.in_error_traceback = False
        elif self.in_error_traceback:
            level = "ERROR"
            if (
                line
                and not line[0].isspace()
                and not line.startswith("File")
                and not any(line.startswith(c) for c in "^~")
                and not any(x in line for x in ["Error", "Exception"])
            ):
                self.in_error_traceback = False
        elif any(x in line_lower for x in ["error", "exception", "fatal"]):
            level = "ERROR"
            self.in_error_traceback = True
        elif any(x in line_lower for x in ["warning", "warn"]):
            level = "WARNING"
            self.in_error_traceback = False
        elif "debug" in line_lower:
            level = "DEBUG"
            self.in_error_traceback = False
        elif any(x in line_lower for x in ["success", "started", "完成", "成功"]):
            level = "SUCCESS"
            self.in_error_traceback = False
        else:
            self.in_error_traceback = False

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_line = f"[{timestamp}] [{level}] {display}"
        # 子进程日志转给 docker；面板自己的提示走 logging，避免和机器人落盘重复
        if py or self.in_error_traceback:
            print(line, flush=True)
        else:
            logging.getLogger("web").log(_WEB_LOG_LEVEL.get(level, logging.INFO), display)

        with self.lock:
            self.logs.append(formatted_line)
            if "Bot started! I'm @" in line:
                self.bot_username = line.split("I'm ", 1)[-1].strip()
                self.state = "running"
                self.waiting_prompt = ""
            for state, needle in PROMPTS:
                if needle.lower() in line.lower():
                    self.state = state
                    self.waiting_prompt = line.strip()
                    break

    def _is_project_bot(self, proc: psutil.Process) -> bool:
        try:
            cmdline = proc.cmdline()
            if "-m" not in cmdline or "bot.run" not in cmdline:
                return False
            return Path(proc.cwd()).resolve() == ROOT
        except (psutil.Error, OSError):
            return False

    def _kill_tree(self, proc: psutil.Process, timeout=5.0):
        try:
            targets = proc.children(recursive=True) + [proc]
        except (psutil.Error, OSError):
            targets = [proc]
        for item in targets:
            try:
                item.terminate()
            except (psutil.Error, OSError):
                pass
        _, alive = psutil.wait_procs(targets, timeout=timeout)
        for item in alive:
            try:
                item.kill()
            except (psutil.Error, OSError):
                pass

    def cleanup_orphans(self, *, silent=False, wait=True):
        """结束本项目残留的 bot.run，避免 session SQLite 被锁。"""
        killed = []
        for proc in psutil.process_iter(["pid"]):
            if proc.pid == os.getpid():
                continue
            if not self._is_project_bot(proc):
                continue
            self._kill_tree(proc)
            killed.append(proc.pid)
        if killed and not silent:
            self.append("已清理残留机器人进程：" + "、".join(map(str, killed)))
        if killed and wait:
            time.sleep(0.8)
        return killed

    def start(self, restart=False):
        data = settings.load()
        missing = settings.missing_required(data)
        if missing:
            message = "缺少必填配置：" + "、".join(missing)
            self.append(message)
            return False, message

        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            if running and not restart:
                return True, "机器人已在运行"

            tracked = self.proc if running else None
            self.proc = None
            if tracked is not None and tracked.poll() is None:
                try:
                    self._kill_tree(psutil.Process(tracked.pid))
                except (psutil.Error, OSError):
                    try:
                        tracked.kill()
                        tracked.wait(timeout=3)
                    except Exception as error:
                        self.append(f"停止失败：{error}")
                        self.proc = tracked
                        return False, str(error)

            self.cleanup_orphans()

            env = os.environ.copy()
            for key in set(settings.BOT_KEYS + [key for key, *_ in settings.FIELDS]):
                env.pop(key, None)
            env.update(settings.bot_env(data))
            env["PYTHONUNBUFFERED"] = "1"

            kwargs = {
                "cwd": str(ROOT),
                "env": env,
                "stdin": PIPE,
                "stdout": PIPE,
                "stderr": STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 0,
            }
            if os.name == "nt":
                kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True

            proc = Popen([sys.executable, "-m", "bot.run"], **kwargs)
            self.proc = proc
            self.state = "starting"
            self.waiting_prompt = ""
            self.bot_username = ""
            self.exit_code = None

        self.append("正在启动机器人...")
        self.reader = threading.Thread(target=self._read, args=(proc,), daemon=True)
        self.reader.start()
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        return True, "已启动"

    def stop(self):
        with self.lock:
            proc = self.proc
            self.proc = None

        stopped = False
        if proc is not None and proc.poll() is None:
            try:
                self._kill_tree(psutil.Process(proc.pid))
                stopped = True
            except (psutil.Error, OSError):
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                    stopped = True
                except Exception as error:
                    self.append(f"停止失败：{error}")
                    with self.lock:
                        self.proc = proc
                    return False, str(error)

        if self.cleanup_orphans(silent=True):
            stopped = True

        with self.lock:
            self.state = "stopped"
            self.waiting_prompt = ""

        if stopped:
            self.append("机器人已停止")
            return True, "已停止"
        return False, "当前没有运行中的机器人"

    def send_input(self, text: str):
        text = (text or "").strip()
        if not text:
            return False, "请输入内容"
        with self.lock:
            proc = self.proc
        if not proc or proc.poll() is not None or not proc.stdin:
            return False, "机器人未在等待输入"
        try:
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
        except Exception as error:
            return False, str(error)
        self.append("已提交验证信息")
        with self.lock:
            if self.state.startswith("waiting"):
                self.state = "starting"
                self.waiting_prompt = ""
        return True, "已提交"

    def _read(self, proc: Popen):
        stream = proc.stdout
        if not stream:
            return
        buf = ""
        partial = False
        while True:
            chunk = stream.read(1)
            if chunk == "":
                break
            buf += chunk
            if chunk in "\n\r":
                if buf.strip():
                    self.append(buf)
                buf = ""
                partial = False
                continue
            lowered = buf.lower()
            if not partial and any(needle.lower() in lowered for _, needle in PROMPTS):
                self.append(buf)
                partial = True
        if buf.strip():
            self.append(buf)

    def _watch(self, proc: Popen):
        code = proc.wait()
        with self.lock:
            if self.proc is proc:
                self.exit_code = code
                if self.state != "stopped":
                    self.state = "exited" if code else "stopped"
                self.waiting_prompt = ""
                self.proc = None
        self.append(f"进程已退出，代码 {code}")


supervisor = BotSupervisor()


def _atexit_stop():
    with supervisor.lock:
        proc = supervisor.proc
    if proc is not None and proc.poll() is None:
        supervisor.stop()


atexit.register(_atexit_stop)

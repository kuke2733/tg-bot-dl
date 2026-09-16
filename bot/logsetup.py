"""按天落盘的日志：config/logs/YYYY-MM-DD.log，控制台格式保持给面板解析。"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path

KEEP_DAYS = 30
STREAM_FORMAT = "%(levelname)s:%(name)s:%(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class FileFormatter(logging.Formatter):
    """文件里不要终端颜色，一条记录拆成多行时后续行缩进，方便阅读。"""

    def format(self, record: logging.LogRecord) -> str:
        text = _ANSI.sub("", super().format(record)).replace("\r", "")
        lines = [line for line in (part.rstrip() for part in text.splitlines()) if line]
        if len(lines) <= 1:
            return lines[0] if lines else ""
        return lines[0] + "".join("\n    " + line for line in lines[1:])


class DailyFileHandler(logging.FileHandler):
    """写到目录下当天的 YYYY-MM-DD.log，日期变化时换文件。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._day = date.today()
        super().__init__(self._path_for(self._day), mode="a", encoding="utf-8", delay=False)

    def _path_for(self, day: date) -> str:
        return str(self.directory / f"{day.isoformat()}.log")

    def emit(self, record: logging.LogRecord) -> None:
        today = date.today()
        if today != self._day:
            if self.stream:
                self.flush()
                self.stream.close()
                self.stream = None
            self.baseFilename = os.path.abspath(self._path_for(today))
            self.stream = self._open()
            self._day = today
        super().emit(record)
        self.flush()


def log_dir(config_folder: str | Path) -> Path:
    path = Path(config_folder) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prune_old_logs(directory: Path, keep_days: int = KEEP_DAYS) -> None:
    cutoff = date.today() - timedelta(days=keep_days)
    for path in directory.glob("????-??-??.log"):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def configure_logging(config_folder: str | Path, *, debug: bool = False) -> Path:
    os.environ.setdefault("NO_COLOR", "1")
    directory = log_dir(config_folder)
    prune_old_logs(directory)

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(STREAM_FORMAT))
    if hasattr(stream.stream, "reconfigure"):
        try:
            stream.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    file_handler = DailyFileHandler(directory)
    file_handler.setFormatter(FileFormatter(FILE_FORMAT))

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        handlers=[stream, file_handler],
        force=True,
    )
    return directory

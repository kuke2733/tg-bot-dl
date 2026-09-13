from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bot.app import CONFIG_FOLDER

DB_PATH = Path(CONFIG_FOLDER) / "downloads.sqlite"
CHUNK_SIZE = 1024 * 1024

_lock = threading.RLock()
_initialized = False


@dataclass(frozen=True)
class DownloadRecord:
    unique_id: str
    sha256: str
    size: int
    path: str
    created_at: float


def init() -> None:
    global _initialized
    with _lock:
        if _initialized:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH, timeout=5) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unique_id TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    path TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_unique_id
                ON downloads(unique_id) WHERE unique_id != ''
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_sha256
                ON downloads(sha256) WHERE sha256 != ''
                """
            )
        _initialized = True


def _connect() -> sqlite3.Connection:
    init()
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _fetchone(sql: str, params: tuple) -> sqlite3.Row | None:
    with _lock:
        with _connect() as conn:
            return conn.execute(sql, params).fetchone()


def _row_to_record(row: sqlite3.Row | None) -> DownloadRecord | None:
    if row is None:
        return None
    return DownloadRecord(
        unique_id=str(row["unique_id"] or ""),
        sha256=str(row["sha256"] or ""),
        size=int(row["size"] or 0),
        path=str(row["path"] or ""),
        created_at=float(row["created_at"] or 0),
    )


def find_by_unique_id(unique_id: str) -> DownloadRecord | None:
    if not unique_id:
        return None
    return _row_to_record(
        _fetchone(
            "SELECT unique_id, sha256, size, path, created_at FROM downloads WHERE unique_id = ?",
            (unique_id,),
        )
    )


def find_by_sha256(sha256: str) -> DownloadRecord | None:
    if not sha256:
        return None
    return _row_to_record(
        _fetchone(
            "SELECT unique_id, sha256, size, path, created_at FROM downloads WHERE sha256 = ?",
            (sha256,),
        )
    )


def delete_by_unique_id(unique_id: str) -> None:
    if not unique_id:
        return
    with _lock:
        with _connect() as conn:
            conn.execute("DELETE FROM downloads WHERE unique_id = ?", (unique_id,))


def delete_by_path(path: str) -> int:
    """按相对路径删除去重记录：文件被删后记录也要失效，避免下次误判「下载过」。"""
    normalized = (path or "").replace("\\", "/").strip("/")
    if not normalized:
        return 0
    with _lock:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM downloads WHERE path = ?", (normalized,))
            return cur.rowcount


def remember(unique_id: str, sha256: str, size: int, path: str) -> None:
    unique_id = unique_id or ""
    sha256 = sha256 or ""
    path = (path or "").replace("\\", "/")
    with _lock:
        with _connect() as conn:
            if unique_id:
                conn.execute("DELETE FROM downloads WHERE unique_id = ?", (unique_id,))
            if sha256:
                existing = conn.execute(
                    "SELECT id FROM downloads WHERE sha256 = ?",
                    (sha256,),
                ).fetchone()
                if existing:
                    return
            conn.execute(
                """
                INSERT INTO downloads (unique_id, sha256, size, path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (unique_id, sha256, int(size or 0), path, time.time()),
            )


def hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()

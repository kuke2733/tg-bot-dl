from __future__ import annotations

from dataclasses import dataclass, field

from pyrogram.client import Client
from pyrogram.types import Message

UNFINISHED_STATUS = {"waiting", "downloading", "duplicate"}
SUCCESS_STATUS = {"done", "content_duplicate"}
FAILED_STATUS = {"failed", "stopped", "deleted"}


@dataclass
class BatchItem:
    download_id: int
    name: str
    status: str = "waiting"
    received: int = 0
    total: int = 0
    started: float = 0.0


@dataclass
class Batch:
    id: str
    folder: str
    total: int
    message: Message
    directory: str = ""
    last_update: float = 0.0
    stopped: bool = False
    items: list[BatchItem] = field(default_factory=list)
    pending_unique: list[Download] = field(default_factory=list)

    def item_for(self, download_id: int) -> BatchItem | None:
        for item in self.items:
            if item.download_id == download_id:
                return item
        return None

    @property
    def done(self) -> int:
        return sum(item.status in SUCCESS_STATUS for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status in FAILED_STATUS for item in self.items)

    @property
    def skipped(self) -> int:
        return sum(item.status == "skipped" for item in self.items)

    @property
    def finished(self) -> int:
        return sum(item.status not in UNFINISHED_STATUS for item in self.items)


@dataclass
class Download:
    client: Client
    id: int
    filename: str
    from_message: Message
    progress_message: Message
    started: float = 0.0
    last_update: float = 0.0
    size: int = 0
    expected_size: int = 0
    batch: Batch | None = None
    batch_item: BatchItem | None = None
    pending_rename: str | None = None
    stopped: bool = False
    ui_seq: int = 0
    unique_id: str = ""
    skip_hash_check: bool = False

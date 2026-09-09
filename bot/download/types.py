from __future__ import annotations

from dataclasses import dataclass, field

from pyrogram.client import Client
from pyrogram.types import Message


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

    def item_for(self, download_id: int) -> BatchItem | None:
        for item in self.items:
            if item.download_id == download_id:
                return item
        return None

    @property
    def done(self) -> int:
        return sum(item.status == "done" for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status in {"failed", "stopped"} for item in self.items)

    @property
    def finished(self) -> int:
        return self.done + self.failed


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

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pyrogram.client import Client
from pyrogram.types import Message

UNFINISHED_STATUS = {"waiting", "downloading", "duplicate", "cold"}
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
    quiet: bool = False
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
    # 频道自动下载的安静模式：不与频道交互、结果走摘要、完成后删临时消息
    quiet: bool = False
    # 正在执行的下载任务；停止后超时会被强制取消
    task: asyncio.Task | None = None
    finalizing: bool = False
    cancel_scheduled: bool = False
    # 短周期重试耗尽后的长周期自动重试：已完成轮数与等待定时任务
    retry_round: int = 0
    retry_task: asyncio.Task | None = None
    # True 时 downloadFile 收尾保留 queue.json 记录（驻留等待自动重试/磁盘恢复期间）
    will_requeue: bool = False
    # 驻留任务收尾单飞标记：停止/唤醒多条路径并发时只收尾一次
    hold_aborted: bool = False

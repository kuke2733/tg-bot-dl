from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

from pyrogram.client import Client
from pyrogram.types import Message

class Status(StrEnum):
    """批次条目/下载任务的状态字。StrEnum：与裸字符串比较/入集合都兼容。"""

    WAITING = "waiting"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    DELETED = "deleted"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"
    CONTENT_DUPLICATE = "content_duplicate"
    COLD = "cold"


class HoldReason(StrEnum):
    """任务驻留原因。驻留表示暂不参与调度，但断点与 queue.json 记录保留。"""

    RETRY = "retry"  # 等待下一轮长周期自动重试
    DISK = "disk"  # 磁盘写满，等 /resume 释放
    COLD = "cold"  # 重试轮次用尽，等连接恢复由健康检查唤醒


UNFINISHED_STATUS = {Status.WAITING, Status.DOWNLOADING, Status.DUPLICATE, Status.COLD}
SUCCESS_STATUS = {Status.DONE, Status.CONTENT_DUPLICATE}
FAILED_STATUS = {Status.FAILED, Status.STOPPED, Status.DELETED}

# 状态展示映射：图标与中文标签，跟枚举放一起作为唯一出处
STATUS_MARK: dict[Status, str] = {
    Status.DONE: "✅",
    Status.WAITING: "⏳",
    Status.DOWNLOADING: "⬇️",
    Status.FAILED: "❌",
    Status.STOPPED: "⏹",
    Status.DELETED: "🗑️",
    Status.DUPLICATE: "⚠️",
    Status.SKIPPED: "⏭",
    Status.CONTENT_DUPLICATE: "⚠️",
    Status.COLD: "❄️",
}

STATUS_LABEL: dict[Status, str] = {
    Status.DONE: "",
    Status.WAITING: "等待中",
    Status.STOPPED: "已停止",
    Status.DELETED: "已删除",
    Status.FAILED: "失败",
    Status.DUPLICATE: "重复",
    Status.SKIPPED: "已跳过",
    Status.CONTENT_DUPLICATE: "内容重复",
    Status.COLD: "等待网络恢复",
}


@dataclass
class BatchItem:
    download_id: int
    name: str
    status: str = "waiting"
    received: int = 0
    total: int = 0
    started: float = 0.0
    resume_from: int = 0


@dataclass
class Batch:
    id: str
    folder: str
    total: int
    message: Message | None
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
    progress_message: Message | None
    started: float = 0.0
    last_update: float = 0.0
    size: int = 0
    expected_size: int = 0
    resume_from: int = 0
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
    # True 时 downloadFile 收尾保留 queue.json 记录，用于驻留等待自动重试或磁盘恢复期间
    will_requeue: bool = False
    # 队列暂停：停下当前传输并重新入队，断点留在 .temp
    pausing: bool = False
    # 驻留任务收尾单飞标记：停止/唤醒多条路径并发时只收尾一次
    hold_aborted: bool = False
    # 面板实时进度（进度回调写入）
    received: int = 0
    speed: float = 0.0
    eta: int | None = None

"""下载中的滑动窗口实时速度。完成时的整段均速仍由调用方按会话字节/用时现算。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

WINDOW_SECONDS = 6.0
MIN_ELAPSED = 0.3


def eta_seconds(received: int, total: int, speed: float) -> int | None:
    if not total:
        return None
    if received >= total:
        return 0
    if speed <= 0:
        return None
    return int((total - received) / speed)


@dataclass
class SpeedMeter:
    samples: deque[tuple[float, int]] = field(default_factory=deque)

    def reset(self) -> None:
        self.samples.clear()

    def update(self, received: int, now: float) -> float:
        received = int(received)
        if self.samples:
            _, last_b = self.samples[-1]
            if received < last_b:
                self.samples.clear()
            elif received == last_b:
                return self.speed_at(now)
        self.samples.append((now, received))
        return self.speed_at(now)

    def speed_at(self, now: float) -> float:
        self._prune(now)
        if not self.samples:
            return 0.0
        t0, b0 = self.samples[0]
        dt = now - t0
        if dt < MIN_ELAPSED:
            return 0.0
        return max(self.samples[-1][1] - b0, 0) / dt

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while (
            len(self.samples) >= 2
            and self.samples[0][0] < cutoff
            and self.samples[1][0] <= cutoff
        ):
            self.samples.popleft()

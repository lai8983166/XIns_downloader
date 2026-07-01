"""配额计数器（design D8 + account-safety 规格）。

- 会话级：每 SESSION_WINDOW_MINUTES 滑动窗口 ≤ SESSION_ACTION_LIMIT 次读取动作。
- 每日：每本地自然日 ≤ DAILY_ACTION_LIMIT 次读取动作。
- 触及上限：采集层拒绝并向上抛 QuotaExceeded，由路由层映射为 HTTP 429。

线程安全：操作极快（窗口内动作数被 limit 封顶），用 threading.Lock 保护即可；
即便在 asyncio 事件循环中调用也不会阻塞。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date

from collector.config import settings


class QuotaExceeded(Exception):
    """配额已达上限。kind: "session" | "daily"。"""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


@dataclass
class QuotaStatus:
    session_used: int
    session_limit: int
    session_window_minutes: int
    daily_used: int
    daily_limit: int


class QuotaGuard:
    """会话滑动窗口 + 每日计数器。

    “动作”= 一次面向 Instagram 的读取（如加载一个帖子、翻一页 Profile）。
    """

    def __init__(self, session_window_minutes: int, session_action_limit: int, daily_action_limit: int):
        self._window_seconds = session_window_minutes * 60
        self._session_limit = session_action_limit
        self._daily_limit = daily_action_limit
        self._lock = threading.Lock()
        self._timestamps: list[float] = []  # 滑动窗口内的动作时间戳
        self._daily_count = 0
        self._daily_day: date | None = None

    def _rollover_daily(self, today: date) -> None:
        if self._daily_day != today:
            self._daily_day = today
            self._daily_count = 0

    def _purge_window(self, now: float) -> None:
        cutoff = now - self._window_seconds
        self._timestamps = [t for t in self._timestamps if t >= cutoff]

    def _evaluate(self, now: float) -> None:
        """在已持锁的前提下检查上限。超限抛 QuotaExceeded（不计数）。"""
        self._rollover_daily(date.today())
        self._purge_window(now)
        if len(self._timestamps) >= self._session_limit:
            raise QuotaExceeded(
                "session",
                f"会话配额已满（{self._window_seconds // 60} 分钟内 ≤ {self._session_limit} 次读取），请稍后再试。",
            )
        if self._daily_count >= self._daily_limit:
            raise QuotaExceeded(
                "daily",
                f"今日配额已满（≤ {self._daily_limit} 次/日），请明日再试。",
            )

    def check(self) -> None:
        """仅检查是否允许新动作（不计数）。超限抛 QuotaExceeded。"""
        with self._lock:
            self._evaluate(time.time())

    def acquire(self) -> None:
        """检查并记一次动作。超限抛 QuotaExceeded（此时不计数）。"""
        with self._lock:
            now = time.time()
            self._evaluate(now)
            self._timestamps.append(now)
            self._daily_count += 1

    def status(self) -> QuotaStatus:
        """当前配额用量（供调试 / 健康检查）。"""
        with self._lock:
            self._evaluate_pure()
            return QuotaStatus(
                session_used=len(self._timestamps),
                session_limit=self._session_limit,
                session_window_minutes=int(self._window_seconds // 60),
                daily_used=self._daily_count,
                daily_limit=self._daily_limit,
            )

    def _evaluate_pure(self) -> None:
        """status() 专用：只清理过期数据，不做上限断言（读状态不应抛错）。"""
        now = time.time()
        self._rollover_daily(date.today())
        self._purge_window(now)


quota = QuotaGuard(
    settings.session_window_minutes,
    settings.session_action_limit,
    settings.daily_action_limit,
)

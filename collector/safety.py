"""风控信号检测 + 冷却状态机（design D7 + account-safety 规格）。

两件事：
1. detect_signal(...)：根据 url / HTTP 状态 / 响应体 / 页面文本检测风控信号。
   命中 → 采集层立即停手（不重试）并上报。
2. CooldownState：阶梯退避（默认 15→30→60→120 分钟，上限 360 分钟）；
   冷却期内所有采集请求直接被拒；连续 24 小时无信号则档位归零。

只读契约：采集层只允许 导航 / 滚动 / 读网络响应 / 下载 CDN 媒体，禁止任何写操作
（点赞 / 关注 / 评论 / 收藏 / 分享）。该契约由 collector 模块的 __all__ 与 API 设计落实，
safety 层不提供运行时强制（脆弱且无必要）——靠代码纪律 + review 保证。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from collector.config import settings

# ----------------------------- 信号类型 -----------------------------

SIGNAL_RATE_LIMITED = "rate_limited"        # 429 / 限流提示
SIGNAL_ACTION_BLOCKED = "action_blocked"    # "Action Blocked"
SIGNAL_CHALLENGE = "challenge"              # /challenge / /interventions / /confirm
SIGNAL_LOGIN_WALL = "login_wall"            # 登录墙 / 会话失效
SIGNAL_UNEXPECTED = "unexpected"            # 异常重定向 / 空响应 / 非预期

# 触发限流的文本特征（小写匹配）
_RATE_LIMIT_PHRASES = (
    "please wait a few minutes",
    "try again later",
    "too many requests",
    "rate limit",
)
_ACTION_BLOCKED_PHRASES = (
    "action blocked",
    "actions blocked",
    "temporarily blocked",
)
_CHALLENGE_HINTS = ("/challenge", "/interventions", "/confirm")
_LOGIN_HINTS = ("/accounts/login",)


def detect_signal(
    *,
    url: Optional[str] = None,
    status: Optional[int] = None,
    body: Optional[str] = None,
    page_text: Optional[str] = None,
) -> Optional[str]:
    """根据可用上下文检测风控信号，返回信号类型常量或 None。

    优先级：HTTP 429 / 限流短语 > Action Blocked > challenge > 登录墙 > 空响应。
    """
    text = f"{body or ''}\n{page_text or ''}".lower()
    url_lower = (url or "").lower()

    if status == 429:
        return SIGNAL_RATE_LIMITED
    if any(p in text for p in _RATE_LIMIT_PHRASES):
        return SIGNAL_RATE_LIMITED
    if any(p in text for p in _ACTION_BLOCKED_PHRASES):
        return SIGNAL_ACTION_BLOCKED
    if any(h in url_lower for h in _CHALLENGE_HINTS):
        return SIGNAL_CHALLENGE
    if any(h in url_lower for h in _LOGIN_HINTS):
        return SIGNAL_LOGIN_WALL
    if body is not None and len(body.strip()) == 0:
        return SIGNAL_UNEXPECTED
    return None


# ----------------------------- 冷却状态机 -----------------------------


class CoolDown(Exception):
    """当前处于冷却期。remaining_seconds: 剩余秒数；reason: 触发信号。"""

    def __init__(self, remaining_seconds: float, reason: Optional[str]):
        self.remaining_seconds = remaining_seconds
        self.reason = reason
        super().__init__(f"cooling down {int(remaining_seconds)}s (reason={reason})")


class CooldownState:
    """阶梯退避冷却状态机。线程安全。"""

    def __init__(self, steps_min: list[int], max_minutes: int):
        self._steps_sec = [m * 60 for m in steps_min]
        self._max_sec = max_minutes * 60
        self._lock = threading.Lock()
        self._level = 0  # 下一次命中所用的档位索引
        self._until = 0.0  # 冷却到期时间戳
        self._last_signal_at = 0.0
        self._last_reason: Optional[str] = None

    def _maybe_reset(self, now: float) -> None:
        """连续 24 小时无信号则档位归零。"""
        if self._level > 0 and (now - self._last_signal_at) > 24 * 3600:
            self._level = 0

    def check(self) -> None:
        """若仍处于冷却期，抛 CoolDown。"""
        with self._lock:
            now = time.time()
            self._maybe_reset(now)
            if now < self._until:
                raise CoolDown(self._until - now, self._last_reason)

    def report(self, signal: str) -> int:
        """上报一个风控信号，进入/加深冷却。返回本次冷却秒数。"""
        with self._lock:
            now = time.time()
            self._maybe_reset(now)
            idx = min(self._level, len(self._steps_sec) - 1)
            secs = int(min(self._steps_sec[idx], self._max_sec))
            self._until = now + secs
            self._level = min(self._level + 1, len(self._steps_sec))
            self._last_signal_at = now
            self._last_reason = signal
            return secs

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            self._maybe_reset(now)
            return {
                "cooling_down": now < self._until,
                "remaining_seconds": max(0, int(self._until - now)),
                "level": self._level,
                "last_reason": self._last_reason,
            }


cooldown = CooldownState(settings.cooldown_steps_min, settings.cooldown_max_minutes)

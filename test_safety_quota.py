"""Phase 2 单元验证：quota + safety（纯逻辑，不联网，不依赖 pytest）。

运行：python test_safety_quota.py
"""
from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from collector.quota import QuotaExceeded, QuotaGuard
from collector.safety import (
    SIGNAL_CHALLENGE,
    SIGNAL_LOGIN_WALL,
    SIGNAL_RATE_LIMITED,
    SIGNAL_UNEXPECTED,
    CoolDown,
    CooldownState,
    detect_signal,
)

_passed = 0
_failed = 0


def ok(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}")


def raises(fn, exc_type) -> bool:
    try:
        fn()
        return False
    except exc_type:
        return True
    except Exception:
        return False


def main() -> int:
    print("=== QuotaGuard ===")
    q = QuotaGuard(session_window_minutes=60, session_action_limit=3, daily_action_limit=100)
    ok("初始 session_used=0", q.status().session_used == 0)
    for _ in range(3):
        q.acquire()
    ok("达到 session 上限后第 4 次抛 QuotaExceeded", raises(lambda: q.acquire(), QuotaExceeded))
    try:
        q.acquire()
    except QuotaExceeded as e:
        ok("超 session 时 kind=session", e.kind == "session")
    ok("status 反映已用 3 次", q.status().session_used == 3)

    # 窗口过期后释放（用极短窗口）
    qw = QuotaGuard(session_window_minutes=0.01, session_action_limit=1, daily_action_limit=100)
    qw.acquire()
    ok("极短窗口内第 2 次抛 QuotaExceeded", raises(lambda: qw.acquire(), QuotaExceeded))
    time.sleep(1)  # > 0.6s 窗口
    ok("窗口过期后第 2 次 acquire 成功", not raises(lambda: qw.acquire(), QuotaExceeded))

    # 每日上限
    qd = QuotaGuard(session_window_minutes=60, session_action_limit=1000, daily_action_limit=2)
    qd.acquire()
    qd.acquire()
    ok("达到每日上限后第 3 次抛 QuotaExceeded", raises(lambda: qd.acquire(), QuotaExceeded))
    try:
        qd.acquire()
    except QuotaExceeded as e:
        ok("超 daily 时 kind=daily", e.kind == "daily")

    print("\n=== detect_signal ===")
    ok("HTTP 429 → rate_limited", detect_signal(status=429) == SIGNAL_RATE_LIMITED)
    ok("限流短语 → rate_limited", detect_signal(body="Please wait a few minutes before you try again") == SIGNAL_RATE_LIMITED)
    ok("/challenge → challenge", detect_signal(url="https://www.instagram.com/challenge/xyz") == SIGNAL_CHALLENGE)
    ok("/accounts/login → login_wall", detect_signal(url="https://www.instagram.com/accounts/login/") == SIGNAL_LOGIN_WALL)
    ok("空响应 → unexpected", detect_signal(body="   ") == SIGNAL_UNEXPECTED)
    ok("正常内容 → None", detect_signal(url="https://www.instagram.com/p/ABC/", body="some media json") is None)

    print("\n=== CooldownState ===")
    c = CooldownState(steps_min=[10, 20, 30, 40], max_minutes=360)  # secs=[600,1200,1800,2400]
    steps = [c.report(SIGNAL_RATE_LIMITED) for _ in range(5)]
    ok("阶梯退避 600/1200/1800/2400/2400(封顶)", steps == [600, 1200, 1800, 2400, 2400])

    c2 = CooldownState(steps_min=[10], max_minutes=360)
    ok("冷却前 check 不抛", not raises(lambda: c2.check(), CoolDown))
    c2.report(SIGNAL_RATE_LIMITED)
    ok("report 后 check 抛 CoolDown", raises(lambda: c2.check(), CoolDown))
    st = c2.status()
    ok("status.cooling_down=True", st["cooling_down"] is True)
    ok("status 带 reason", st["last_reason"] == SIGNAL_RATE_LIMITED)

    # 24h 无信号重置档位
    c3 = CooldownState(steps_min=[10, 20], max_minutes=360)
    c3.report(SIGNAL_RATE_LIMITED)  # level→1
    c3.report(SIGNAL_RATE_LIMITED)  # level→2（封顶）
    c3._last_signal_at = time.time() - 25 * 3600  # 模拟 25 小时无信号
    secs = c3.report(SIGNAL_RATE_LIMITED)  # 重置后从档位 0 开始 → 600
    ok("连续 24h 无信号后档位归零（重回 600s）", secs == 600)

    print(f"\n===== 结果：{_passed} 通过，{_failed} 失败 =====")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

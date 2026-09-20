"""auto-download-mode 单测：ActionScheduler（纯逻辑，不联网，不依赖 pytest）。

运行：python test_scheduler.py
"""
from __future__ import annotations

import asyncio
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from collector.quota import ActionScheduler, QuotaExceeded

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
    print("=== 日程生成（jittered grid） ===")
    s = ActionScheduler(window_seconds=3600, limit=300, min_gap_sec=8, jitter=0.35)
    slots = s._generate_slots(1000.0)
    ok("生成 300 个日程点", len(slots) == 300)
    gaps = [b - a for a, b in zip(slots, slots[1:])]
    ok("相邻间隔全部 ≥ min_gap", all(g >= 8.0 - 1e-9 for g in gaps))
    ok("间隔非固定等间隔", len({round(g, 6) for g in gaps}) > 10)
    ok("首点不早于 anchor", slots[0] >= 1000.0)
    ok("日程点单调递增", all(b > a for a, b in zip(slots, slots[1:])))

    s2 = ActionScheduler(window_seconds=60, limit=10, min_gap_sec=30, jitter=0.35)
    slots2 = s2._generate_slots(0.0)
    gaps2 = [b - a for a, b in zip(slots2, slots2[1:])]
    ok("min_gap 支配时强制钳制（30s）", all(g >= 30.0 - 1e-9 for g in gaps2))

    s3 = ActionScheduler(window_seconds=60, limit=0, min_gap_sec=8, jitter=0.35)
    ok("limit=0 → 无日程点", s3._generate_slots(0.0) == [])

    print("\n=== wait_for_slot：扣名额 / 窗口滚动 ===")
    sw = ActionScheduler(window_seconds=2, limit=4, min_gap_sec=0.05, jitter=0.3)

    async def consume(n: int):
        for _ in range(n):
            await sw.wait_for_slot()

    t0 = time.time()
    asyncio.run(consume(4))
    st = sw.status()
    ok("消耗 4 名额后窗口剩余 0", st["window_remaining"] == 0 and st["window_used"] == 4)
    gen_before = st["generation"]
    asyncio.run(sw.wait_for_slot())  # 第 5 次：名额尽 → 立即开新窗（不抛错）
    st = sw.status()
    ok("名额用尽立即滚动新窗（generation +1）", st["generation"] == gen_before + 1 and st["window_used"] == 1)
    ok("滚动发生在名义窗口附近（< 4s）", time.time() - t0 < 4)

    print("\n=== acquire_now：手动路径 ===")
    sa = ActionScheduler(window_seconds=3600, limit=2, min_gap_sec=0.1, jitter=0.3)
    sa.acquire_now()
    sa.acquire_now()
    ok("窗口满且未过期 → 抛 QuotaExceeded", raises(sa.acquire_now, QuotaExceeded))
    try:
        sa.acquire_now()
    except QuotaExceeded as e:
        ok("kind=session", e.kind == "session")

    sb = ActionScheduler(window_seconds=0.2, limit=1, min_gap_sec=0.05, jitter=0.3)
    sb.acquire_now()
    ok("过期前满窗拒绝", raises(sb.acquire_now, QuotaExceeded))
    time.sleep(0.3)
    ok("名义时长过后手动可开新窗", not raises(sb.acquire_now, QuotaExceeded))

    print("\n=== 手动推后自动日程（last_finish + min_gap） ===")
    sc = ActionScheduler(window_seconds=3600, limit=10, min_gap_sec=5, jitter=0.3)
    sc.acquire_now()   # 手动消耗尾端点（9 个剩余）
    ok("手动消耗最远端点（近端不受影响）", sc.status()["window_remaining"] == 9)
    sc.report_finish()
    st = sc.status()
    ok("report_finish 推进 last_finish", st["last_finish"] is not None)
    # wait_for_slot 的 floor = last_finish + min_gap → 首个动作至少在 finish+5s 后
    t_finish = st["last_finish"]

    async def first_slot_time() -> float:
        await sc.wait_for_slot()
        return time.time()

    start = asyncio.run(first_slot_time())
    ok("自动首动作 ≥ 手动完成 + min_gap", start >= t_finish + 5.0 - 0.05)

    print("\n=== invalidate：作废重排 ===")
    si = ActionScheduler(window_seconds=3600, limit=10, min_gap_sec=0.1, jitter=0.3)
    si.acquire_now()  # 先开窗
    old_anchor = si.status()["window_anchor"]
    si.invalidate()
    st = si.status()
    ok("作废后 anchor=None、名额恢复满额", st["window_anchor"] is None and st["window_remaining"] == 10)
    asyncio.run(si.wait_for_slot())  # 重新开窗取点
    st = si.status()
    ok("重新开窗生成新日程（anchor 前移）", st["window_anchor"] is not None and st["window_anchor"] >= old_anchor)

    print(f"\n===== 结果：{_passed} 通过，{_failed} 失败 =====")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

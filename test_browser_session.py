"""Phase 3 验证：browser_session（async 单例 + 串行锁 + lazy 启动），不联网。

运行：python test_browser_session.py
"""
from __future__ import annotations

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from collector.browser_session import session

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


async def holder(name: str, log: list[str], delay: float) -> None:
    async with session.page() as page:
        log.append(f"{name}:in")
        await page.goto("about:blank")
        await asyncio.sleep(delay)
        log.append(f"{name}:out")


async def main() -> int:
    print("=== lazy 启动 + 基本 page ===")
    ok("初始 context 为 None", session._context is None)
    async with session.page() as page:
        await page.goto("about:blank")
        await page.set_content("<title>xins-probe</title>")
        title = await page.title()
        ok("导航 + set_content + title 可用", title == "xins-probe")
    ok("lazy 启动后 context 已建立", session._context is not None)

    print("\n=== 串行锁（两个并发 page 持有者） ===")
    log: list[str] = []
    await asyncio.gather(holder("A", log, 0.2), holder("B", log, 0.1))
    print("  顺序:", log)
    names = [entry[0] for entry in log]
    serial = names in (["A", "A", "B", "B"], ["B", "B", "A", "A"])
    ok("两个持有者严格串行（无交错）", serial)

    print("\n=== stop 清理 ===")
    await session.stop()
    ok("stop 后 context 为 None", session._context is None)
    ok("stop 后 playwright 为 None", session._playwright is None)

    # 再次 lazy 启动（验证可重复）
    async with session.page() as page:
        await page.goto("about:blank")
    ok("stop 后可再次 lazy 启动", session._context is not None)
    await session.stop()

    print(f"\n===== 结果：{_passed} 通过，{_failed} 失败 =====")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

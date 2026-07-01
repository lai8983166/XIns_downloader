"""Phase 1 校验（async 版）：collector vs instaloader，同一帖子字段对比。

用法：python verify_phase1.py [shortcode]   # 默认 DY6NvmAk5J5
"""
from __future__ import annotations

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from collector.instagram_collector import CollectorError, preview_post
from collector.browser_session import session

DEFAULT = "DY6NvmAk5J5"


async def main() -> int:
    shortcode = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    print(f"===== collector (Playwright/async) shortcode={shortcode} =====")
    try:
        result = await preview_post(shortcode)
    except CollectorError as exc:
        print(f"❌ collector 失败: kind={exc.kind} status={exc.status} msg={exc}")
        await session.stop()
        return 1

    print(f"shortcode      : {result.shortcode}")
    print(f"owner_username : {result.owner_username}")
    print(f"caption(前60)  : {(result.caption or '')[:60]}")
    print(f"resources      : {len(result.resources)} 个")
    for item in result.resources:
        print(f"  [{item.index}] type={item.type} file={item.filename}")
        print(f"       url  : {item.url[:110]}")
        print(f"       thumb: {(item.thumbnail_url or '')[:110]}")

    print("\n===== 对比 instaloader 旧路径 =====")
    try:
        from main import preview_instagram_post

        old = preview_instagram_post(f"https://www.instagram.com/p/{shortcode}/")
        print(f"instaloader owner : {old.owner_username} | resources: {len(old.resources)}")
        same_owner = result.owner_username == old.owner_username
        same_count = len(result.resources) == len(old.resources)
        print(f"owner 一致: {same_owner} | resources 数量一致: {same_count}")
        print("✅ 字段一致" if (same_owner and same_count) else "⚠️ 存在差异（见上）")
    except Exception as exc:  # noqa: BLE001
        print(f"instaloader 对比失败（可能限流/未配会话）: {type(exc).__name__}: {str(exc)[:140]}")

    await session.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

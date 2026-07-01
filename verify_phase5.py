"""Phase 5 验证：preview_profile（首屏 + 加载更多 + 字段完整性）。

用法：python verify_phase5.py [profile]   # 默认 bellness__
"""
from __future__ import annotations

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from collector.browser_session import session
from collector.instagram_collector import CollectorError, preview_profile


async def show(p, label):
    print(f"\n--- {label} ---")
    print(f"username={p.username} | full_name={p.full_name}")
    print(f"profile_pic={'有' if p.profile_pic_url else '无'} | mediacount={p.mediacount} | is_private={p.is_private}")
    print(f"cursor={p.cursor} next_cursor={p.next_cursor} has_more={p.has_more} posts={len(p.posts)}")
    for post in p.posts[:3]:
        print(f"  [{post.index}] {post.shortcode} type={post.type} media_count={post.media_count} date={p.posts[0].date_utc[:10] if p.posts else ''}")
        print(f"       thumb={post.thumbnail_url[:80]}")


async def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else "bellness__"
    print(f"===== preview_profile({profile}) =====")
    try:
        p0 = await preview_profile(profile, cursor=0, limit=6)
        await show(p0, "首屏 cursor=0 limit=6")

        p6 = await preview_profile(profile, cursor=p0.next_cursor or 6, limit=6)
        await show(p6, f"加载更多 cursor={p6.cursor}（应命中缓存，不重新导航）")

        # 字段完整性
        sample = p0.posts[0] if p0.posts else None
        if sample:
            print("\n首帖字段检查:")
            print(f"  shortcode={sample.shortcode} | url={sample.url}")
            print(f"  owner={sample.owner_username} | caption前30={(sample.caption or '')[:30]}")
            print(f"  resources 数={len(sample.resources)}")
            for r in sample.resources[:2]:
                print(f"    resource[{r.index}] type={r.type} file={r.filename} url={r.url[:75]}")
        print("\n✅ Profile 采集正常" if p0.posts and p0.has_more else "⚠️ 结果异常")
    except CollectorError as exc:
        print(f"❌ 失败: kind={exc.kind} status={exc.status} msg={exc}")

    await session.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

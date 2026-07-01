"""探测 Profile 页数据结构（Phase 5 调研）。

捕获：1) 首屏 SSR HTML relay store（用户信息 + 首批帖子）；
      2) 滚动触发的翻页网络响应（后续帖子 + cursor）。

用法：python probe_profile.py [username]   # 默认 bellness__
"""
from __future__ import annotations

import json
import sys

from patchright.sync_api import sync_playwright

from collector.config import PROJECT_ROOT, settings

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "bellness__"
URL = f"https://www.instagram.com/{USERNAME}/"
PROBE_DIR = PROJECT_ROOT / ".browser"

# 翻页/帖子相关关键字（用于过滤有意义的 JSON 响应）
TIMELINE_HINTS = (
    "timeline_media",
    "edge_owner_to_timeline",
    "xdt_api__v1__feed",
    "user_timeline",
    "PolarisProfile",
    "xdt_profile_timeline",
)


def main() -> int:
    print(f"[probe-profile] user={USERNAME} url={URL}")
    resp_count = 0

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            nonlocal resp_count
            try:
                ctype = resp.headers.get("content-type", "")
                if "json" not in ctype.lower():
                    return
                body = resp.text()
            except Exception:
                return
            if not any(h in body for h in TIMELINE_HINTS):
                return
            resp_count += 1
            out = PROBE_DIR / f"probe_profile_{USERNAME}_resp{resp_count}.json"
            try:
                out.write_text(json.dumps(json.loads(body), ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                out.write_text(body, encoding="utf-8")
            print(f"  [resp#{resp_count}] status={resp.status} len={len(body)}")
            print(f"           url={resp.url[:130]}")
            print(f"           -> {out}")

        page.on("response", on_response)
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        html = page.content()  # 首屏

        print("[probe-profile] 滚动触发翻页...")
        for _ in range(4):
            page.mouse.wheel(0, 3500)
            page.wait_for_timeout(2500)
        page.wait_for_timeout(2000)
        final_url = page.url
        ctx.close()

    html_path = PROBE_DIR / f"probe_profile_{USERNAME}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"\n[probe-profile] 首屏 HTML -> {html_path} (len={len(html)})")
    print(f"[probe-profile] 翻页响应命中: {resp_count}")
    print(f"[probe-profile] final_url={final_url}")
    print("[probe-profile] HTML 关键字段计数:")
    for fld in (
        "media_count",
        "profile_pic_url",
        "is_private",
        "full_name",
        "timeline_media",
        "edge_owner_to_timeline",
        "xdt_api",
        "paging",
        "has_next_page",
        "end_cursor",
    ):
        print(f"  {fld}: {html.count(fld)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

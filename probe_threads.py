"""探测 Threads profile 页数据结构（Threads 采集前置调研）。

捕获：1) 首屏 SSR HTML；2) 滚动触发的网络响应（帖子 timeline + cursor + 媒体字段）。

用法：python probe_threads.py [username]   # 默认 zuck
"""
from __future__ import annotations

import json
import sys

from patchright.sync_api import sync_playwright

from collector.config import PROJECT_ROOT, settings

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "zuck"
URL = f"https://www.threads.net/@{USERNAME}"
PROBE_DIR = PROJECT_ROOT / ".browser"


def main() -> int:
    print(f"[probe-threads] user={USERNAME} url={URL}")
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
            # 只关注含 threads/timeline/media/user 相关的响应
            if not any(h in body for h in ("thread", "text_post", "timeline", '"user"', "media", "paging")):
                return
            resp_count += 1
            out = PROBE_DIR / f"probe_threads_{USERNAME}_resp{resp_count}.json"
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
        html = page.content()

        print("[probe-threads] 滚动触发翻页...")
        for _ in range(4):
            page.mouse.wheel(0, 3500)
            page.wait_for_timeout(2500)
        page.wait_for_timeout(2000)
        final_url = page.url
        ctx.close()

    html_path = PROBE_DIR / f"probe_threads_{USERNAME}.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"\n[probe-threads] 首屏 HTML -> {html_path} (len={len(html)})")
    print(f"[probe-threads] 翻页响应命中: {resp_count}")
    print(f"[probe-threads] final_url={final_url}")
    print("[probe-threads] HTML 关键字段计数:")
    for fld in (
        "thread_items", "text_post_app", "image_versions", "video_versions",
        "taken_at", "caption", '"user"', "paging", "cursor", "has_next",
        "media_count", "profile_pic_url", "is_private",
    ):
        print(f"  {fld}: {html.count(fld)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

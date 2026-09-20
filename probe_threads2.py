"""深入调研 Threads 翻页机制（首屏 mediaData 拿到了，但滚动没触发翻页 graphql）。

尝试：scrollTo 页底多次 + 等更久；捕获所有 threads graphql/query 响应，看翻页请求。
另外记录 page_info.end_cursor，看翻页是否用 cursor 参数。

用法：python probe_threads2.py [username]   # 默认 zuck
"""
from __future__ import annotations

import json
import sys

from patchright.sync_api import sync_playwright

from collector.config import PROJECT_ROOT, settings

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "zuck"
URL = f"https://www.threads.com/@{USERNAME}"
PROBE_DIR = PROJECT_ROOT / ".browser"


def main() -> int:
    print(f"[probe2-threads] user={USERNAME} url={URL}")
    resp_log = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            try:
                ctype = resp.headers.get("content-type", "")
                if "json" not in ctype.lower():
                    return
                body = resp.text()
            except Exception:
                return
            if "graphql" not in resp.url or "mediaData" not in body:
                return
            try:
                data = json.loads(body)
            except Exception:
                return
            md = (data.get("data") or {}).get("mediaData") or {}
            edges = md.get("edges") or []
            pi = md.get("page_info") or {}
            resp_log.append({
                "url": resp.url[:160],
                "edges": len(edges),
                "has_next": pi.get("has_next_page"),
                "end_cursor": (pi.get("end_cursor") or "")[:40],
            })

        page.on("response", on_response)
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        print(f"  首屏后响应数: {len(resp_log)}")

        # scrollTo 页底 + 等久，多次
        for i in range(8):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(3000)
            print(f"  scrollTo #{i+1} 后累计响应数: {len(resp_log)}")

        # 也试 mouse.wheel
        for i in range(3):
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(2500)
        print(f"  wheel 后累计响应数: {len(resp_log)}")

        final_url = page.url
        ctx.close()

    print(f"\n[probe2-threads] final_url={final_url}")
    print(f"[probe2-threads] graphql/mediaData 响应总数: {len(resp_log)}")
    for i, r in enumerate(resp_log):
        print(f"  resp#{i+1}: edges={r['edges']} has_next={r['has_next']} cursor={r['end_cursor']}")
    if len(resp_log) <= 1:
        print("\n⚠️ 滚动未触发翻页 graphql——threads 翻页可能用别的机制（按钮/不同滚动/cursor 重放）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

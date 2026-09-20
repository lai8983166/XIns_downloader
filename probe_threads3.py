"""调研 Threads graphql 请求结构（query hash + variables + cursor），为翻页重放做准备。

捕获首屏 graphql/query 的请求（method + url + post_data），看：
- query hash / documentId / operationName
- variables 里的 cursor / after 参数

用法：python probe_threads3.py [username]   # 默认 zuck
"""
from __future__ import annotations

import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from patchright.sync_api import sync_playwright

from collector.config import settings

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "zuck"
URL = f"https://www.threads.com/@{USERNAME}"


def main() -> int:
    print(f"[probe3-threads] {URL}")
    requests_log = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            if "graphql" not in req.url:
                return
            try:
                post_data = req.post_data
            except Exception:
                post_data = None
            requests_log.append({
                "method": req.method,
                "url": req.url,
                "post_data": post_data,
            })

        page.on("request", on_request)
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        ctx.close()

    print(f"\ngraphql 请求总数: {len(requests_log)}")
    for i, r in enumerate(requests_log[:6]):
        print(f"\n--- req#{i+1} ---")
        print(f"  method: {r['method']}")
        print(f"  url: {r['url'][:200]}")
        pd = r["post_data"]
        if pd:
            print(f"  post_data (raw, 前400): {pd[:400]}")
            try:
                from urllib.parse import parse_qs
                parsed = parse_qs(pd)
                for k in ("fb_api_req_friendly_name", "doc_id", "query_id", "variables"):
                    if k in parsed:
                        print(f"  {k}: {parsed[k][0][:400]}")
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""验证 Threads 翻页重放（带首屏 headers + cookies）。

捕获首屏 graphql/query 的 headers + post_data + end_cursor，
用 context.request（继承 cookies）+ 复制 headers 重放翻页。

用法：python probe_threads5.py [username]   # 默认 zuck
"""
from __future__ import annotations

import json
import sys
from urllib.parse import parse_qs, urlencode

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from patchright.sync_api import sync_playwright

from collector.config import settings

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "zuck"
URL = f"https://www.threads.com/@{USERNAME}"
GRAPHQL_URL = "https://www.threads.com/graphql/query"


def main() -> int:
    print(f"[probe5-threads] {URL}")
    first = {"headers": None, "post_data": None}
    end_cursor = {"v": None}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            if first["headers"] is None and "graphql/query" in req.url:
                pd = req.post_data or ""
                if "BarcelonaProfileThreadsTab" in pd:
                    first["headers"] = dict(req.headers)
                    first["post_data"] = pd

        def on_response(resp):
            if end_cursor["v"] is not None or "graphql/query" not in resp.url:
                return
            try:
                data = json.loads(resp.text())
                md = (data.get("data") or {}).get("mediaData") or {}
                pi = md.get("page_info") or {}
                if pi.get("end_cursor"):
                    end_cursor["v"] = pi.get("end_cursor")
                    print(f"  首屏 edges={len(md.get('edges') or [])} end_cursor={(end_cursor['v'] or '')[:32]}...")
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # 打印首屏关键 headers
        if first["headers"]:
            interesting = {k: v for k, v in first["headers"].items() if k.lower() in (
                "x-ig-app-id", "x-asbd-id", "x-fb-http-header", "x-bloks-version-id",
                "sec-fetch-mode", "origin", "referer", "x-ig-www-claim",
            )}
            print(f"  首屏关键 headers: {interesting}")

        if not first["post_data"] or not end_cursor["v"]:
            print("❌ 未捕获首屏请求或 end_cursor")
            ctx.close()
            return 1

        pd = parse_qs(first["post_data"])
        variables = json.loads(pd["variables"][0])
        variables["after"] = end_cursor["v"]
        pd["variables"] = [json.dumps(variables)]
        new_body = urlencode({k: v[0] for k, v in pd.items()})

        print(f"\n重放翻页（context.request + 首屏 headers）...")
        resp = ctx.request.post(GRAPHQL_URL, data=new_body, headers=first["headers"])
        text = resp.text()
        print(f"  status={resp.status} len={len(text)}")
        try:
            data = json.loads(text)
            md = (data.get("data") or {}).get("mediaData") or {}
            edges = md.get("edges") or []
            pi = md.get("page_info") or {}
            print(f"  ✅ 翻页 edges={len(edges)} has_next={pi.get('has_next_page')}")
            if edges:
                tis = (edges[0].get("node") or {}).get("thread_items") or [{}]
                print(f"  翻页首帖 code={(tis[0].get('post') or {}).get('code')}")
        except Exception:
            print(f"  ❌ 非 JSON，前300: {text[:300]}")

        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

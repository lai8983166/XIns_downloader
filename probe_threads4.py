"""验证 Threads 翻页重放是否可行。

1. 捕获首屏 graphql/query 请求的 post_data + 响应的 end_cursor + userID。
2. 用 page.evaluate 在浏览器内 fetch 重放（post_data 的 variables.after 改成 end_cursor）。
3. 看是否拿到翻页 mediaData（新 edges）。

用法：python probe_threads4.py [username]   # 默认 zuck
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
    print(f"[probe4-threads] {URL}")
    first_post_data = {"v": None}
    end_cursor = {"v": None}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            if first_post_data["v"] is None and "graphql/query" in req.url:
                pd = req.post_data or ""
                if "BarcelonaProfileThreadsTab" in pd:
                    first_post_data["v"] = pd

        def on_response(resp):
            if end_cursor["v"] is not None or "graphql/query" not in resp.url:
                return
            try:
                body = resp.text()
                data = json.loads(body)
                md = (data.get("data") or {}).get("mediaData") or {}
                pi = md.get("page_info") or {}
                if pi.get("end_cursor"):
                    end_cursor["v"] = pi.get("end_cursor")
                    edges = md.get("edges") or []
                    print(f"  首屏 edges={len(edges)} end_cursor={(end_cursor['v'] or '')[:32]}...")
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        if not first_post_data["v"] or not end_cursor["v"]:
            print("❌ 未捕获到首屏请求或 end_cursor")
            ctx.close()
            return 1

        # 重放：variables.after 改成 end_cursor
        pd = parse_qs(first_post_data["v"])
        variables = json.loads(pd["variables"][0])
        variables["after"] = end_cursor["v"]
        pd["variables"] = [json.dumps(variables)]
        new_body = urlencode({k: v[0] for k, v in pd.items()})

        print(f"\n重放翻页（after={end_cursor['v'][:32]}...）...")
        result = page.evaluate(
            """async (body) => {
                const r = await fetch(window.location.origin + '/graphql/query', {
                    method: 'POST',
                    headers: {'content-type': 'application/x-www-form-urlencoded'},
                    body,
                    credentials: 'include',
                });
                return {status: r.status, text: await r.text()};
            }""",
            new_body,
        )
        print(f"  status={result['status']} len={len(result['text'])}")
        try:
            data = json.loads(result["text"])
            md = (data.get("data") or {}).get("mediaData") or {}
            edges = md.get("edges") or []
            pi = md.get("page_info") or {}
            print(f"  ✅ 翻页 edges={len(edges)} has_next={pi.get('has_next_page')} new_cursor={(pi.get('end_cursor') or '')[:32]}...")
            if edges:
                node = edges[0].get("node") or {}
                tis = node.get("thread_items") or [{}]
                code = (tis[0].get("post") or {}).get("code")
                print(f"  翻页首帖 code={code}")
        except Exception as exc:
            print(f"  ❌ 解析失败: {exc}")
            print(f"  body 前300: {result['text'][:300]}")

        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

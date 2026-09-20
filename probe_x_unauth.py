"""未登录 probe X（原推特），验证登录墙 + 抓 SSR/网络证据。

回答四个问题：
1. 访问 x.com 是否被重定向到 /i/flow/login？
2. SSR HTML 里有没有 __INITIAL_STATE__ / 媒体 URL / og:image？
3. 触发了哪些 GraphQL / i/api 请求？状态码？
4. 当前 browser profile 是否已登录 X（看 auth_token / ct0 cookie）？

用法：
    python probe_x_unauth.py                       # 默认 probe profile
    python probe_x_unauth.py https://x.com/elonmusk/status/123...
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from patchright.sync_api import sync_playwright

from collector.config import settings

URL = sys.argv[1] if len(sys.argv) > 1 else "https://x.com/elonmusk"


def main() -> int:
    print(f"[probe-x-unauth] {URL}")
    print(f"[probe-x-unauth] profile dir = {settings.browser_user_data_dir}")
    print()

    events: list[dict] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            u = resp.url.lower()
            if "twitter.com" in u or "x.com" in u:
                if any(k in u for k in ("graphql", "/i/api", "/i/flow", "/login", "guest")):
                    events.append({
                        "type": "resp",
                        "status": resp.status,
                        "url": resp.url[:220],
                        "ct": (resp.headers.get("content-type") or "")[:50],
                    })

        page.on("response", on_response)

        try:
            resp = page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"❌ 导航失败: {type(e).__name__}: {e}")
            ctx.close()
            return 1

        print("=== 导航结果 ===")
        print(f"  初始 URL : {URL}")
        print(f"  最终 URL : {page.url}")
        print(f"  HTTP 状态: {resp.status if resp else '?'}")
        login_wall = "/login" in page.url or "/i/flow" in page.url
        print(f"  命中登录墙: {login_wall}")
        print()

        page.wait_for_timeout(5000)

        html = page.content()
        print(f"=== HTML 概览（len={len(html)}） ===")
        markers = [
            "__INITIAL_STATE__",
            "__NEXT_DATA__",
            "Sign in to X",
            "Log in to X",
            "For years",                # X 主页 placeholder 文案
            "pbs.twimg.com",
            "video.twimg.com",
            "pmd.raven.app",
            '"require_auth"',
        ]
        for m in markers:
            count = html.count(m)
            tag = "✅" if count > 0 else "❌"
            snippet = ""
            if count > 0:
                idx = html.find(m)
                snippet = html[max(0, idx - 30):idx + 70].replace("\n", " ")
                snippet = "  例：…" + snippet + "…"
            print(f"  {tag} '{m}' × {count}{snippet}")
        print()

        og_img = re.search(r'<meta property="og:image"[^>]*content="([^"]+)"', html)
        og_title = re.search(r'<meta property="og:title"[^>]*content="([^"]+)"', html)
        print("=== og 标签 ===")
        print(f"  og:image: {og_img.group(1) if og_img else '❌ 无'}")
        print(f"  og:title: {og_title.group(1) if og_title else '❌ 无'}")
        print()

        print(f"=== 关键网络事件（共 {len(events)}） ===")
        for e in events[:40]:
            print(f"  {e['status']:3} {e['ct']:28} {e['url']}")
        if len(events) > 40:
            print(f"  ... 还有 {len(events) - 40} 条未显示")
        print()

        try:
            body_text = page.inner_text("body")
            print("=== 页面前 400 字符可见文本 ===")
            print(body_text[:400])
        except Exception as e:
            print(f"  ❌ 读取 body 文本失败: {e}")
        print()

        cookies = ctx.cookies(["https://x.com", "https://twitter.com"])
        auth_cookies = [c for c in cookies if c.get("name") in ("auth_token", "ct0", "twid")]
        print("=== X 登录态 cookies ===")
        for c in auth_cookies:
            v = c.get("value", "")
            print(f"  {c['name']}: {v[:24]}{'...' if len(v) > 24 else ''}")
        if not auth_cookies:
            print("  ❌ 无 auth_token / ct0 / twid → 确认未登录")
        print()

        out_dir = Path("openspec")
        out_dir.mkdir(exist_ok=True)
        (out_dir / "probe_x_unauth.html").write_text(html, encoding="utf-8")
        (out_dir / "probe_x_unauth_events.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("已保存: openspec/probe_x_unauth.html, openspec/probe_x_unauth_events.json")

        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

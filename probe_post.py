"""探测 Instagram 单帖页的网络响应结构（Phase 1 调研 v2）。

v2 改进：不过滤关键字，记录所有 JSON 响应，定位含目标 shortcode 的那个 endpoint。
"""
from __future__ import annotations

import json
import sys

from patchright.sync_api import sync_playwright

from collector.config import PROJECT_ROOT, settings

DEFAULT_SHORTCODE = "DY6NvmAk5J5"
PROBE_DIR = PROJECT_ROOT / ".browser"


def main() -> int:
    shortcode = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SHORTCODE
    url = f"https://www.instagram.com/p/{shortcode}/"
    print(f"[probe] shortcode={shortcode} url={url}")

    all_count = 0
    sc_hits = 0

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            nonlocal all_count, sc_hits
            try:
                ctype = resp.headers.get("content-type", "")
                if "json" not in ctype.lower():
                    return
                body = resp.text()
            except Exception:
                return
            all_count += 1
            has_sc = shortcode in body
            tag = " ★HAS-SHORTCODE" if has_sc else ""
            print(f"  [json#{all_count}] has_sc={has_sc} status={resp.status} len={len(body)}{tag}")
            print(f"             url={resp.url[:140]}")
            if has_sc:
                sc_hits += 1
                out = PROBE_DIR / f"probe_{shortcode}_sc{sc_hits}.json"
                try:
                    out.write_text(json.dumps(json.loads(body), ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    out.write_text(body, encoding="utf-8")
                print(f"             -> {out}")

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_timeout(7000)
        except Exception:
            pass
        final_url = page.url
        ctx.close()

    print(f"\n[probe] 共 {all_count} 个 JSON 响应，其中含 shortcode 的 {sc_hits} 个。final_url={final_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

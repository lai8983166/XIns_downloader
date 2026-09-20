"""登录态 probe X：探查 graphql 端点 + 必要 headers + 视频 mp4 + 翻页 cursor。

前置：先用 login_x.py 完成 X 登录。

回答五个问题：
1. 是否登录态（cookies: auth_token / ct0 / twid）
2. Profile / 单帖 触发了哪些 graphql operationName（UserTweets / TweetDetail / ...）
3. 关键请求 headers 清单（authorization / x-csrf-token / x-twitter-active-user / ...）
4. 视频帖的 mp4 variants 直链（不同码率/分辨率）
5. 翻页 cursor 机制（cursor-top / cursor-bottom / 自定义串）

产物：
- openspec/probe_x_auth_summary.json   完整摘要（含每条请求的 headers + post_data + 响应摘要）

用法：
    python probe_x_auth.py [username]   # 默认 elonmusk
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from patchright.sync_api import sync_playwright

from collector.config import settings

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "elonmusk"
OUT_DIR = Path("openspec")

# 关心的请求 headers（小写匹配）
INTERESTING_HEADERS = (
    "authorization",
    "x-csrf-token",                 # 应等于 ct0 cookie
    "x-twitter-auth-type",
    "x-twitter-active-user",
    "x-twitter-client-language",
    "x-client-transaction-id",      # X 反爬虫关键 header
    "content-type",
    "referer",
    "sec-ch-ua",
)
GRAPHQL_PREFIX = "/i/api/graphql/"


def is_graphql(url: str) -> bool:
    return GRAPHQL_PREFIX in url


def summarize_response(data: dict) -> dict:
    """从 graphql 响应里抽媒体 + cursor 关键证据（足够支撑后续设计）。"""
    text = json.dumps(data, ensure_ascii=False)
    video_variants: list[dict] = []
    image_urls: list[str] = []
    cursors: list[str] = []

    # 视频 variants 块（典型：{"bitrate": N, "content_type": "...", "url": "..."}）
    for vm in re.finditer(r'\{[^{}]*"content_type"[^{}]*"url"[^{}]*\}', text):
        block = vm.group(0)
        url_m = re.search(r'"url":\s*"([^"]+)"', block)
        ct_m = re.search(r'"content_type":\s*"([^"]+)"', block)
        br_m = re.search(r'"bitrate":\s*(\d+)', block)
        if url_m and "video.twimg.com" in url_m.group(1):
            video_variants.append({
                "url": url_m.group(1).replace("\\/", "/"),
                "content_type": ct_m.group(1) if ct_m else None,
                "bitrate": int(br_m.group(1)) if br_m else None,
            })

    # 图片直链
    for m in re.finditer(r'"(https://pbs\.twimg\.com/media/[^"]+)"', text):
        url = m.group(1).replace("\\/", "/").replace("&amp;", "&")
        if url not in image_urls:
            image_urls.append(url)

    # cursor（X 的 cursor 命名通常是 "cursor-top-XXX" / "cursor-bottom-XXX"，或长 base64 串）
    for m in re.finditer(r'"value":\s*"([^"]{16,256})"', text):
        v = m.group(1).replace("\\/", "/")
        if "cursor" in v or re.fullmatch(r"[A-Za-z0-9+/_=%-]{16,}", v):
            if v not in cursors:
                cursors.append(v)

    # 去重 + 限量
    video_variants = list({v["url"]: v for v in video_variants}.values())[:10]
    image_urls = list(dict.fromkeys(image_urls))[:15]
    cursors = cursors[:5]
    return {"video_variants": video_variants, "image_urls": image_urls, "cursors": cursors}


def main() -> int:
    print("=" * 60)
    print(f" probe-x-auth    username = {USERNAME}")
    print("=" * 60)
    OUT_DIR.mkdir(exist_ok=True)

    captured: list[dict] = []
    phase = {"v": "init"}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )

        # 1. 登录态检查
        cookies = ctx.cookies(["https://x.com", "https://twitter.com"])
        auth_cookies = {c["name"]: c["value"] for c in cookies
                        if c["name"] in ("auth_token", "ct0", "twid")}
        print("=== 登录态 ===")
        for k in ("auth_token", "ct0", "twid"):
            v = auth_cookies.get(k)
            print(f"  {k}: {(v[:24] + '...') if v else '❌ 缺失'}")
        if not auth_cookies:
            print("\n❌ 未检测到 X 登录态。请先运行: python login_x.py")
            ctx.close()
            return 1
        print()

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            if not is_graphql(req.url):
                return
            try:
                pd = req.post_data or ""
            except Exception:
                pd = ""
            parsed = urlparse(req.url)
            # graphql URL 形态：/i/api/graphql/{queryId}/{operationName}
            m = re.search(r"/graphql/([^/]+)/([^/?]+)", parsed.path)
            captured.append({
                "phase": phase["v"],
                "operation": m.group(2) if m else "?",
                "query_id": m.group(1) if m else "?",
                "method": req.method,
                "url": req.url[:200],
                "post_data": pd[:1500],
                "headers": {k: v for k, v in req.headers.items()
                            if k.lower() in INTERESTING_HEADERS},
                "resp_status": None,
                "resp_size": None,
                "resp_summary": None,
            })

        def on_response(resp):
            if not is_graphql(resp.url):
                return
            try:
                body = resp.text()
                data = json.loads(body)
            except Exception:
                return
            # 匹配最近一条同 URL 且未填响应的 request
            for cap in reversed(captured):
                if cap["url"] == resp.url[:200] and cap["resp_status"] is None:
                    cap["resp_status"] = resp.status
                    cap["resp_size"] = len(body)
                    cap["resp_summary"] = summarize_response(data)
                    break

        page.on("request", on_request)
        page.on("response", on_response)

        # 2. Profile
        phase["v"] = "profile"
        profile_url = f"https://x.com/{USERNAME}"
        print(f"=== Phase 1: Profile   {profile_url} ===")
        try:
            resp = page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            print(f"  最终 URL: {page.url}   HTTP {resp.status if resp else '?'}")
        except Exception as e:
            print(f"  ❌ 导航失败: {e}")
        page.wait_for_timeout(5000)

        # 从首屏 SSR 抽一个 status URL，用于单帖 phase
        status_url = None
        try:
            html = page.content()
            m = re.search(rf"/{USERNAME}/status/(\d{{15,}})", html)
            if m:
                status_url = f"https://x.com/{USERNAME}/status/{m.group(1)}"
        except Exception:
            pass

        # 3. 单帖
        if status_url:
            phase["v"] = "post"
            print(f"\n=== Phase 2: Post   {status_url} ===")
            try:
                resp = page.goto(status_url, wait_until="domcontentloaded", timeout=60000)
                print(f"  最终 URL: {page.url}   HTTP {resp.status if resp else '?'}")
            except Exception as e:
                print(f"  ❌ 导航失败: {e}")
            page.wait_for_timeout(5000)
        else:
            print("\n（未在 SSR 中找到 status URL，跳过单帖 phase）")

        ctx.close()

    # 4. 汇总
    print("\n" + "=" * 60)
    print(f" 捕获 {len(captured)} 条 graphql 请求")
    print("=" * 60)

    by_op: dict[str, int] = {}
    for c in captured:
        by_op[c["operation"]] = by_op.get(c["operation"], 0) + 1
    print("\n=== operation 分布 ===")
    for op, n in sorted(by_op.items(), key=lambda x: -x[1]):
        print(f"  {n:3}  {op}")

    print("\n=== 详情（前 12 条） ===")
    for i, c in enumerate(captured[:12]):
        print(f"  [{i:02}] {c['phase']:8} {c['operation']:28} "
              f"status={c.get('resp_status') or '-'} size={c.get('resp_size') or '-'}")

    if captured:
        print("\n=== 关键 headers（取自首条 graphql 请求） ===")
        for k, v in captured[0]["headers"].items():
            vs = v if len(v) <= 80 else v[:80] + "..."
            print(f"  {k}: {vs}")

    # 5. 媒体证据聚合
    print("\n=== 媒体证据（聚合全部响应） ===")
    all_videos: list[dict] = []
    all_images: list[str] = []
    all_cursors: list[str] = []
    for c in captured:
        s = c.get("resp_summary") or {}
        all_videos.extend(s.get("video_variants", []))
        all_images.extend(s.get("image_urls", []))
        all_cursors.extend(s.get("cursors", []))
    all_videos = list({v["url"]: v for v in all_videos}.values())
    all_images = list(dict.fromkeys(all_images))
    all_cursors = list(dict.fromkeys(all_cursors))

    print(f"  视频直链（mp4 variants）: {len(all_videos)}")
    for v in all_videos[:5]:
        print(f"    {v.get('bitrate') or '?'} bps / {v.get('content_type') or '?'}")
        print(f"      {v['url'][:140]}")
    print(f"  图片直链: {len(all_images)}")
    for u in all_images[:3]:
        print(f"    {u[:140]}")
    print(f"  cursor 候选: {len(all_cursors)}")
    for cursor in all_cursors[:3]:
        print(f"    {cursor[:120]}")

    # 6. 保存
    out = OUT_DIR / "probe_x_auth_summary.json"
    out.write_text(
        json.dumps({
            "username": USERNAME,
            "cookies_present": list(auth_cookies.keys()),
            "operation_distribution": by_op,
            "captured": captured,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n已保存完整摘要 → {out}")
    print("后续可据此实现 collector/x_collector.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

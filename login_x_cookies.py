"""X cookie 注入登录 —— 自动化浏览器被"限制登录"时的替代路径。

背景（实测 2026-10）：X 对自动化控制下的浏览器在【登录环节】有严格指纹检测，
login_x.py 弹出的窗口登录会被限制；自己的日常浏览器登录正常。
auth_token 是设备无关的长期会话令牌（与 ct0 成对），在正常浏览器登录后
注入采集 profile 即可跳过登录关卡，后续无头采集复用。

取值步骤（日常浏览器，登录小号后）：
    F12 → Application（应用）→ Cookies → https://x.com
    找 auth_token / ct0 / twid 三行，复制 Value 备用。

用法（在你自己的终端运行；cookie 值只进本机 .browser/，不进对话/仓库）：
    python login_x_cookies.py
"""
from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from patchright.sync_api import sync_playwright

from collector.config import settings

X_HOME = "https://x.com/home"


def _ask(name: str, required: bool = True) -> str:
    v = input(f"{name}: ").strip().strip('"').strip("'")
    if required and not v:
        raise SystemExit(f"❌ {name} 必填")
    return v


def main() -> int:
    print("=" * 60)
    print(" X cookie 注入（auth_token / ct0 [/ twid]）")
    print("=" * 60)
    print("值来源：日常浏览器 F12 → Application → Cookies → https://x.com")
    print("-" * 60)
    auth_token = _ask("auth_token")
    ct0 = _ask("ct0")
    twid = _ask("twid（可留空）", required=False)

    one_year = int(time.time()) + 365 * 24 * 3600
    cookies = [
        {"name": "auth_token", "value": auth_token, "domain": ".x.com", "path": "/",
         "httpOnly": True, "secure": True, "expires": one_year},
        {"name": "ct0", "value": ct0, "domain": ".x.com", "path": "/",
         "secure": True, "expires": one_year},
    ]
    if twid:
        cookies.append({"name": "twid", "value": twid, "domain": ".x.com", "path": "/",
                        "secure": True, "expires": one_year})

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_user_data_dir),
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        ctx.add_cookies(cookies)

        # 验证：导航 home，无效会话会被重定向到 /login
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(X_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        final_url = page.url
        after = {c["name"] for c in ctx.cookies(["https://x.com"])}
        ctx.close()

    login_ok = "auth_token" in after and "/login" not in final_url and "/i/flow" not in final_url
    print("-" * 60)
    print(f"最终 URL: {final_url}")
    print(f"auth_token 注入后仍存在: {'auth_token' in after}")
    if login_ok:
        print("✅ 注入成功，登录态有效。下一步：python probe_x_auth.py")
        return 0
    if "/login" in final_url or "/i/flow" in final_url:
        print("❌ 会话被重定向到登录页：cookie 可能复制不完整或已失效，重新取值再试。")
    else:
        print("⚠️ 未能确认登录态（可能触发了人机验证页）。可跑 python probe_x_auth.py 看详细状态。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

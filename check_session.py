"""登录态复用检查（Phase 0，任务 1.6 验证用）。

无头复用 .browser/profile（与 login.py 同一个 userDataDir），打开 Instagram 首页，
判断登录态是否仍有效。判定：cookies 含非空 sessionid 且未被重定向到登录页。

用法
    python check_session.py
"""
from __future__ import annotations

import sys

from patchright.sync_api import sync_playwright

from collector.config import settings

INSTAGRAM_HOME = "https://www.instagram.com/"


def main() -> int:
    user_data_dir = settings.browser_user_data_dir
    if not user_data_dir.exists():
        print(f"[check] 未找到浏览器配置目录：{user_data_dir}")
        print("[check] 请先运行 python login.py 完成一次性登录。")
        return 1

    print(f"[check] 复用配置目录：{user_data_dir}")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(INSTAGRAM_HOME, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            print(f"[check] 打开 Instagram 失败：{exc}")
            context.close()
            return 2

        url = page.url
        cookies = context.cookies()
        context.close()

    sessionid = next(
        (c for c in cookies if c.get("name") == "sessionid" and c.get("value")),
        None,
    )
    redirected_to_login = "accounts/login" in url or url.rstrip("/").endswith("/login")

    if sessionid and not redirected_to_login:
        print(f"[check] ✅ 登录态有效（sessionid 存在，当前 URL: {url}）")
        return 0

    print(f"[check] ❌ 未检测到有效登录态（sessionid={'有' if sessionid else '无'}, URL={url}）")
    print("[check] 请重新运行 python login.py 登录。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

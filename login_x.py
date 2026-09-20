"""一次性有头登录工具 —— X（原推特）专用。

用途
    用【小号】在真实浏览器里手动登录 X 一次，登录态持久化到
    .browser/profile（userDataDir），与 Instagram 登录态共存（cookie 按域名隔离）。
    后续采集器以无头模式复用同一 user_data_dir。

重要
    - 只用专用小号，绝不登录主账号（硬约束：账号不得被封禁）
    - 自动化填账号密码是高危封号行为，禁止。登录只能由人工完成
    - .browser/ 内含登录态等敏感数据，不提交仓库不外传（已写进 .gitignore）

用法
    python login_x.py

可重复运行：再次执行复用同一 user_data_dir；若已是登录态，直接回车保存即可。
"""
from __future__ import annotations

import sys

from patchright.sync_api import sync_playwright

from collector.config import settings

X_HOME = "https://x.com/"


def main() -> int:
    user_data_dir = settings.browser_user_data_dir
    user_data_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(" X 一次性登录工具（专用小号）")
    print("=" * 60)
    print(f"浏览器配置目录 (userDataDir): {user_data_dir}")
    print("（与 Instagram 登录态共享此目录，cookie 按域名隔离，互不影响）")
    print("-" * 60)
    print("即将打开浏览器。请【只用小号】完成登录（含 2FA / 人机验证）。")
    print("切勿登录你的 X 主账号。")
    print("登录成功（页面显示已登录的 home timeline）后，回到本终端按回车保存。")
    print("-" * 60)

    with sync_playwright() as p:
        # persistent context：状态自动写入 user_data_dir，下次无头运行即可复用
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,  # 必须有头，才能人工登录
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(X_HOME, wait_until="domcontentloaded")

        try:
            input(">>> 完成登录后，按回车保存登录态并退出（Ctrl+C 取消，不保存）... ")
        except EOFError:
            print("\n[login_x] 未检测到交互输入，请在终端中直接运行 python login_x.py")
            context.close()
            return 1
        except KeyboardInterrupt:
            print("\n[login_x] 已取消，不保存登录态。")
            context.close()
            return 130

        context.close()

    print("[login_x] 完成。后续采集器将复用该登录态。")
    print("[login_x] 下一步：python probe_x_auth.py   验证登录态 + 抓 graphql 端点")
    return 0


if __name__ == "__main__":
    sys.exit(main())

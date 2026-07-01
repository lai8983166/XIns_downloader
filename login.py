"""一次性有头登录工具（Phase 0，任务 1.5）。

用途
    用【专用备用账号】在真实浏览器里手动登录 Instagram 一次，把登录态持久化到
    .browser/profile（userDataDir），并导出一份 .browser/state.json（storage_state）作备份。
    之后采集器以无头模式复用同一 user_data_dir，免再次登录。

为什么必须手动
    自动化登录（自动填账号密码并提交）是高危封号行为，本方案禁止（见 account-safety 规格）。
    登录只能由人工完成一次（含 2FA / 人机验证 / 挑战页）。

重要
    - 只用专用备用账号，绝不在本流程中登录主账号（硬约束：账号不得被封禁）。
    - .browser/ 内含登录态等敏感数据，切勿提交到代码仓库或外传。

用法
    python login.py

可重复运行：再次执行会复用已存在的 user_data_dir；如已是登录态，直接回车保存即可。
"""
from __future__ import annotations

import sys

from patchright.sync_api import sync_playwright

from collector.config import settings

INSTAGRAM_HOME = "https://www.instagram.com/"


def main() -> int:
    user_data_dir = settings.browser_user_data_dir
    storage_state_path = settings.browser_storage_state
    user_data_dir.mkdir(parents=True, exist_ok=True)
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(" Instagram 一次性登录工具（专用备用账号）")
    print("=" * 60)
    print(f"浏览器配置目录 (userDataDir): {user_data_dir}")
    print(f"登录态备份 (storage_state)  : {storage_state_path}")
    print("-" * 60)
    print("即将打开浏览器。请【只使用专用备用账号】完成登录（含 2FA / 人机验证）。")
    print("切勿登录你的 Instagram 主账号。")
    print("登录成功（页面显示首页 feed / 已登录态）后，回到本终端按回车保存。")
    print("-" * 60)

    with sync_playwright() as p:
        # persistent context：状态自动写入 user_data_dir，下次无头运行即可复用
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,  # 必须有头，才能人工登录
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(INSTAGRAM_HOME, wait_until="domcontentloaded")

        try:
            input(">>> 完成登录后，按回车保存登录态并退出（Ctrl+C 取消，不保存）... ")
        except EOFError:
            # 非交互环境（如被管道喂入），无法等待人工操作，直接放弃保存
            print("\n[login] 未检测到交互输入，放弃保存。请在终端中直接运行 python login.py。")
            context.close()
            return 1
        except KeyboardInterrupt:
            print("\n[login] 已取消，不保存登录态。")
            context.close()
            return 130

        try:
            # 额外导出 storage_state 作备份（persistent_context 本身已持久化到 user_data_dir）
            context.storage_state(path=str(storage_state_path))
            print(f"[login] 已保存 storage_state 备份：{storage_state_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[login] 警告：导出 storage_state 失败（不影响 user_data_dir 持久化）：{exc}")

        context.close()

    print("[login] 完成。后续采集器将复用该登录态。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

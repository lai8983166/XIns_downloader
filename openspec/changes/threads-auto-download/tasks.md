## 1. threads_collector 挂钩与回灌保护

- [x] 1.1 新增 4 个 auto 挂钩（`rehydrate_threads_cache` / `touch_threads_cache` / `reset_threads_cache` / `threads_cache_nodes`，语义对齐 IG 版；`rehydrate` 置 `navigated=False`、`rehydrated=True`、不恢复 `first_req`），加入 `__all__`；同时把 `_extract_threads_username` 导出为 `extract_threads_username`（路由层复用）。验证：`python -c "from collector.threads_collector import rehydrate_threads_cache, touch_threads_cache, reset_threads_cache, threads_cache_nodes, extract_threads_username"` 通过，且 `extract_threads_username("@zuck")` / `("https://www.threads.net/@zuck")` 均返回 `zuck`。
- [x] 1.2 `preview_threads_profile` 的 `do_navigate_and_fetch` 增加 `keep_on_navigate` 保护（entry 含 `rehydrated` 时不清 `captured`/`have_codes`，对齐 IG 版）；`_threads_cache` 条目写入 `rehydrated` 键（首次导航后置 False）。验证：新增轻量单测（fake page 注入，不联网）覆盖"回灌 → 重导航 → 旧帖去重、新帖追加、captured 不清空"。

## 2. auto_job 平台参数化

- [x] 2.1 定义 `PlatformAdapter`（name / folder_prefix / preview / rehydrate / touch / reset / cache_nodes / node_code）与 `instagram_adapter` / `threads_adapter` 两个模块级实例及 `_ADAPTERS` 注册表（IG 的 `node_code=lambda n: n.get("code")`；Threads 的从 `thread_items[0].post.code` 提取）。验证：`python -c "from collector.auto_job import _ADAPTERS; assert set(_ADAPTERS) == {'instagram','threads'}"`。
- [x] 2.2 `_Manifest` 接收 `node_code`，边车 load/append/rewrite 三处去重改走它；`data` 增加 `platform` 字段，`load(username, adapter)` 回填平台且旧清单（无 `platform`）按 `instagram` 兼容。验证：单测——旧版 IG 清单 JSON（无 platform）加载后 `data["platform"] == "instagram"` 且行为不变；threads 形状节点（code 嵌套）边车 append/rewrite 不丢。
- [x] 2.3 `AutoJob(username, max_posts, download_root, adapter)`：`_run_inner`/`_snapshot_nodes` 内五个 IG 挂钩调用换 `adapter.*`，folder 用 `adapter.folder_prefix`，`status_dict()` 增加 `platform`，task 名含平台；`AutoJobManager.start(username, max_posts=None, platform="instagram")` 按名取适配器，互斥逻辑不动。验证：`python -c "import collector.auto_job"` 通过，IG 路径单测全部保持绿色。
- [x] 2.4 更新 `test_auto_job.py` 的 fake 注入方式（构造 fake adapter 替代逐函数 patch），并新增用例：threads adapter 下任务跑通（翻页→下载→清单落 `threads_{username}`）、跨平台互斥（IG 活跃时 threads start 抛 `active_job`）、threads 崩溃恢复走 `rehydrate_threads_cache`。验证：`python test_auto_job.py` 全绿。

## 3. 后端路由

- [x] 3.1 `main.py` 新增 `POST /threads/profile/auto`（复用 `AutoStartRequest`/`AutoStartResponse`）：`extract_threads_username` 校验 → `auto_manager.start(username, req.max_posts, platform="threads")`；`AutoJobError` 映射同 IG（`active_job` → 409 附 job_id）。验证：TestClient（或起服务 curl）——非法输入 400、合法输入经 fake 适配器返回 202 与 `job_id`、`GET /profile/auto/jobs/{job_id}` 状态含 `platform: "threads"`。

## 4. 前端

- [x] 4.1 `App.jsx` 模式按钮：Threads 平台下显示 Profile / 自动 / 审查（隐藏「单帖」）；`handlePlatformChange("threads")` 仅在 mode 为 `post` 时回落 `profile`。验证：`npm run build` 通过，切到 Threads 后「自动」按钮可选。
- [x] 4.2 `startAutoJob` 按平台请求 `"/threads/profile/auto" | "/profile/auto"`（轮询/取消路径不变）；`AutoJobPanel` 对 `mediacount` 为 null（Threads 无总数）退化展示计数、不渲染百分比；自动模式输入提示与启动文案按平台区分。验证：build 通过 + 启动 Threads 任务后状态面板正常轮询展示。

## 5. 集成验证

- [x] 5.1 全量回归：`python test_auto_job.py && python test_scheduler.py && python test_safety_quota.py && python test_review.py` 全绿。
- [x] 5.2 真实端到端冒烟（需本机浏览器登录态）：对一个小体量 Threads 用户 `POST /threads/profile/auto`（`max_posts=2`），观察状态轮询推进至 `done`、文件落 `downloads/threads_{username}/`、`.auto_state.json` 含 `platform: "threads"` 与边车非空；再次启动验证增量（全部 skipped）；中途取消验证优雅退出。（实测 zuck：首跑 done/12 帖、续跑回灌恢复无重下（旧文件 mtime 不变）、熔断→取消→清单保留均按设计；暴露"持久 page 卡死→零捕获误判 not_found"缺口，已补废弃 page 修复 + 单测；修复后续跑冒烟 done）

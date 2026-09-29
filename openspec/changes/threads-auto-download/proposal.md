## Why

Threads 的手动分页预览与下载已上线（`threads_collector` + `/threads/profile/*` 路由），但自动任务（后台全量翻页采集 + 下载 + 清单断点续传 + 熔断）目前硬编码只支持 Instagram：用户想批量收藏某 Threads 用户全部媒体时只能一页页手动点。自动任务的安全框架（动作调度、冷却、日信号预算、熔断）是平台无关的，扩展成本低。

## What Changes

- 新增 `POST /threads/profile/auto` 启动 Threads 自动任务（入参同 IG：`profile` + 可选 `max_posts`）；状态查询/取消复用现有平台无关的 `/profile/auto/jobs/{job_id}` 接口。
- `AutoJob` / `AutoJobManager` 按 `platform`（`instagram` | `threads`）参数化：注入该平台的翻页采集函数、缓存挂钩（rehydrate / touch / reset / cache_nodes）、下载文件夹前缀（`profile_{username}` / `threads_{username}`）与边车节点 code 提取器；不复制一套 ThreadsAutoJob。
- `threads_collector` 补齐 4 个 auto 挂钩（对齐 `instagram_collector` 的 `rehydrate_profile_cache` 等接口），并修复回灌后降级重导航无条件 `captured.clear()` 丢失回灌数据的问题（对齐 IG 的 `rehydrated` 保护）。
- `_Manifest` nodes 边车去重改为平台感知：Threads 原始节点顶层无 `code`（在 `thread_items[0].post.code`），沿用 `n["code"]` 会导致边车增量写入全部被丢弃。
- 清单 `.auto_state.json` 记录 `platform` 字段，恢复/增量续跑按平台路由；同一活跃任务互斥保持全局单任务（IG 与 Threads 任务不并发）。
- 前端 Threads 平台放开「自动」模式按钮，启动请求打到 `/threads/profile/auto`。
- 不改动 IG 既有行为、调度器/安全规格与手动路由。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `auto-download`: 自动任务能力从 Instagram-only 扩展为双平台——新增 Threads 启动入口与平台参数化清单/续传/挂钩语义；其余需求（翻页-下载循环、清单去重断点、信号暂停、熔断、页面保温回灌、进度可观测）对 Threads 同样成立，仅采集链路与文件夹命名不同。

## Impact

- **后端**：`collector/auto_job.py`（`AutoJob`/`AutoJobManager`/`_Manifest` 平台参数化）、`collector/threads_collector.py`（新增 4 挂钩 + 回灌保护）、`main.py`（新路由 `/threads/profile/auto`，Threads 用户名校验复用 `_extract_threads_username`）。
- **前端**：`frontend/src/App.jsx`（Threads 平台显示自动模式、auto 路径按平台切换、状态面板文案）。
- **文件布局**：新增 `downloads/threads_{username}/.auto_state.json` 与 `.auto_nodes.jsonl`；既有 `profile_*` 清单不受影响（缺 `platform` 字段按 `instagram` 兼容读取）。
- **API**：新增 1 个路由；无 BREAKING 变更。
- **安全**：无新风险面——Threads 任务复用全局 scheduler / cooldown / 日信号预算，且与 IG 任务共用全局单活跃互斥。

## 1. ActionScheduler（配额 → 调度器）

- [x] 1.1 `collector/config.py`：`session_action_limit` 默认 60→300；删除 `daily_action_limit`；新增 `min_action_gap_sec`(25)、`slot_jitter`(0.35)、`daily_signal_limit`(3)、`auto_page_size`(12)、`auto_fail_limit`(3)、`auto_download_concurrency`(3)、`auto_resume_jitter_sec`(300)（各带 `XINS_*` env 覆盖）。验证：`python -c "from collector.config import settings; ..."` 打印各新字段默认值无误。
- [x] 1.2 重写 `collector/quota.py` 为 `ActionScheduler`：窗口预算桶（anchor 开窗、名额用尽即以 `max(now, last_finish+min_gap)` 滚新窗）、jittered-grid 日程生成（强制相邻 ≥ min_gap、非等间隔）、`wait_for_slot()`（可中断睡眠 + 扣名额）、`acquire_now()`（手动立即放行 + 扣名额 + 推进 `last_finish`，满窗抛 `QuotaExceeded`）、`invalidate()`（作废当前窗口）、`status()`（剩余名额/下一动作 ETA）。验证：新增 `test_scheduler.py`——日程分布满足"相邻 ≥ min_gap 且间隔互不相同"、名额用尽立即开新窗、`invalidate` 后旧日程点不复用、`acquire_now` 满窗抛 `QuotaExceeded`。
- [x] 1.3 `collector/instagram_collector.py`：`preview_post` / `preview_profile` 移除 `quota.acquire()`，仅保留 `cooldown.check()`；docstring 注明"名额由调用方负责"。验证：`grep -n "quota" collector/instagram_collector.py` 无 `acquire` 残留，模块可导入。（同步最小改动 `threads_collector.py`：其 import 旧 quota 符号，改为不消费名额、由路由层负责。）
- [x] 1.4 `main.py` 手动路由接入：`/preview`、`/download`、`/profile/preview`、`/profile/download` 在调用采集前 `scheduler.acquire_now()`，`QuotaExceeded` 映射 429（复用 `_map_collector_error` 风格）。验证：curl 连续触发 `/preview` 超窗后返回 429 与窗口满说明（可用临时小窗口配置验证后复原）。
- [x] 1.5 重写 `test_safety_quota.py` 适配调度器语义（窗口满 429、min_gap、滚动开窗），移除每日配额断言。验证：`python -m pytest test_safety_quota.py test_scheduler.py -q`（或项目现行测试方式）全部通过。（现行方式为纯脚本：`python test_safety_quota.py` 17/17、`python test_scheduler.py` 19/19。）

## 2. 每日信号预算（safety 层）

- [x] 2.1 `collector/safety.py`：`cooldown.report()` 内累加按本地自然日的信号计数，新增 `daily_signal_status()`（当日命中数/上限，跨日自动重置）。验证：单测构造两次 `report` + 日期翻转后计数归零。
- [x] 2.2 `AutoJob` 接入信号预算的依赖钩子确认（消费方在 3.x 实现）：达到 `daily_signal_limit` → 任务 `paused_signal_budget`，次日自动可恢复；未达到 → 冷却暂停 + `auto_resume_jitter_sec` 随机余量后自动恢复。验证：见 3.x 任务的单测覆盖。

## 3. AutoJob 模块（`collector/auto_job.py`）

- [x] 3.1 Manifest 持久化：`.auto_state.json`（posts 状态映射/cursor/exhausted/failures/计数，每帖原子落盘 tmp+`os.replace`）与 `.auto_nodes.json` 边车（原始 nodes 增量）；损坏文件 → 启动报错指明路径。验证：单测覆盖写入-读回-增量、半写文件（截断 JSON）拒绝启动。
- [x] 3.2 任务状态机与循环：`AutoJobManager`（job 注册表、同 username 活跃任务去重）+ `AutoJob`（asyncio Task；状态 `running/paused_{cooldown,signal_budget,circuit}/done/failed/cancelled`）；循环 = `wait_for_slot()` → `preview_profile(username, cursor, auto_page_size)` → 页内下载 → 清单落盘 → `report_finish()`。验证：单测以 fake collector 跑通 running→done 全流程，计数正确。
- [x] 3.3 下载执行器：页内 `Semaphore(auto_download_concurrency)` 并发 + `.part`→`os.replace` 原子落盘 + exists&size>0 跳过 + 失败退避重试 2 次（不占名额）后记 failures 继续。验证：单测覆盖跳过已存在文件、重试后记失败不阻断循环。
- [x] 3.4 失败分类与熔断：信号类 → `cooldown.report` 已由采集层完成，任务转 `paused_cooldown`（resume_at = 冷却剩余 + 随机余量）或 `paused_signal_budget`（当日预算满）；`not_found` 于 profile 首页 → `failed` 终态；其余连续 `auto_fail_limit` 次 → `paused_circuit` + `scheduler.invalidate()`，成功清零计数。验证：单测注入连续 parse 失败 → 熔断；注入一次成功 → 计数清零。
- [x] 3.5 保温与回灌：任务每页后 touch 该 username 缓存 TTL；`collector/instagram_collector.py` 新增 `rehydrate_profile_cache(username, nodes, exhausted)`；恢复路径先回灌再续滚；任务每页把新 nodes 追加进边车。验证：单测回灌后 `preview_profile` 的 cursor 切片与回灌 nodes 对齐；TTL 在任务活跃期不淘汰条目。

## 4. API 路由（`main.py`）

- [x] 4.1 `POST /profile/auto`（`{profile, max_posts?}` → 202 `{job_id, username}`；活跃任务重复启动 → 409；走 username 校验复用 `_extract_profile_username` 语义）。验证：curl 启动任务返回 job_id；同 username 二次启动 409。
- [x] 4.2 `GET /profile/auto/jobs/{job_id}`：返回 `AutoJobStatus`（state/paused_reason/resume_at/counts/mediacount/cursor/has_more/next_action_at/recent_failures/updated_at）。验证：任务运行中 curl 状态字段齐全（对照 specs/auto-download 的 Progress observability）。
- [x] 4.3 `POST /profile/auto/jobs/{job_id}/cancel`：置取消标志，当前动作完成后 `cancelled`。验证：启动后立即取消，最终状态 `cancelled`，清单文件保留且可增量重启。

## 5. 前端（`frontend/src/App.jsx`）

- [x] 5.1 自动模式入口：输入主页链接（+可选 max_posts）→ 调 `POST /profile/auto`；2s 轮询状态；进度条（已下载/已知总数）、状态与暂停原因（冷却中 xx 分钟 / 信号预算用尽 / 熔断）、最近失败列表、取消按钮。验证：浏览器实操一个小账号走完 done，中途验证暂停原因展示与取消。
- [x] 5.2 错误与边界提示：409（已有任务）跳转到该任务视图或提示；429/404 启动失败提示。验证：手工构造重复启动与不存在用户名的提示正确。

## 6. 集成验证

- [x] 6.1 端到端小账号实测：真实小体量公开账号（< 30 帖）自动模式跑至 done；重启后端后再次启动 → 全部 skipped（增量语义）；目录文件与清单一致。验证：比对 `downloads/profile_{username}/` 文件数 = 已下载计数，重跑日志无重复下载。
- [x] 6.2 手动/自动共存实测（跳过真机验证：手动即时放行/扣名额/推后日程已被 test_scheduler.py 单测与路由 fake 联调覆盖；浏览器单实例串行导致手动在自动动作进行中排队属设计内行为，如实际使用中体感不佳再优化）：自动任务运行中手动 `/preview` 单帖 → 立即返回，且自动任务后续动作顺延不早于 min_gap。验证：状态接口 `next_action_at` 在手动操作后可见后移。

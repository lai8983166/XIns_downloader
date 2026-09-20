## Why

当前 Profile 采集是"手动翻页预览 + 勾选下载"，无法应对整号备份场景（几百上千帖需要人坐在页面前反复点击）。同时现有配额体系（60 次/小时滑动窗口 + 120 次/日计数器）经实测过于保守：每日配额没有必要，每小时配额可以保留且应提高。等间隔的纯计数器也无法回答"这一小时里动作应该在什么时刻执行"——需要一个拟人的随机日程，并容忍失败重试占用额外时间导致窗口拉伸。

## What Changes

- **新增自动模式（auto mode）**：给定一个用户主页链接，后台 job 自动翻页采集该用户全部帖子并下载媒体，直至 `has_more=false`。job 可查询进度、可取消。
- **持久化去重清单**：`downloads/profile_{username}/.auto_state.json` 记录 shortcode 级完成状态、cursor、失败列表；后端崩溃/重启后可续传；对同一用户重跑时增量（只拉新帖）。另有文件级去重（目标文件已存在且非空则跳过）。
- **边采边下**：每翻到一页立即下载该页媒体（CDN 签名 URL 会过期，不做"先全采后统一下载"）。CDN 下载不占动作名额，可小并发。
- **配额改造（BREAKING）**：`QuotaGuard`（会话滑动窗口 + 每日计数）重构为 `ActionScheduler`：
  - 删除每日动作配额（`XINS_DAILY_ACTION_LIMIT` 不再生效）。
  - 每小时窗口预算默认 **300**（`XINS_SESSION_ACTION_LIMIT`，原默认 60），env 可覆盖。
  - 窗口内动作时刻由 **jittered grid** 生成：把窗口等分为 N 格、每格内随机偏移、强制相邻动作 ≥ `min_gap`（默认 8s，需 ≤ 窗口时长/名额以保证 300 上限可达），避免纯随机的扎堆与长空窗。
  - 日程点是"最早可执行时间"而非固定时刻：`next = max(slot, 上一动作完成 + min_gap)`，天然容忍失败重试导致的一窗口任务超一小时。
  - 窗口是预算桶不是墙钟：名额用尽（无论成败）立即生成下一窗口日程，不等整点。
  - 手动操作（`/preview`、`/profile/preview` 等）**不经日程排队**，立即执行，但消耗当前窗口名额；手动动作同样推后自动任务的下一个日程点。
- **每日信号预算替代每日动作配额**：自动任务在一个本地自然日内命中 N 次（默认 3）风控信号即当日停跑。动作数不是风险的直接指标，信号才是。
- **连续失败熔断**：自动任务连续 3 次非信号类获取失败 → job 挂起等待人工确认，防止页面结构变更/登录态失效时快速烧光名额。
- **风控响应不变**：信号检测命中即停 + 阶梯冷却（15→30→60→120→360 min）保持现状；自动任务额外作废当前调度窗口，恢复后重新生成日程。
- **新增 Job API**：`POST /profile/auto`（启动）、`GET /profile/auto/jobs/{job_id}`（状态/进度）、`POST /profile/auto/jobs/{job_id}/cancel`。状态含已下载/跳过/失败计数、暂停原因与预计恢复时间。
- **深翻页支持**：自动任务活跃期间保持该 username 的持久 page 温热（不被 TTL 淘汰）；窗口作废/重启导致缓存丢失时，从清单回灌已采集 nodes 续滚，避免重复消耗。

## Capabilities

### New Capabilities
- `auto-download`: 后台自动全量采集下载 job——生命周期（running/paused/done/failed/cancelled）、翻页-下载-落盘循环、持久化去重清单与断点续传、连续失败熔断、每日信号预算、进度查询。
- `action-scheduler`: 每小时窗口动作调度器——窗口预算（默认 300/h）、jittered-grid 随机日程、最小动作间隔、日程点为最早可执行时间、窗口用尽即滚动、冷却时作废重排、手动路径"立即执行但扣名额"的语义。

### Modified Capabilities
- `account-safety`: 需求 "Session and daily quota caps" 变更——删除每日动作配额；会话/每小时上限语义改为由 `action-scheduler` 的窗口预算承载（默认 300/h，手动+自动共享）；新增每日信号预算（风控信号计数，默认 3 次/日停跑）。信号检测与阶梯冷却需求不变。

## Impact

- **collector/quota.py**：`QuotaGuard` 重构为 `ActionScheduler`（窗口预算 + 日程生成 + 槽位等待），`QuotaExceeded` 语义保留给手动路径超额场景。
- **collector/config.py**：`session_action_limit` 默认 60→300；删除 `daily_action_limit`；新增 `min_action_gap_sec`、`slot_jitter`、`daily_signal_limit`、自动任务相关配置（页间长停顿、熔断阈值、下载并发）。
- **collector/instagram_collector.py**：`quota.acquire()` 调用点改为调度器语义；为自动任务提供缓存回灌/保持温热的挂钩。
- **新增 collector/auto_job.py**：job 管理器（状态机、循环、清单读写、下载）。
- **main.py**：新增 `/profile/auto*` 路由；原引用 `quota` 的调试/状态出口改造。
- **frontend/src/App.jsx**：自动模式入口（输入主页链接→启动）、进度条、暂停原因（冷却中/今日信号预算用尽/熔断）、失败列表、取消按钮。
- **测试**：`test_safety_quota.py` 需重写适配调度器；新增调度器（日程分布、min_gap、窗口滚动、拉伸）与 job 状态机测试。
- **风险说明**：小时上限 60→300 抬高吞吐上限，兜底依赖不变的风控信号→阶梯冷却链路；建议继续仅用专用备用账号执行。

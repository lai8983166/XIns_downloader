## Why

Instagram 与 Threads 的"Profile 分页预览 + 批量自动下载"链路已稳定运行（`PlatformAdapter` 双平台）。X 的登录态探查（2026-10，`openspec/probe_x_auth_summary.json`，含凭证不入库）已确认全部采集前提：cookie 注入登录态可用、`UserOriginalsTimeline` 响应（253KB/页）含整页推文与媒体、滚动即触发翻页 cursor、`pbs.twimg.com`/`video.twimg.com` 直链可得。缺的只是第三平台适配，成本已被前两次平台接入摊薄。

## What Changes

- 新增 `collector/x_collector.py`：X 用户主页分页采集（持久 page 滚动捕获 `UserOriginalsTimeline` 响应，模式对齐 threads；无 graphql 重放——浏览器自带 `x-client-transaction-id` 反爬头，滚动即翻页），X 推文节点解析（图片 `pbs.twimg.com` 直链；视频取 `video_info.variants` 最大码率 mp4、跳过 HLS），auto 挂钩（rehydrate/touch/reset/cache_nodes + 回灌保护）。
- `auto_job` 注册 `x_adapter`：自动任务三平台化（文件夹前缀 `x_{username}`，边车按推文 id 去重）。
- `main.py` 新增 `POST /x/profile/preview|download|auto`（状态/取消复用现有 job 接口）；下载白名单增加 `twimg.com`；X 下载 Referer 按平台。
- 登录态：复用 `login_x_cookies.py` 注入的 cookie（`auth_token`/`ct0`）；`auth_token` 缺失或失效 → `login_required`（401），提示重新注入。`detect_signal` 现有 `/login` hint 已覆盖 `x.com/login`，仅以单测固化，不改逻辑。
- 前端新增 X 平台（platform `x`，Profile/自动/审查模式，无单帖）。
- 不改动 IG/Threads 行为、调度与安全框架。

## Capabilities

### New Capabilities

- `x-collection`: X（原推特）用户主页的浏览器采集与手动预览/下载——滚动捕获 `UserOriginalsTimeline`、整型 cursor 分页契约（对齐 instagram-collection）、推文媒体提取（图片直链/最大码率视频）、注入登录态与失效检测、`/x/profile/*` 路由与 `twimg.com` 白名单。

### Modified Capabilities

- `auto-download`: 自动任务从双平台扩展为三平台（Instagram / Threads / X）——新增 `POST /x/profile/auto` 入口与 `x_{username}` 清单目录；互斥、断点续传、熔断、回灌、可观测等需求以平台无关形式对三平台成立（本增量按三平台最终形态书写，与 pending 的 threads-auto-download 增量在归档时合成同一最终文本）。

## Impact

- **后端**：`collector/x_collector.py`（新）、`collector/auto_job.py`（`x_adapter` 一处注册）、`main.py`（路由 + 白名单 + per-platform Referer）、`collector/safety.py`（仅加单测，逻辑不动）。
- **前端**：`frontend/src/App.jsx`（平台组加 X；输入提示/文案/面板平台标识）。
- **文件布局**：新增 `downloads/x_{username}/`（媒体 + `.auto_state.json` + `.auto_nodes.jsonl`）。
- **API**：新增 3 个路由，复用既有请求/响应模型；无 BREAKING。
- **运维前置**：X 采集依赖人工一次性 `python login_x_cookies.py` 注入登录态（新号，节奏由现有 scheduler/cooldown/日预算约束）。

## Context

`PlatformAdapter`（threads-auto-download D1）已把自动任务的平台差异收敛为一个适配对象，接入第三平台的成本只剩采集器本身。X 登录态探查（`openspec/probe_x_auth_summary.json`）确认：cookie 注入后 graphql 全 200（16/16）、`UserOriginalsTimeline` 单响应 253KB 含整页推文与媒体、翻页 cursor 在响应内且**滚动即触发**（浏览器自动生成 `x-client-transaction-id`，无需伪造）、媒体直链在 `pbs.twimg.com` / `video.twimg.com`（多码率 mp4 + HLS）。登录路径已建：日常浏览器登录 + `login_x_cookies.py` 注入（X 对自动化浏览器的**登录环节**检测最严，注入跳过该关卡；采集期探查已验证不受影响）。见 proposal.md - Why。

## Goals / Non-Goals

**Goals:**

- `x_collector` 提供与 threads 同构的 Profile 分页采集 + 手动路由 + auto 挂钩；`x_adapter` 一处注册进自动任务。
- X 专属解析独立成纯函数（推文结构与 IG media 节点不同，不复用 `_node_to_profile_post`）。
- IG/Threads 行为零变化。

**Non-Goals:**

- 不做 X 单帖采集（`TweetDetail` 证据已在探查留档，未来可加独立变更）。
- 不做转发/引用推文的引用媒体展开（只取推文本体媒体）。
- 不改 `safety.py` 检测逻辑（`/login` hint 已覆盖 `x.com/login`，仅单测固化）。
- 不做自动登录/注册（硬约束：登录只能人工 + 注入）。

## Decisions

### D1: 架构复用 threads 模式，滚动捕获替代 graphql 重放

`x_collector.py` 结构对齐 `threads_collector`：持久 page 池键 `x:{username}`、TTL 缓存（600s）、整型 cursor 偏移分页、`rehydrated` 回灌保护、零捕获时废弃持久 page、四个 auto 挂钩。差异：翻页靠**逐步滚动**触发浏览器自身的时间线请求（对齐 IG 的 `_scroll_until`，直接复用），X 不需要 threads 的 graphql 重放路径——这简化了续传（无 `first_req` 依赖）。纯文字推文与无有效媒体的推文跳过（对齐 threads `_has_media` 语义）。

### D2: 按结构特征识别时间线响应，推文解析独立纯函数

时间线识别不绑定 operationName（探查见 `UserOriginalsTimeline`，X 也常见 `UserTweets` 变体，且会随前端改版变动）：on_response 匹配 JSON 内含 `tweet_results`（或 `tweetResult`）数组的 instruction/entry 结构即按时间线处理，遍历 entry 提取推文节点与 `cursor-bottom-*`（滚动续传游标）。解析纯函数：

- `x_tweet_resources(tweet)`：`extended_entities.media[]` → 图片 `media_url_https` 直链（`?format=…&name=…` 取 `name=orig` 原图）；视频 `video_info.variants` 过滤 `content_type=video/mp4` 取最大 `bitrate`（跳过 m3u8/其他），缩略图 `media_url_https`；`index` 从 1 递增；`filename` `{tweet_id}_{index}.{ext}`。
- `x_node_code(node)`：推文 id（`rest_id` / `legacy.id_str`），供边车去重与 `x_adapter.node_code`。
- `shortcode` 即推文 id，`url` 为 `https://x.com/{user}/status/{id}`；`date_utc` 由 `legacy.created_at`（RFC 2822）解析。

### D3: 登录态检测走既有信号链路

导航后 `detect_signal(url=page.url)` 已能命中 `x.com/login` / `/i/flow/login`（`_LOGIN_HINTS` 含 `/login`）→ `login_required` + 冷却，与 IG/Threads 同路径。`auth_token` 预检（导航前读 context cookies）用于给出更明确的"请运行 login_x_cookies.py"提示。账号冻结/封禁页（`suspended` 文案）落入 `not_found`（404）语义。`safety.py` 逻辑不动，仅新增单测固化 X URL 命中。

### D4: 下载白名单与 per-platform Referer

`ALLOWED_MEDIA_HOST_SUFFIXES` 增加 `"twimg.com"`（`pbs.twimg.com` / `video.twimg.com` 同后缀）。`download_file` / auto `_download_file` 增加 `referer` 参数：X 用 `https://x.com/`，IG/Threads 维持 `https://www.instagram.com/`（实测不回归）。auto job 的下载 Referer 经 adapter 扩展（`download_referer` 字段）。

### D5: `sec-ch-ua` 泄漏"HeadlessChrome"——第一版不动，列为观察项

探查 16/16 请求 200，说明 X 当前不以此拦截（patchright 已处理主要指纹）。UA/sec-ch-ua 覆盖反而引入新指纹面。处理：上线后若出现 403/challenge 现象再针对该现象加覆盖（记入 Risks 观察项，不做预防性复杂化）。

### D6: `x_adapter` 注册与前端

`auto_job.py` 仅新增：`x_adapter = PlatformAdapter(name="x", folder_prefix="x_", preview=preview_x_profile, rehydrate/touch/reset/cache_nodes=x_collector 四挂钩, node_code=x_node_code)`，加入 `_ADAPTERS`。`main.py` 三个路由 `/x/profile/preview|download|auto`（复用既有模型与错误映射，校验用 `x_collector.extract_x_username`）。前端 platform `"x"`：平台组加按钮，模式 = Profile/自动/审查（无单帖，对齐 threads），路径/文案按平台切换。

### D7: 新号采集节奏

共享既有 `scheduler`（小时窗预算 + jittered grid）/`cooldown` / 日信号预算，跨平台单任务互斥不变；首次真实运行建议先手动预览 1–2 次验证稳定性再启动自动任务，且首批任务用 `max_posts` 限幅。预算可通过既有 `XINS_SESSION_ACTION_LIMIT` 环境变量整体调低。

## Risks / Trade-offs

- [新注册小号被风控/冻结的概率高于老号] → 保守节奏起步（D7）+ 共享日信号预算熔断 + 只读契约（导航/滚动/读响应，无任何写操作）；若账号被冻结，`suspended` 页落入 404 语义，需人工换号重新注入。
- [`sec-ch-ua` 等 headless 指纹未来被 X 启用拦截] → 观察项（D5）：出现 403/challenge 即停（信号链路），届时按现象补 UA/client-hints 覆盖，不预防性处理。
- [时间线响应结构随 X 前端改版变动（operationName/嵌套层级）] → D2 按结构特征识别而非 op 名；解析失败落入既有 `parse` 失败 → 连续失败熔断路径，不静默错采。
- [视频 variants 含 HLS 与混合 content_type] → 硬过滤 `video/mp4` + 最大 bitrate，单测固化（含 m3u8 排除用例）。
- [cookie 登录态失效（X 主动过期/踢下线）] → D3 检测为 `login_required` 401，前端/任务暂停提示重跑 `login_x_cookies.py`；清单保留，重注入后增量续跑。

## Migration Plan

无数据迁移：新平台落独立 `downloads/x_{username}/`；IG/Threads 路径与清单不受影响。回滚即 revert 本变更。前置运维：目标机器需人工完成一次 `login_x_cookies.py` 注入（工具已入库）。

## Purpose

以"合法 Web 客户端"身份（Playwright 驱动真实浏览器 + 注入 cookie 登录态）采集 X（原推特）用户主页内容：滚动捕获浏览器自身发出的时间线 GraphQL 响应提取推文与媒体，为手动预览/下载与自动任务提供与 Instagram/Threads 同构的数据面。

## ADDED Requirements

### Requirement: X profile paginated collection via browser

系统 SHALL 使用浏览器打开目标 X 用户主页（`https://x.com/{username}`，兼容 `twitter.com` 与 `@user` 输入），通过逐步滚动触发 X Web 自身的时间线 GraphQL 请求并捕获其响应累积推文列表（不自行构造 API 请求、不重放）。API 边界 SHALL 保持与 `instagram-collection` 相同的整型 `cursor` 偏移分页契约：请求 `[cursor, cursor+limit)` 区间，`next_cursor = cursor + 本次返回数`，`has_more` 反映是否还有更多。内部 SHALL 维护按 `username` 的有序缓存（TTL + 持久 page 跨请求续传，滚动只推进未取区间），纯文字推文（无有效媒体）SHALL 被跳过。

#### Scenario: Profile 首屏分页
- **WHEN** 客户端 `POST /x/profile/preview` 传入用户名、`cursor=0`、`limit=6`
- **THEN** 系统经浏览器加载主页并（必要时）滚动捕获，`posts` 为该用户最新至多 6 条含媒体推文，每条含 `shortcode`（推文 id）/`url`（推文链接）/`caption`/`date_utc`/`type`/`media_count`/`thumbnail_url`/`resources[]`，`next_cursor` 与 `has_more` 与真实情况一致

#### Scenario: 加载更多
- **WHEN** 客户端在前一页基础上传 `cursor=6`、`limit=6`
- **THEN** 系统返回第 7–12 条含媒体推文，不与首屏重复；续传时仅滚动补足缺口区间

#### Scenario: 触及时间线末尾
- **WHEN** 当前 `cursor` 之后已无更多含媒体推文
- **THEN** 系统返回剩余推文，`has_more=false` 且 `next_cursor=null`

#### Scenario: 用户不存在
- **WHEN** 目标用户主页不存在（404/被冻结账号页面）
- **THEN** 系统返回 HTTP 404，不进入翻页循环

### Requirement: X login session via injected cookies

X 采集 SHALL 复用经人工注入的浏览器 cookie 登录态（`auth_token`/`ct0`，工具 `login_x_cookies.py`）；系统 MUST NOT 在任何环节自动执行账号密码登录或注册。采集入口 SHALL 在导航后检测登录态：`auth_token` 缺失、被重定向到 `x.com/login` / `/i/flow/login` 或响应提示需要登录时，SHALL 返回 `login_required`（HTTP 401）并提示重新执行注入，同时按风控信号处理（停手 + 冷却）。

#### Scenario: 未注入登录态
- **WHEN** 浏览器 profile 无 `auth_token` 且客户端请求 `POST /x/profile/preview`
- **THEN** 系统返回 HTTP 401，detail 提示先运行 cookie 注入工具

#### Scenario: 登录态失效
- **WHEN** 采集中导航被重定向至 X 登录页
- **THEN** 系统命中登录墙信号，返回 401 并进入冷却（不重试）

### Requirement: X tweet media extraction

推文媒体解析 SHALL：图片取 `pbs.twimg.com` 直链；视频取 `video_info.variants` 中 `content_type=video/mp4` 且码率最大的变体（MUST NOT 返回 HLS `.m3u8` 或非 mp4 变体），缩略图取推文图片直链；多图/多视频推文 SHALL 展开全部子媒体且 `index` 从 1 递增；`filename` 形如 `{tweet_id}_{index}.{jpg|mp4}`。下载白名单 SHALL 允许 `twimg.com` 域，X 媒体下载 SHALL 携带 X 平台 Referer。

#### Scenario: 单图推文
- **WHEN** 解析一条含 1 张图片的推文
- **THEN** `resources` 恰含 1 个 `type=image` 条目，URL 为 `pbs.twimg.com` 直链，`filename` 为 `{tweet_id}_1.jpg`

#### Scenario: 视频推文取最大码率
- **WHEN** 解析一条视频推文，其 variants 含 480x270/256kbps、1280x720/2176kbps 的 mp4 与一个 `.m3u8`
- **THEN** `resources` 恰含 1 个 `type=video` 条目，URL 为 2176kbps 变体直链，`filename` 为 `{tweet_id}_1.mp4`

#### Scenario: 多媒体推文展开
- **WHEN** 解析一条含 2 图 1 视频的推文
- **THEN** `resources` 含 3 个条目，`index` 为 1/2/3，各条目类型与原帖一致

### Requirement: X manual routes

系统 SHALL 提供 `POST /x/profile/preview` 与 `POST /x/profile/download`（请求/响应模型与 Threads 手动路由一致）：预览走采集分页契约；下载从该用户采集缓存取勾选推文的媒体资源（缓存未命中返回 404 提示先预览），保存至 `downloads/x_{username}/`。手动操作消耗调度器窗口名额，与既有手动路由语义一致。

#### Scenario: 预览并勾选下载
- **WHEN** 客户端先 `POST /x/profile/preview` 预览某用户，再 `POST /x/profile/download` 勾选 2 条推文
- **THEN** 系统从缓存解析这 2 条推文的全部资源并保存到 `downloads/x_{username}/`，返回 `files` 列表

#### Scenario: 缓存未命中
- **WHEN** 客户端未预览直接请求下载某推文 id
- **THEN** 系统返回 HTTP 404，提示先预览该用户主页

### Requirement: X risk-signal detection parity

既有信号检测 SHALL 覆盖 X 的登录墙/挑战 URL（`x.com/login`、`x.com/i/flow/login` 命中 `login` hint）：命中即停手、上报冷却、任务进入暂停态，与 Instagram/Threads 行为一致；SHALL 以单元测试固化该检测（不改变检测逻辑本身）。

#### Scenario: 登录墙 URL 被识别
- **WHEN** `detect_signal` 收到 `https://x.com/i/flow/login` 的页面 URL
- **THEN** 返回登录墙信号，采集层停手并进入冷却

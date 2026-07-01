## ADDED Requirements

### Requirement: Single-post collection via browser
系统 SHALL 使用 Playwright 驱动真实浏览器打开 Instagram 单帖页面（`/p/`、`/reel/`、`/tv/`），
并通过拦截浏览器自身发出的网络响应来提取媒体数据，而非由系统自行构造对 Instagram API 的请求。
解析结果 SHALL 能填充现有 `PreviewResponse` 所需的全部字段（`shortcode`、`owner_username`、
`caption`、`resources[]`，每个 resource 含 `index`/`type`/`url`/`thumbnail_url`/`filename`），
包括单图、单视频与多图/多视频（GraphSidecar）帖子的全部子节点。

#### Scenario: 单图帖子预览
- **WHEN** 客户端 `POST /preview` 传入一个单图帖子链接
- **THEN** 系统经浏览器加载该帖子并捕获网络响应，返回的 `resources` 恰好包含 1 个 `type=image` 的条目，
  其 `url`/`thumbnail_url` 为可下载的 CDN 直链，`filename` 形如 `<shortcode>_1.jpg`

#### Scenario: 多图(sidecar)帖子预览
- **WHEN** 客户端 `POST /preview` 传入一个含 3 张图/视频的 sidecar 帖子链接
- **THEN** 系统返回的 `resources` 恰好包含 3 个条目，`index` 从 1 递增，`type` 与原帖各子节点一致，
  每个 resource 的媒体 URL 均来自浏览器捕获的网络响应

#### Scenario: 链接格式非法
- **WHEN** 客户端传入无法识别为 `/p/`、`/reel/`、`/tv/` 的链接
- **THEN** 系统返回 HTTP 400，且不发起任何浏览器导航

### Requirement: Profile paginated collection via browser
系统 SHALL 使用浏览器打开目标 Profile 页，通过**逐步滚动**触发 Instagram Web 自身发出的下一页
请求，并捕获其网络响应来累积帖子列表。系统 SHALL 在 `/profile/preview` 的 API 边界保持**整型
`cursor` 偏移分页**契约不变：请求 `[cursor, cursor+limit)` 区间，`next_cursor = cursor + 本次返回数`，
`has_more` 反映是否还有更多帖子。内部 SHALL 维护按 `username` 的有序缓存以支撑"加载更多"，
缓存 SHALL 设 TTL 与容量上限。

#### Scenario: Profile 首屏分页
- **WHEN** 客户端 `POST /profile/preview` 传入用户名、`cursor=0`、`limit=6`
- **THEN** 系统经浏览器加载 Profile 并（必要时）滚动捕获，返回的 `posts` 为该 Profile 最新至多 6 个帖子，
  每个帖子含 `shortcode`/`url`/`caption`/`date_utc`/`type`/`media_count`/`thumbnail_url`/`resources[]`，
  `next_cursor=6`（当存在更多帖子时），`has_more` 与真实情况一致

#### Scenario: 加载更多
- **WHEN** 客户端在前一页基础上 `POST /profile/preview` 传入 `cursor=6`、`limit=6`
- **THEN** 系统返回第 7–12 个帖子，`posts` 不与首屏重复，`next_cursor=12`（当仍有更多时）

#### Scenario: 触及 Profile 末尾
- **WHEN** 当前 `cursor` 之后已无更多帖子
- **THEN** 系统返回剩余（可能少于 `limit`）帖子，`has_more=false` 且 `next_cursor=null`

#### Scenario: 私密账号
- **WHEN** 目标 Profile 为私密账号且当前备用账号未关注它
- **THEN** 系统返回 `is_private=true` 且不触发写操作；`posts` 仅包含可访问的内容（可能为空）

### Requirement: API contract fidelity
迁移 SHALL 保持现有所有接口的请求与响应模型**逐字段不变**，使前端零改动。`/preview`、
`/profile/preview`、`/profile/download`、`/download`、`/media-proxy` 的字段名、类型、可空性、
语义 SHALL 与迁移前一致。

#### Scenario: 响应结构不回归
- **WHEN** 采集层从 instaloader 切换为浏览器采集后，客户端请求任一上述接口
- **THEN** 返回 JSON 的结构（字段集合与类型）与迁移前的响应一一对应，前端无需改动即可正常解析

### Requirement: Download uses collector-sourced media URLs
`/download` 与 `/profile/download` SHALL 使用采集层（浏览器捕获）产出的媒体 CDN 直链进行下载，
下载本身仍经现有 `httpx` 直拉 `cdninstagram.com`/`fbcdn.net`。`selected_indices` 的筛选语义
（为空=全部）SHALL 不变。

#### Scenario: 下载勾选的资源
- **WHEN** 客户端 `POST /download` 传入链接与 `selected_indices=[2,3]`
- **THEN** 系统仅下载第 2、3 个资源到 `downloads/post_<shortcode>/`，`files` 列出已保存路径，
  下载来源为采集层产出的 CDN 直链

#### Scenario: 未传 selected_indices 下载全部
- **WHEN** 客户端 `POST /download` 仅传链接、不带 `selected_indices`
- **THEN** 系统下载该帖子全部资源

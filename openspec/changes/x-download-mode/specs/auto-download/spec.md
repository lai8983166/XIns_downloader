## MODIFIED Requirements

### Requirement: Auto job lifecycle

自动任务 SHALL 支持三个平台：Instagram 经 `POST /profile/auto`、Threads 经 `POST /threads/profile/auto`、X 经 `POST /x/profile/auto` 启动。各平台请求格式一致：含主页链接（用户名或 URL）与可选 `max_posts` 上限，返回 `job_id`；用户名/链接校验 SHALL 按平台各自的规则执行。任务状态 SHALL 覆盖：`running`（运行/等待日程）、`paused`（带原因：冷却中 / 信号预算用尽 / 熔断）、`done`、`failed`、`cancelled`。系统 SHALL 提供平台无关的 `GET /profile/auto/jobs/{job_id}` 查询状态与进度、`POST /profile/auto/jobs/{job_id}/cancel` 取消任务。活跃任务互斥 SHALL 是全局的：不论平台，已有活跃任务时重复启动 SHALL 被拒绝（409 语义，响应附现有 job_id）或幂等返回现有任务（不同平台的任务不得并发）。

#### Scenario: 启动并查询进度
- **WHEN** 客户端经任一平台的启动入口传入该平台用户的主页链接
- **THEN** 系统返回 `job_id`；随后 `GET /profile/auto/jobs/{job_id}` 返回状态、进度计数与下一个动作预计时间

#### Scenario: 跨平台任务互斥
- **WHEN** 一个平台的自动任务运行中，客户端经另一平台的启动入口提交新任务
- **THEN** 启动被拒绝（409 语义）并附现有任务的 `job_id`

#### Scenario: 取消任务
- **WHEN** 任务运行中客户端调用 `POST /profile/auto/jobs/{job_id}/cancel`
- **THEN** 任务在完成当前动作后停止，状态转为 `cancelled`，已落盘的清单与文件保留

#### Scenario: 采集完成自动结束
- **WHEN** 任务翻页至 `has_more=false` 且全部帖子处理完毕
- **THEN** 任务状态转为 `done`，状态接口展示最终计数

### Requirement: Fetch-page-then-download loop

自动任务 SHALL 逐页采集（复用对应平台的 Profile 分页采集链路及其全部风控保护；Instagram 与 X 为浏览器滚动捕获时间线，Threads 为 GraphQL 翻页重放，均受动作调度与信号检测约束），每采集到一页 SHALL 立即下载该页各帖子的媒体资源。系统 MUST NOT 先采集全部 URL 再统一下载（媒体 CDN 直链为签名 URL，会过期）。CDN 下载失败 SHALL 以小退避重试 2~3 次，且下载与下载重试 MUST NOT 消耗动作窗口名额；单资源最终失败 SHALL 记入失败清单并继续，不阻断任务。

#### Scenario: 每页采集后立即下载
- **WHEN** 任务采集到新的一页帖子
- **THEN** 该页媒体在进入下一页采集前完成下载（或重试后记为失败）

#### Scenario: X 逐页采集
- **WHEN** X 任务需要更多帖子
- **THEN** 翻页按浏览器滚动捕获推进（每次翻页受动作日程约束），每页媒体下载完成后才进入下一页

#### Scenario: 下载重试不占动作名额
- **WHEN** 某媒体文件下载失败并重试
- **THEN** 动作窗口剩余名额不因下载重试减少

#### Scenario: 单资源失败不阻断
- **WHEN** 某帖子的一个媒体资源经重试仍下载失败
- **THEN** 该资源记入失败清单，任务继续处理其余资源与帖子

### Requirement: Persistent dedup manifest and resume

任务 SHALL 为每个（平台, username）维护持久化清单：Instagram 位于 `downloads/profile_{username}/.auto_state.json`、Threads 位于 `downloads/threads_{username}/.auto_state.json`、X 位于 `downloads/x_{username}/.auto_state.json`（与各平台手动下载的文件夹一致），记录：帖子标识到完成/失败状态的映射、当前 cursor、失败列表、任务元信息（含 `platform`）。每完成一帖的下载 SHALL 落盘一次。后端重启后任务 SHALL 能从清单恢复：已完成的帖子不再重新采集下载（增量）；文件级去重以目标文件已存在且大小大于 0 为准。对同一（平台, username）重新启动任务时 SHALL 从已记录进度增量续跑，只采集清单未覆盖的新帖；清单的原始节点边车 SHALL 按平台各自的节点结构提取帖子标识去重（Instagram 顶层 `code`；Threads 位于 `thread_items[0].post.code`；X 为推文 id）。既有的无 `platform` 字段清单 SHALL 兼容按 Instagram 处理。

#### Scenario: 崩溃后重启续传
- **WHEN** 任务运行中后端进程崩溃并重启，随后任务被再次启动
- **THEN** 已完成帖子不重复下载，采集从清单记录的进度继续

#### Scenario: X 清单按平台恢复
- **WHEN** X 任务崩溃后重启，再次启动时提交同一 X 用户
- **THEN** 任务读取 `downloads/x_{username}/` 下的清单续跑，回灌的 X 原始推文节点按推文 id 去重且不丢失

#### Scenario: 已存在文件跳过
- **WHEN** 目标文件在磁盘上已存在且大小大于 0
- **THEN** 下载被跳过并计入"已跳过"，不发起 CDN 请求

#### Scenario: 增量重跑
- **WHEN** 任务 `done` 后该用户新发了帖子，用户再次启动自动任务
- **THEN** 任务只采集并下载清单未记录的新帖

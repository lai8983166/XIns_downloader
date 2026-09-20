## Purpose

给定 Instagram 用户主页链接，后台自动翻页采集该用户全部帖子并下载媒体：持久化去重、断点续传、失败熔断与进度可观测，采集节奏完全受 `action-scheduler` 日程约束。

## ADDED Requirements

### Requirement: Auto job lifecycle

系统 SHALL 提供 `POST /profile/auto` 启动自动任务：请求含主页链接（用户名或 URL）与可选 `max_posts` 上限，返回 `job_id`。任务状态 SHALL 覆盖：`running`（运行/等待日程）、`paused`（带原因：冷却中 / 信号预算用尽 / 熔断）、`done`、`failed`、`cancelled`。系统 SHALL 提供 `GET /profile/auto/jobs/{job_id}` 查询状态与进度、`POST /profile/auto/jobs/{job_id}/cancel` 取消任务。已有活跃任务时重复启动 SHALL 被拒绝（409 语义，响应附现有 job_id）或幂等返回现有任务。

#### Scenario: 启动并查询进度
- **WHEN** 客户端 `POST /profile/auto` 传入用户主页链接
- **THEN** 系统返回 `job_id`；随后 `GET /profile/auto/jobs/{job_id}` 返回状态、进度计数与下一个动作预计时间

#### Scenario: 取消任务
- **WHEN** 任务运行中客户端调用 `POST /profile/auto/jobs/{job_id}/cancel`
- **THEN** 任务在完成当前动作后停止，状态转为 `cancelled`，已落盘的清单与文件保留

#### Scenario: 采集完成自动结束
- **WHEN** 任务翻页至 `has_more=false` 且全部帖子处理完毕
- **THEN** 任务状态转为 `done`，状态接口展示最终计数

### Requirement: Fetch-page-then-download loop

自动任务 SHALL 逐页采集（复用 Profile 分页采集链路及其全部风控保护），每采集到一页 SHALL 立即下载该页各帖子的媒体资源。系统 MUST NOT 先采集全部 URL 再统一下载（媒体 CDN 直链为签名 URL，会过期）。CDN 下载失败 SHALL 以小退避重试 2~3 次，且下载与下载重试 MUST NOT 消耗动作窗口名额；单资源最终失败 SHALL 记入失败清单并继续，不阻断任务。

#### Scenario: 每页采集后立即下载
- **WHEN** 任务采集到新的一页帖子
- **THEN** 该页媒体在进入下一页采集前完成下载（或重试后记为失败）

#### Scenario: 下载重试不占动作名额
- **WHEN** 某媒体文件下载失败并重试
- **THEN** 动作窗口剩余名额不因下载重试减少

#### Scenario: 单资源失败不阻断
- **WHEN** 某帖子的一个媒体资源经重试仍下载失败
- **THEN** 该资源记入失败清单，任务继续处理其余资源与帖子

### Requirement: Persistent dedup manifest and resume

任务 SHALL 为每个 username 维护持久化清单（建议位于 `downloads/profile_{username}/.auto_state.json`），记录：帖子 shortcode 到完成/失败状态的映射、当前 cursor、失败列表、任务元信息。每完成一帖的下载 SHALL 落盘一次。后端重启后任务 SHALL 能从清单恢复：已完成的帖子不再重新采集下载（增量）；文件级去重以目标文件已存在且大小大于 0 为准。对同一 username 重新启动任务时 SHALL 从已记录进度增量续跑，只采集清单未覆盖的新帖。

#### Scenario: 崩溃后重启续传
- **WHEN** 任务运行中后端进程崩溃并重启，随后任务被再次启动
- **THEN** 已完成帖子不重复下载，采集从清单记录的进度继续

#### Scenario: 已存在文件跳过
- **WHEN** 目标文件在磁盘上已存在且大小大于 0
- **THEN** 下载被跳过并计入"已跳过"，不发起 CDN 请求

#### Scenario: 增量重跑
- **WHEN** 任务 `done` 后该用户新发了帖子，用户再次启动自动任务
- **THEN** 任务只采集并下载清单未记录的新帖

### Requirement: Risk-signal pause and automatic resume

命中风控信号时任务 SHALL 立即停止当次采集（不重试）并进入 `paused`，暂停原因与预计恢复时间（冷却剩余加少量随机余量）SHALL 在状态接口可见。冷却结束后任务 SHALL 自动恢复并按重新生成的窗口日程继续。一个自然日内信号命中达到预算上限时任务 SHALL 当日停跑（见 `account-safety` 的信号预算），状态接口 SHALL 展示"次日恢复"。

#### Scenario: 冷却暂停与自动恢复
- **WHEN** 采集中命中限流信号
- **THEN** 任务转入 `paused`（原因：冷却中，预计恢复时间可见），冷却结束后自动恢复继续

#### Scenario: 信号预算用尽当日停跑
- **WHEN** 当日风控信号命中次数达到预算上限
- **THEN** 任务当日不再自动恢复，状态接口展示信号预算已用尽

### Requirement: Consecutive-failure circuit breaker

连续多次（默认 3，可配置）非信号类采集失败（如解析失败、页面结构变更、登录态失效）时，任务 SHALL 熔断挂起（`paused`，原因：熔断），MUST NOT 滚动新窗口继续消耗名额，等待人工确认恢复或保持挂起。单次失败后连续成功 SHALL 将失败计数清零。

#### Scenario: 连续失败触发熔断
- **WHEN** 任务出现连续 3 次非信号类采集失败
- **THEN** 任务挂起不再消耗窗口名额，状态接口展示熔断原因

#### Scenario: 失败计数可清零
- **WHEN** 失败 2 次后第 3 次采集成功
- **THEN** 连续失败计数归零，任务继续正常运行

### Requirement: Keep profile page warm and rehydrate

任务活跃期间系统 SHALL 尽量保持该 username 的持久采集页可续传（不因缓存 TTL 被淘汰）；当缓存/页面因故丢失（如冷却过久、进程重启）时，任务恢复 SHALL 将清单中的已采集数据回灌采集层缓存后续滚，避免为重建深度进度而重复消耗大量动作名额。

#### Scenario: 长暂停后回灌续滚
- **WHEN** 任务因冷却暂停超过缓存 TTL 后恢复
- **THEN** 已采集的帖子数据从清单回灌，翻页从既有进度附近继续而非从零重建

### Requirement: Progress observability

状态接口 SHALL 至少暴露：任务状态与暂停原因、已下载/已跳过/失败计数、已知总帖数与当前进度（可得时）、当前 cursor 与是否还有更多、下一个动作预计执行时间、最近失败明细列表。前端 SHALL 能以短轮询展示以上信息。

#### Scenario: 状态字段完整
- **WHEN** 任务运行中客户端查询状态接口
- **THEN** 响应包含状态、暂停原因（如有）、下载/跳过/失败计数、进度、下一个动作预计时间与最近失败列表

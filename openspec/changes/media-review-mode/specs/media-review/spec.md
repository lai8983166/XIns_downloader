## Purpose

对已下载到 `downloads/profile_{username}/` 的媒体文件做人工筛选：逐文件三档判定（保留 / 候选 / 删除），动作即时生效，纯文件级粒度，全程不联网、不触碰采集链路与风控状态。

## ADDED Requirements

### Requirement: Folder and file enumeration

系统 SHALL 提供 `GET /review/folders` 列出 `DOWNLOAD_ROOT` 下可作为审查目标的文件夹（至少包含名为 `profile_*` 且含媒体文件的目录），每项含文件夹名与待审文件数。系统 SHALL 提供 `GET /review/files?folder=<name>` 列出该文件夹**顶层**的媒体文件（按名称排序，含文件名、类型 image/video、字节大小），MUST 排除 `.` 开头的隐藏文件（清单 `.auto_state.json`、边车 `.auto_nodes.jsonl` 等）、子目录（含 `_candidate/`）与非媒体扩展名文件。folder 参数非法（不存在、含路径分隔符或 `..`）时 SHALL 返回 400。

#### Scenario: 列出可审查文件夹
- **WHEN** 客户端 `GET /review/folders`
- **THEN** 响应包含每个含媒体文件的 `profile_*` 文件夹及其待审文件数，不含空文件夹与 `downloads` 根之外的目录

#### Scenario: 列出文件夹内待审文件
- **WHEN** 客户端 `GET /review/files?folder=profile_user1`
- **THEN** 响应为该文件夹顶层媒体文件列表（名称排序），不含 `.auto_state.json`、`.auto_nodes.jsonl` 与 `_candidate/` 内文件

#### Scenario: 非法 folder 参数
- **WHEN** 客户端传入含 `..`、`/`、`\` 或不存在的 folder
- **THEN** 系统返回 400，不访问文件系统之外的位置

### Requirement: Path-confined local file serving

系统 SHALL 提供 `GET /review/file/{folder}/{filename}` 从 `DOWNLOAD_ROOT` 内读取并返回本地媒体文件，Content-Type 按扩展名正确设置（图片 `image/jpeg` 等、视频 `video/mp4`）。folder 与 filename SHALL 受与枚举接口相同的路径约束，任何试图逃离 `DOWNLOAD_ROOT` 的请求 MUST 被拒绝（400）。

#### Scenario: 读取本地图片
- **WHEN** 客户端请求合法 folder/filename 的 jpg 文件
- **THEN** 响应 200，Content-Type 为 `image/jpeg`，内容为该文件字节

#### Scenario: 读取本地视频
- **WHEN** 客户端请求合法 mp4 文件
- **THEN** 响应 200，Content-Type 为 `video/mp4`，浏览器可直接播放

#### Scenario: 路径穿越被拒
- **WHEN** filename 或 folder 含 `..`、绝对路径或分隔符
- **THEN** 系统返回 400，不读取任何 `DOWNLOAD_ROOT` 之外的文件

### Requirement: Three-way immediate file action

系统 SHALL 提供 `POST /review/action`（`{folder, filename, action}`，action ∈ `keep|candidate|delete`）：
- `keep`：SHALL 不做任何文件系统操作（文件留在源文件夹）；
- `candidate`：SHALL 将文件移动到同文件夹下 `_candidate/` 子目录（不存在则创建；同名冲突 SHALL 报 409）；
- `delete`：SHALL 硬删除文件（`os.remove` 级语义，**不可撤销，不进回收站**——用户明确选定的语义）。

动作 SHALL 即时执行（单个请求单个文件即时落盘），成功后响应含执行结果。文件不存在或已移动 SHALL 返回 404；路径非法返回 400。粒度为**纯文件级**：每个文件独立判定，不按 shortcode/帖分组，也 MUST NOT 波及同帖其他文件或清单记录。

#### Scenario: 候选移动
- **WHEN** 对 `profile_user1/ABC_1.jpg` 执行 action=candidate
- **THEN** 文件移动到 `profile_user1/_candidate/ABC_1.jpg`，原位置不再存在

#### Scenario: 硬删除
- **WHEN** 对某文件执行 action=delete
- **THEN** 文件被立即删除且不可恢复，同帖其他文件与 `.auto_state.json` 均不受影响

#### Scenario: 保留为空操作
- **WHEN** 对某文件执行 action=keep
- **THEN** 不发生任何文件系统变更，响应成功

#### Scenario: 目标文件不存在
- **WHEN** 对已被移动/删除的文件再次执行动作
- **THEN** 系统返回 404

### Requirement: Keyboard-driven review flow

前端 SHALL 提供审查界面：选择文件夹后逐文件大图浏览，键盘 `1`/`2`/`3` 分别对应 保留/候选/删除 且按键后 SHALL 立即执行动作并自动前进到下一文件；`←`/`→`（或等价控件）切换当前文件；SHALL 提供网格总览以便跳转。审查过程中界面 SHALL 实时展示进度（已审/总数）与最近动作结果。

#### Scenario: 快捷键判定并前进
- **WHEN** 审查者在大图视图按 `2`
- **THEN** 当前文件移入 `_candidate/`，视图自动切到下一文件，进度计数 +1

#### Scenario: 进度实时可见
- **WHEN** 审查进行中
- **THEN** 界面显示已审 X / 共 Y，且数字随动作即时更新

#### Scenario: 视频可预览
- **WHEN** 当前待审文件为 mp4
- **THEN** 大图视图以内嵌播放器呈现，可播放后再判定

### Requirement: Review does not touch collection state

审查 SHALL 只操作文件系统（移动/删除），MUST NOT 修改 `.auto_state.json`、`.auto_nodes.jsonl` 或任何采集/调度状态。已知并接受的边界：已被自动任务记录为完成的帖子不受文件增删影响（不重下）；**未进清单**的文件被移入 `_candidate/` 后，未来对该账号的自动任务可能在顶层重新下载同名文件。

#### Scenario: 清单不受审查影响
- **WHEN** 审查删除/移动若干文件后读取 `.auto_state.json`
- **THEN** 清单内容与审查前逐字节一致

#### Scenario: 已审文件不因清单重下
- **WHEN** 已在清单记录为 done 的帖子的文件被删除后，对该账号再次运行自动任务（增量）
- **THEN** 该帖被帖子级跳过，被删文件不被重新下载

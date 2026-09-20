## ADDED Requirements

### Requirement: Dedicated backup account isolation
采集流程 MUST 仅使用专用备用账号；操作者的主 Instagram 账号 MUST 不得参与任何采集、登录或
会话。备用账号应视为可弃用资源，其风险与主账号物理隔离。

#### Scenario: 主账号永不接触采集
- **WHEN** 配置浏览器持久化登录态
- **THEN** 仅记录专用备用账号的登录态；主账号的凭据不会出现在采集链路的任何环节

### Requirement: Persistent login via one-time manual headed login
首次建立会话 MUST 通过有头浏览器由人工完成一次性登录（含 2FA / 人机验证 / 挑战页），
登录态 MUST 持久化到 `storage_state` 与持久 `userDataDir`，供后续无头运行复用。系统 MUST 不得
对备用账号执行自动化登录（自动填表提交用户名密码）。

#### Scenario: 首次登录
- **WHEN** 操作者运行登录脚本建立会话
- **THEN** 以有头模式打开浏览器，由人工完成登录与任何验证，退出时登录态已落盘可复用

#### Scenario: 后续运行复用登录态
- **WHEN** 服务以无头模式启动并执行采集
- **THEN** 复用已落盘的登录态，不再触发登录流程

### Requirement: Human-like pacing
所有面向 Instagram 的动作 MUST 采用人类化节奏：动作间延迟 SHALL 为带随机抖动的区间值（不得为
固定常数），Profile 翻页 SHALL 通过逐步滚动而非瞬时跳转触发加载。系统 MUST 不得以固定等间隔或
最大并发执行采集动作。

#### Scenario: 节奏带随机抖动
- **WHEN** 采集器连续执行多次导航/滚动
- **THEN** 相邻动作之间的等待时长在配置的随机区间内变化，不出现固定间隔

### Requirement: Serialized single browser session
采集 MUST 通过单一常驻持久化浏览器上下文执行，且同一时刻至多进行一次 Instagram 导航/滚动。
系统 MUST 不得为提升吞吐而并发打开多个页面/标签同时刷取 Instagram。

#### Scenario: 请求串行化
- **WHEN** 多个采集请求几乎同时到达
- **THEN** 它们在同一浏览器上下文中按序执行，任意时刻仅一个请求正在与 Instagram 交互

### Requirement: Signal detection and cooldown
采集器 MUST 在每次导航/响应后检测风控与挑战信号：登录墙、挑战页（`/challenge`、"/confirm"）、
限流提示（如 "please wait a few minutes"、"Try Again Later"）、"Action Blocked"、异常重定向或
空/非预期响应。一旦命中，系统 MUST 立即停止当次采集且不重试，将当前会话置为"冷却中"
（指数退避，如 15→30→60 分钟），并向客户端返回 429/503 与可读说明；冷却期内所有采集请求 MUST
被直接拒绝。

#### Scenario: 命中限流信号
- **WHEN** Instagram 返回限流/挑战信号
- **THEN** 系统立即停止当次采集、不重试，会话进入冷却，客户端收到 429/503 及说明

#### Scenario: 冷却期拒绝新请求
- **WHEN** 会话处于冷却期且有新采集请求到达
- **THEN** 系统直接拒绝并返回冷却提示，不再与 Instagram 交互

### Requirement: Session and daily quota caps
系统 MUST 维护会话级与每日（按本地自然日）采集动作计数，并在触及上限时拒绝请求、返回 429 及
提示次日再试。默认阈值 MUST 取保守值且可配置。

#### Scenario: 触及每日上限
- **WHEN** 当日采集动作数达到每日上限
- **THEN** 系统拒绝后续采集请求并返回 429，提示次日再试

### Requirement: Read-only enforcement
采集器 MUST 仅执行：页面导航、滚动、读取网络响应、下载 CDN 媒体。系统 MUST 不得执行任何写操作
（点赞、关注、评论、收藏、分享等），且代码层面 MUST 不提供此类方法。

#### Scenario: 不产生任何写动作
- **WHEN** 采集流程执行完毕
- **THEN** 仅发生读取与导航/滚动，未对 Instagram 产生任何点赞/关注/评论等写副作用

### Requirement: instaloader fallback is off by default and never auto-invoked
instaloader MUST 默认关闭，仅可由人工显式开关启用。它 MUST 仅在浏览器采集失败且该失败并非限流/
挑战信号时、仅用于**单帖**场景（`/preview`、`/download`）作为应急。系统 MUST 不得对 Profile 接口
提供 instaloader 兜底，且 MUST 不得在单次请求内自动回退到 instaloader。

#### Scenario: 默认不回退
- **WHEN** 浏览器采集失败且未人工开启 instaloader 开关
- **THEN** 系统向客户端返回采集失败错误，不调用 instaloader

#### Scenario: 限流时禁止兜底
- **WHEN** 浏览器采集因限流/挑战信号失败
- **THEN** 即使 instaloader 开关已开启，系统也不调用 instaloader，而是进入冷却并返回 429/503

#### Scenario: Profile 接口无 instaloader 兜底
- **WHEN** Profile 采集失败
- **THEN** 系统不回退到 instaloader（无论开关状态），Profile 接口不提供 instaloader 路径

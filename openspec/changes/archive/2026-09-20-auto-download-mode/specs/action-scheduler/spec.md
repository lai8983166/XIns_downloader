## Purpose

把"每小时最多 N 个采集动作"从被动计数升级为主动排程：每个小时窗口开启时生成带随机抖动、带最小间隔的动作日程，自动任务按日程点执行；手动操作立即执行但共享同一窗口预算。

## ADDED Requirements

### Requirement: Hourly action window budget

系统 SHALL 以小时窗口为预算桶，限制面向 Instagram 的采集动作总数（手动与自动共享同一预算），默认上限 300 次/窗口，可经 `XINS_SESSION_ACTION_LIMIT` 覆盖。窗口名额用尽时：自动任务等待下一窗口；手动请求被拒绝并返回 429 语义与可读说明。

#### Scenario: 手动请求在窗口满时被拒
- **WHEN** 当前窗口名额已用尽且收到手动采集请求（如 `/preview`）
- **THEN** 系统返回 429 与"窗口配额已满"说明，不发起采集

#### Scenario: 自动任务在窗口满时等待
- **WHEN** 自动任务请求下一个动作槽位但当前窗口名额已用尽
- **THEN** 系统开启新窗口并让任务按新日程继续，任务不失败、不报错

### Requirement: Jittered-grid schedule generation

每个窗口开启时，系统 SHALL 生成与剩余名额等量的动作时间点：将窗口时长等分为 N 格，每格内施加可配置的随机抖动（jitter），并对全部时间点强制任意相邻两点的间隔不小于 `min_gap`（默认 8 秒，可配置；需 ≤ 窗口时长/名额，300 名额/3600s 时为 12s，8s 下限保证上限可达且防扎堆）。生成结果 MUST NOT 呈固定等间隔，MUST NOT 存在间隔小于 `min_gap` 的相邻点。

#### Scenario: 日程点随机且不扎堆
- **WHEN** 系统为一个上限 300 的窗口生成日程
- **THEN** 任意相邻两个日程点的间隔均不小于 `min_gap`，且相邻间隔数值互不相同（非固定等间隔）

#### Scenario: 配置变更生效
- **WHEN** 通过环境变量覆盖抖动系数或最小间隔后重启服务
- **THEN** 新窗口的日程生成使用新配置值

### Requirement: Slots are earliest-start times

日程点 SHALL 语义为"最早可执行时间"而非固定时刻：某动作的实际开始时刻不早于 `max(日程点, 上一动作完成时刻 + min_gap)`。当失败重试或下载耗时拖延进度时，后续日程点 SHALL 顺延（窗口整体拉伸），系统 MUST NOT 为追赶日程而压缩动作间隔或并发执行采集。

#### Scenario: 前一动作拖延则后续日程顺延
- **WHEN** 某次采集动作因页面缓慢或重试耗时长于预期
- **THEN** 其后的日程点按"上一动作完成 + min_gap"顺延执行，动作间实际间隔仍不小于 `min_gap`

### Requirement: Window rollover on exhaustion

窗口是预算桶而非墙钟周期：当窗口名额耗尽（含被失败动作消耗）时，系统 SHALL 立即开启新窗口并生成新日程，MUST NOT 等待窗口自然结束的整点。窗口内名额未用尽而任务提前完成时，窗口自然闲置。

#### Scenario: 提前用尽名额立即滚动
- **WHEN** 窗口内 300 个名额在 40 分钟内全部消耗
- **THEN** 系统立即开启新窗口并生成新日程，自动任务连续运行

### Requirement: Schedule invalidation on risk signal

命中风控信号进入冷却时，系统 SHALL 作废当前窗口的剩余日程与剩余名额；冷却结束恢复采集时 SHALL 重新生成窗口日程，MUST NOT 沿用冷却前未执行的旧日程点。

#### Scenario: 冷却恢复后重排日程
- **WHEN** 会话因风控信号进入冷却随后冷却结束
- **THEN** 恢复后的动作来自重新生成的新窗口日程，而非冷却前剩余的旧日程点

### Requirement: Manual path bypasses the schedule

手动采集操作（如 `/preview`、`/profile/preview`）SHALL 不经日程排队、立即执行，但 SHALL 消耗当前窗口一个名额；手动动作完成后，自动任务的下一个执行点 SHALL 不早于该手动动作完成时刻 + `min_gap`。

#### Scenario: 手动操作即时执行并扣名额
- **WHEN** 自动任务正在按日程运行时用户手动请求 `/preview`
- **THEN** 手动请求立即执行（不等待日程点），当前窗口剩余名额减一

#### Scenario: 手动动作推后自动日程
- **WHEN** 手动动作于时刻 T 完成
- **THEN** 自动任务的下一次采集不早于 T + `min_gap` 执行

### Requirement: No daily action quota

系统 MUST NOT 维护或执行每日采集动作配额。日级风险控制由风控信号预算（见 `account-safety`）承担，小时级节奏控制由本能力的窗口预算承担。

#### Scenario: 连续执行不受每日计数限制
- **WHEN** 自动任务跨多个自然日持续运行且每日窗口预算未用尽
- **THEN** 任务不被任何每日动作计数阻断，仅在风控信号预算或熔断触发时停

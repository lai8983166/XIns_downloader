## REMOVED Requirements

### Requirement: Session and daily quota caps
**Reason**: 实测表明每日动作配额无必要且过紧；小时级节奏控制已由 `action-scheduler` 能力的每小时窗口预算（默认 300，原会话上限 60）与拟人日程完整承载，且手动与自动共享同一预算。
**Migration**: `XINS_DAILY_ACTION_LIMIT` 环境变量不再生效；`XINS_SESSION_ACTION_LIMIT` 语义由"60 分钟滑动窗口计数"变为"每小时窗口预算"，默认值 60→300。日级风险控制改由下方"每日风控信号预算"承担。

## ADDED Requirements

### Requirement: Daily risk-signal budget

系统 SHALL 按本地自然日统计风控信号（限流 / Action Blocked / 挑战页 / 登录墙等，由既有信号检测产生）的命中次数。当日命中次数达到预算上限（默认 3，可经 `XINS_DAILY_SIGNAL_LIMIT` 配置）时，所有自动采集任务 SHALL 当日停止自动恢复，直至次日或人工确认恢复。手动采集路径不受信号预算限制，但仍受冷却状态机约束。信号预算的计数 SHALL 在达到上限前的日常运行中对正常任务无感知。

#### Scenario: 达到上限当日停跑
- **WHEN** 当日风控信号命中次数累计达到预算上限
- **THEN** 自动任务当日不再自动恢复采集，状态展示信号预算已用尽

#### Scenario: 次日自动恢复
- **WHEN** 信号预算用尽导致任务停跑后进入下一自然日
- **THEN** 信号预算计数重置，被停跑的自动任务可自动恢复

#### Scenario: 手动路径不受信号预算限制
- **WHEN** 信号预算已用尽且不在冷却期
- **THEN** 手动采集请求仍按冷却状态机的判定执行，不被信号预算直接拒绝

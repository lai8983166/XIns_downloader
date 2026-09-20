## Context

现状（`migrate-to-playwright-collector` 已落地的架构）：采集主路径为 patchright 浏览器 + 捕获
网络响应；`collector/quota.py` 的 `QuotaGuard` 维护"60 分钟滑动窗口 ≤ 60 次 + 每日 ≤ 120 次"两
级计数，采集函数内部调用 `quota.acquire()`；`collector/safety.py` 的信号检测 + 阶梯冷却
（15→30→60→120→360 min）负责风控兜底；Profile 分页为整型 cursor 偏移 + 内存缓存
（TTL 600s）+ 持久 page 续传（`collector/instagram_collector.py`）。

约束：单人自用、Windows、硬约束——专用备用账号不得被封。接口契约方面，现有
`/preview`、`/profile/preview` 等手动路径的请求/响应模型保持不变；本变更新增独立路由。

实测反馈（操作者）：每日配额无必要；小时配额保留且提高（定为 300）；核心诉求是"窗口内
什么时刻执行"由系统以拟人随机日程决定，并容忍失败重试导致的窗口拉伸。

## Goals / Non-Goals

**Goals:**
- 自动模式：一个主页链接 → 后台任务自动翻页、下载全部媒体，直到 `has_more=false`。
- 三层去重（采集层 `have_codes` / 文件层 exists+size>0 / 帖层持久化清单）与崩溃续传、增量重跑。
- 配额改造：`QuotaGuard` → `ActionScheduler`（每小时窗口预算 300 + jittered-grid 日程 +
  min_gap + 名额用尽即滚动 + 冷却作废重排）；删除每日动作配额。
- 日级安全网从"动作计数"改为"风控信号计数"（每日信号预算，默认 3）。
- 手动操作保持即时响应，不被日程排队，但共享窗口预算。

**Non-Goals:**
- 不做进程级任务持久守护（后端重启不自动拉起任务；清单使重启动=增量续跑）。
- 不做多账号/多任务并行（单任务串行，与现有浏览器串行锁一致）。
- 不做"成簇浏览"（burst）节奏模式——预留配置项结构，实现留待后续按需加。
- 不改 Threads / X 采集逻辑（Threads 仅随 quota 重写同步 import 与名额语义——名额改由
  其手动路由在 main.py 统一 acquire_now；自动调度仅接入 Instagram）。
- 不做 Stories、高清原图变体等新内容类型。

## Decisions

### D1：调度器替代配额计数器——单一预算桶、双消费路径

- **选择**：`collector/quota.py` 重写为 `ActionScheduler`（导出名 `scheduler`），删除每日计数。
  窗口语义：**预算桶**而非墙钟小时——首个动作到来时开启窗口（anchor=now），名额用尽立即
  以 `max(now, 上一动作完成 + min_gap)` 为新 anchor 开启下一窗口；不等整点、不跨窗口追补。
- **接口**：
  - `await wait_for_slot()`：自动任务专用。睡到下一个日程点（可被取消/中断），隐含扣名额。
  - `acquire_now()`：手动路径专用。立即放行并扣 1 名额；窗口满则抛 `QuotaExceeded`（保留
    该异常类型，路由层仍映射 429）。同时记录"最近动作完成时刻"，使自动任务下一日程点
    不早于 `手动完成 + min_gap`。
  - `invalidate()`：冷却/熔断时作废当前窗口剩余日程；下次任何 acquire 重新开窗生成日程。
  - `status()`：窗口剩余名额、下一动作 ETA（供 job 状态接口与调试出口）。
- **备选与否决**：保留 `QuotaGuard` 给手动 + 独立 scheduler 给自动（两套预算会相互失序，
  总速率不可控，且手动高峰会绕过预算）；墙钟整点窗口（整点边界前后可能出现两个窗口的
  名额连用，形成事实上的双倍速率峰值）。

### D2：日程生成 = jittered grid（非纯随机）

- **算法**：窗口时长 T、名额 N、抖动系数 j（默认 0.35）、最小间隔 g（默认 8s）。
  `base = T/N`；`t_i = anchor + i*base + U(-j*base, +j*base)`；排序后自左向右强制
  `t_{i+1} = max(t_{i+1}, t_i + g)`；尾部若被推移溢出窗口自然顺延（窗口本就是预算桶）。
- **理由**：纯均匀随机在 N=300 时有可观的扎堆概率（相邻 <10s）与长空窗；等间隔则是
  最典型的机器人特征。抖动网格兼具"看起来无规律"与"永不忘间隔下限"。
- **min_gap 默认 8s 而非更早草案的 25s（实施期修正）**：N=300、T=3600s 时 base=12s，
  g=25 会使 300 名额永远消费不完（实际被钳在 ~144/小时）且网格退化为准等间隔，
  与"上限可达 + 非等间隔"两目标冲突；g=8 < base=12 使两者同时成立。
- **随机性注意**：仅用 `random.uniform`，与现有 `instagram_collector.py` 的人类化抖动同源；
  不引入 `random.Random()` 独立实例需求（单进程单事件循环）。

### D3：日程点是"最早可执行时间"——`max(slot, prev_finish + min_gap)`

- **选择**：`wait_for_slot()` 内部计算
  `next_start = max(下一日程点, scheduler.last_finish + min_gap)`。任务循环里一次动作的
  "完成"= 采集返回（含其内部导航/滚动）+ 该页媒体下载全部落定（下载并发见 D6）之后，
  调 `scheduler.report_finish()` 打点。
- **理由**：失败重试、慢页面、大视频下载天然把后续日程往后推，无需任何"超时检测与重排"
  逻辑；一窗口任务超一小时是正常结果，窗口定义不依赖墙钟。
- **注意**：`prev_finish` 由手动 `acquire_now()` 与自动 `report_finish()` 共同推进
  （手动也是一次真实动作）。

### D4：名额消费从采集函数内部移到调用方

- **选择**：`preview_post` / `preview_profile` **移除** `quota.acquire()`，仅保留
  `cooldown.check()` 与信号检测。名额消费移至：自动任务循环（`wait_for_slot`）与手动路由
  （main.py 里 `scheduler.acquire_now()`）。
- **理由**：同一次动作的"何时做"（调度）与"风控刹车"（冷却/信号）职责分离；采集函数
  保持纯采集，测试无需模拟调度器；自动/手动两条路径的消费语义天然清晰。
- **兼容**：`/preview`、`/profile/preview` 响应模型不变；新增的 429 触发条件（窗口满）
  对手动用户表现为"稍后再试"，与原 quota 行为一致。

### D5：AutoJob = 进程内 asyncio 任务 + 双文件持久化（清单 + nodes 边车）

- **选择**：新增 `collector/auto_job.py`。`AutoJobManager`（进程级单例）持有
  `{job_id: AutoJob}`；`AutoJob` 是一个 asyncio Task + 状态机
  （`running / paused_{cooldown|signal_budget|circuit} / done / failed / cancelled`）。
  同一 username 存在活跃任务时拒绝重复启动。
- **持久化两文件**（都在 `downloads/profile_{username}/`）：
  - `.auto_state.json`：小清单——username、cursor、exhausted、`posts: {shortcode:
    {status, files, date_utc}}`、failures、计数、updated_at。**每帖落盘一次**，写法为
    临时文件 + `os.replace`（原子，防半写损坏）。
  - `.auto_nodes.jsonl`：原始采集 nodes 边车（供缓存丢失后回灌；见 D7）；运行中按 code
    去重增量 append，任务收尾（done）时用最终缓存快照整写一次以保持时间线顺序。
- **增量刷新模式（实施期补充）**：对 `exhausted=true` 的清单再次启动任务时进入 refresh
  模式——丢弃缓存全新捕获（保证时间线顺序，新帖在头部）、cursor 归零从头翻、
  遇"整页均为已知完成帖"即判定已到旧覆盖区而收尾；中途崩溃恢复（`exhausted=false`）
  则走回灌续滚路径，两者互斥。
- **重启语义**：后端重启 → 任务消亡、注册表清空；用户再次 `POST /profile/auto` 时读取
  清单增量续跑（这正是 Non-Goal"不做自动拉起"的成本边界：重启后一键续跑，无需从零）。
- **备选与否决**：SQLite（单机自用场景过度工程）；仅内存状态（崩溃即重采，违背去重目标）。

### D6：下载执行器——页内小并发 + .part 原子落盘 + 免名额重试

- **选择**：页级下载用 `asyncio.Semaphore(XINS_AUTO_DOWNLOAD_CONCURRENCY, 默认 3)` 并发
  （复用 main.py 的 `download_file`，header/UA 一致）；写盘先写 `<name>.part` 再
  `os.replace` 为最终名——文件级去重的 `size>0` 判定因此可信（不会把半截文件当完成）。
  单资源失败：退避重试 2 次（如 2s/8s），仍失败记入 failures 继续；下载与重试不占名额。
- **理由**：CDN（`cdninstagram.com`/`fbcdn.net`）下载与 instagram.com 的风控相互独立，
  已有实践（`/profile/download`）证明低风险；签名 URL 数小时过期决定了必须"边采边下"。

### D7：深翻页——任务活跃期保温 + nodes 回灌

- **保温**：任务循环每次翻页后 touch 该 username 的缓存条目（`fetched_at = now`），
  使 TTL 淘汰在任务存活期内不生效；持久 page 池 LRU 中该页常驻（任务是最主要使用者）。
- **回灌**：任务把每次翻页捕获的原始 nodes 增量写入 `.auto_nodes.json`；恢复路径
  （重启/长冷却后缓存已丢）先调 `collector` 新增的
  `rehydrate_profile_cache(username, nodes, exhausted)` 回灌 `_profile_cache`，再续传滚动。
  IG Web 时间线无 seek，重滚无法避免，但回灌保证：cursor 算术正确、不重复存储、
  `_scroll_until` 的 40 次滚动上限只约束单次调用（缓存累积使后续调用从既有深度继续）。
- **备选与否决**：调大 attempts 上限（治标且加剧单次会话请求密度）；纯 cursor 重滚不回灌
  （深度账号在缓存丢失后永远追不回进度）。

### D8：每日信号预算放在 safety 层，与冷却阶梯同源计数

- **选择**：`collector/safety.py` 增加按本地自然日的信号计数器（`daily_signals`），
  在 `cooldown.report()` 内顺带 +1；`AutoJob` 在收到信号类 `CollectorError` 后检查
  `daily_signals >= XINS_DAILY_SIGNAL_LIMIT(默认 3)`：达到则任务转
  `paused_signal_budget`，次日自动可恢复；未达到则按冷却暂停自动恢复（恢复点 =
  冷却剩余 + `XINS_AUTO_RESUME_JITTER_SEC` 默认 300s 的随机量）。
- **手动路径**：信号预算不拦手动请求（手动仍只受冷却约束）——预算管的是"无人值守的量"。
- **计数持久性**：进程内存计数即可。重启清零的可接受性论证：重启同时丢失温热缓存与任务，
  冷却阶梯（最高 360 min）仍是硬兜底；为计数引入持久化不值得。
- **备选与否决**：沿用每日动作配额（动作数与风险的相关性弱，实测过紧）；信号预算计数放
  AutoJob 内（信号可能在任务外被触发，如手动操作命中，集中计数更准）。

### D9：连续失败熔断与失败分类

- **选择**：任务循环对 `CollectorError` 分类：
  - 信号类（`rate_limited`/`login_required`）→ D8 路径；
  - `cooldown`/`quota` → 等待重排（调度器已处理）；
  - 其余（`not_found`/`parse`/`invalid_url` 等）→ 连续计数 +1，**连续**达
    `XINS_AUTO_FAIL_LIMIT(默认 3)` → `paused_circuit`，等待人工确认（状态接口提示）；
    任一次成功清零。熔断时同时 `scheduler.invalidate()`。
- **理由**：页面结构变更/登录态失效的表现是快速连败；不熔断则 300 名额在几分钟内烧光并
  立即滚新窗口继续烧，与"节奏控制"目标背道而驰。
- **profile 级 not_found 特判**：用户不存在/私密的 `not_found` 直接 `failed` 终态
  （重试无意义），不计入连续失败。

### D10：API 形状

```
POST /profile/auto            {profile: str, max_posts?: int>0}
                              → 202 {job_id, username}   # 已有活跃任务 → 409
GET  /profile/auto/jobs/{id}  → AutoJobStatus
POST /profile/auto/jobs/{id}/cancel → {status: "cancelling"|"cancelled"}
```
`AutoJobStatus`：`job_id, username, state, paused_reason?, resume_at?, counts{downloaded,
skipped, failed}, mediacount?, cursor, has_more, next_action_at?, recent_failures[],
updated_at`。前端 2s 轮询（不做 SSE——本地单用户，轮询足够且零新依赖）。
取消语义：置 cancel 标志，当前动作与页内下载完成后退出（不清单、不断文件）。

### D11：配置项（`collector/config.py`）

| 配置 | 默认 | 说明 |
|---|---|---|
| `session_action_limit` | 60→**300** | 每小时窗口预算（env `XINS_SESSION_ACTION_LIMIT`） |
| `daily_action_limit` | **删除** | 每日动作配额移除；env 不再生效 |
| `min_action_gap_sec` | 8 | 相邻动作最小间隔（`XINS_MIN_ACTION_GAP_SEC`；≤ 窗口/名额 才能使上限可达） |
| `slot_jitter` | 0.35 | 日程抖动系数（`XINS_SLOT_JITTER`） |
| `daily_signal_limit` | 3 | 每日风控信号预算（`XINS_DAILY_SIGNAL_LIMIT`） |
| `auto_page_size` | 12 | 自动任务每动作翻页帖数（`XINS_AUTO_PAGE_SIZE`） |
| `auto_fail_limit` | 3 | 连续失败熔断阈值（`XINS_AUTO_FAIL_LIMIT`） |
| `auto_download_concurrency` | 3 | 页内 CDN 下载并发（`XINS_AUTO_DOWNLOAD_CONCURRENCY`） |
| `auto_resume_jitter_sec` | 300 | 冷却恢复额外随机余量上限（`XINS_AUTO_RESUME_JITTER_SEC`） |

## Risks / Trade-offs

- [小时预算 60→300 抬高封号风险] → 兜底不变：信号检测 + 阶梯冷却 + 每日信号预算；预算可
  env 回调；文档明示"仅专用备用账号"。
- [窗口滚动边界处两窗口名额连用形成局部加速] → 新窗口 anchor 强制
  `max(now, last_finish + min_gap)`，min_gap 跨窗口连续生效。
- [深账号缓存丢失后重滚产生短时高密度请求] → 滚动节奏沿用 `scroll_delay`（1.5–3.5s/次）；
  保温 + nodes 边车把重滚频次压到"仅重启/长冷却后一次"。
- [`quota.acquire` 从采集函数移出后，新增调用方忘记扣名额] → 采集函数 docstring 与
  `__all__` 注明契约"名额由调用方负责"；tasks 中含回归测试（手动路径满窗 429、自动路径
  日程等待）。
- [清单/边车 JSON 损坏] → 原子写（tmp + `os.replace`）；启动读取失败时任务拒绝启动并
  报错指明文件路径（不静默重采）。
- [后端重启丢任务] → 明示 Non-Goal；清单使重启动 = 增量续跑，用户感知为"再点一次开始"。
- [`download_file` 串行写 `open(...)` 阻塞事件循环] → 现状已如此（页内并发 3 时的写放大
  有限）；如实测卡顿再引入 `asyncio.to_thread`，不阻塞本变更。

## Migration Plan

1. 先落 `ActionScheduler`（含 `test_safety_quota.py` 重写），手动路由切到 `acquire_now()`，
   此时系统行为 = 原配额语义的新实现（可独立验证、可独立回滚）。
2. 再落 `auto_job.py` + `/profile/auto*` 路由 + 前端面板（纯新增，不影响存量路由）。
3. 回滚：git revert 对应提交即可；新增文件均为独立文件，无存量数据迁移
   （`.auto_state.json` 首次出现于本变更）。

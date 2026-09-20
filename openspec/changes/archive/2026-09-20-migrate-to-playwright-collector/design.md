## Context

当前 `main.py`（641 行）的全部 Instagram 数据采集由 `instaloader` 支撑：`/preview`（单帖）、
`/profile/preview`（分页）、`/profile/download`、`/download`。instaloader 是"伪造请求的 API
客户端"——它自己构造对 Instagram 私有 GraphQL 的请求（自带查询哈希、伪造 Header）。
`profile.get_posts()` 会连续发起多次此类请求，实测在 Profile 根页面读取多条内容时很快触发
429 / "please wait a few minutes"，而重试 = 更密集的伪造请求 = 封号风险飙升。

前端 `frontend/src/App.jsx` 已锁定接口契约（`/preview`、`/profile/preview` 等的请求/响应结构），
因此本变更**只换数据来源层，不动接口**。账号策略已定为**专用备用账号**（与主账号完全隔离），
instaloader **保留为应急兜底、默认关闭、不自动回退**。

约束与干系人：单人开发/自用项目；运行环境 Windows；硬约束——账号不得被封禁。

## Goals / Non-Goals

**Goals:**
- 用 Playwright 驱动真实 Chromium，以"合法 Web 客户端"身份采集单帖与 Profile 内容，取代
  instaloader 成为主路径。
- 把对 Instagram 的请求恢复成"真人浏览"指纹：页面由真实浏览器加载，数据来自**捕获浏览器自身的
  网络响应**，而非我们伪造的请求。
- 系统性降低封号风险：专用账号隔离、持久化登录态、人类化节奏、信号检测与冷却、配额上限、只读。
- 接口契约逐字段不变，前端零改动。
- instaloader 作为受控兜底保留，默认关闭，绝不自动回退。

**Non-Goals:**
- 不做"更快/更高并发"的采集优化（安全优先于速度）。
- 不实现账号多开/代理池/IP 轮换（MVP 单账号、单浏览器、单机）。
- 不支持 Stories / Reels 之外的探索、搜索、关系链等复杂交互（仅单帖 + Profile 帖子流）。
- 不把 instaloader 升级为主路径或自动回退路径。
- 不改动前端代码或 Pydantic 响应模型字段。

## Decisions

### D1：数据获取 = 浏览器加载页面 + 拦截其自身网络响应（而非 DOM 抓取 / 请求重放）
- **选择**：用 Playwright 打开 Instagram 页面，监听 `page.on("response")`，捕获浏览器在加载/滚动
  过程中**自己发出的** GraphQL/`i.instagram.com/api/v1/...` 响应 JSON，从中解析媒体节点。
- **理由**：这些请求由 Instagram 官方 Web 客户端发出，指纹是"真人浏览器"，与 instaloader 伪造请求
  有本质区别——这是降低风控判定的根本。
- **实现备注（Phase 1 实测，2026-06）**：单帖页的主帖完整数据已内联在 SSR HTML 的 relay store
  （`<script data-sjs>`）中，浏览器**不再为单帖发独立 JSON**。故单帖采集改为从 `page.content()`
  解析 relay store（按 `code==shortcode` 定位 media 节点；media_type 1/2/8 + `image_versions2.candidates`
  取最大尺寸 + `video_versions` 取最大宽度）。这仍是"从浏览器加载的主文档内容提取"、不重放请求，
  D1 精神不变。**Profile 分页**仍走"滚动 + 捕获网络响应"（Phase 2）。
- **替代方案**：
  - *纯 DOM 抓取*（读 `img[src]`/节点）：脆弱，Instagram 频繁改版，且拿不到完整 sidecar/视频直链/caption/时间。
  - *我们自己重放 GraphQL（带 cursor）*：等于换了皮的 instaloader，重新引入伪造请求风险——**否决**。
  - *继续 instaloader*：本变更要解决的问题——**否决**。

### D2：反检测 = `patchright` + 持久化 `userDataDir`（已锁定）
- **选择**：用 `patchright`（针对 CDP/自动化指纹打了补丁的 Playwright fork，API 兼容、当前比
  `playwright-stealth` 更有效）替代裸 `playwright`，作为**最终反检测方案**；用
  `launch_persistent_context(userDataDir=...)` 维持一个真实可复用的浏览器配置目录。
- **理由**：裸 Playwright 的 Chromium 带明显自动化指纹（`navigator.webdriver`、CDD 检测、缺失插件），
  Instagram 能识别。持久化配置目录让账号像"老用户反复回来"，登录态/cookie/localStorage 自然续存。
- **替代方案**：
  - *裸 `playwright` + `playwright-stealth`*：stealth 已被部分站点识破，patchright 更稳。
  - *每次新建 context + 注入 cookie*：无持久化画像，更像机器，且需反复注入——**否决**。

### D3：账号 = 专用备用账号；登录 = 一次性有头手动登录，落盘 `storage_state`
- **选择**：仅用专用备用账号采集，主账号绝不接触采集流程。首次通过一个独立 CLI 脚本（如
  `login.py`）以**有头模式**打开浏览器，由人工完成登录（含 2FA/挑战），脚本退出前把登录态写入
  `storage_state` 文件与持久 `userDataDir`；之后服务以无头模式复用。
- **理由**：把封号风险物理隔离到可弃用账号；一次性手动登录避免自动登录被风控，且能处理 2FA/人机验证。
- **替代方案**：
  - *复用主账号*：违背硬约束——**否决**。
  - *用环境变量注入 sessionid 自动登录*：仍由 instaloader 使用，且 cookie 易失效——仅作为 instaloader
    兜底的输入，不作为浏览器主路径。

### D4：单浏览器常驻 + 串行化（asyncio.Lock / 有界队列）
- **选择**：FastAPI 启动时（`lifespan`）拉起**一个**持久化浏览器上下文，常驻；所有采集请求通过
  一个 `asyncio.Lock` 串行执行（同一时刻只有一次 Instagram 导航/滚动）。
- **理由**：①强制串行是"人类化节奏"的天然保证（真人不会同时开多 tab 刷）；②多 tab/高并发是最明显的
  机器人特征；③单浏览器便于集中管控配额与冷却。
- **替代方案**：*每请求新建浏览器*：开销大、画像碎片化、无法集中限流——**否决**。

### D5：人类化节奏 + 分页 = 滚动驱动 + 随机抖动 + 小批量
- **选择**：动作之间插入**随机抖动延迟**（保守默认，均可配置）：
  - 首次导航后等页面稳定：`random.uniform(2.0, 4.0)` 秒
  - 动作之间（导航/翻页/读帖）：`random.uniform(3.0, 7.0)` 秒
  - Profile 滚动步进之间：`random.uniform(1.5, 3.5)` 秒
- Profile 翻页通过**逐步滚动**触发 Instagram Web 自身的下一页请求并捕获（见 D1），而非我们重放
  请求；单次预览/分页返回**小批量**（与前端 `PROFILE_PAGE_SIZE=6` 一致）。
- **理由**：等间隔/固定节奏是机器人指纹；真人节奏有抖动。滚动驱动保证"下一页"由合法客户端发出。

### D6：Profile 分页契约调和（整型 offset ↔ Instagram opaque cursor）
- **背景**：前端用**整型 `cursor`**（0,1,2…）+ `limit` 做偏移分页；Instagram 实际用不透明字符串
  `end_cursor`。当前 instaloader 实现靠枚举 `get_posts()` 跳过 `index<cursor` 来伪造整型偏移。
- **选择**：在 API 边界保持**整型 offset**不变；内部为每个 username 维护一个**内存缓存**
  `(username → 已捕获帖子有序列表 + 当前 IG end_cursor + TTL)`。请求 `[cursor, cursor+limit)` 时：
  若已捕获不足，则在浏览器会话内继续滚动捕获直至够数，再切片返回。`next_cursor = cursor + 已返回数`。
  缓存设 TTL（如 10 分钟）与容量上限（LRU），避免无限增长。
- **理由**：不动前端契约；整型 offset 在浏览器滚动模型下仍可表达；缓存让"加载更多"不重复滚动已读内容。
- **风险**：缓存与真实 timeline 漂移 → 用 TTL + 滚动时以最新数据为准刷新。

### D7：信号检测 + 冷却（核心防封机制）
- **选择**：采集器在每次导航/响应后检测风控/挑战信号：登录墙（`/accounts/login`）、挑战页
  （`/challenge`、"/confirm"）、限流提示（"please wait a few minutes"、"Try Again Later"）、
  "Action Blocked"、异常重定向、空/非预期响应。**一旦命中：立即停止当次采集，不重试**，
  将会话标记为"冷却中"并按阶梯退避（保守默认，可配置）：
  **第 1 次 → 15 分钟，第 2 次 → 30 分钟，第 3 次 → 60 分钟，第 4 次及以后 → 120 分钟**，
  **上限 360 分钟（6 小时）**；连续 24 小时无信号则计数器归零。向客户端返回 429/503 + 可读中文说明。
  冷却期内所有采集请求直接拒绝。
- **理由**：限流/挑战出现时继续操作是封号主因。立即停手 + 长冷却是降低风险的关键。
- **额外**：账号安全层独立维护冷却状态机，采集层只负责"上报信号"。

### D8：配额上限（会话级 + 每日）
- **选择**：维护会话级与每日（按自然日，本地时区）动作计数（"动作"= 一次帖子/Profile 加载）。
  触及上限时拒绝并返回 429 + 提示次日再试。**保守默认（可配置）**：
  - 会话级：**每 60 分钟滑动窗口 ≤ 30 次读取动作**
  - 每日：**每本地自然日 ≤ 120 次读取动作**
  实际数值后续按观察调优。
- **理由**：即使节奏正常，总量过大也会触发风控；硬上限是最后一道闸。

### D9：严格只读
- **选择**：采集器只允许：导航、滚动、读取网络响应、下载 CDN 媒体。**禁止**任何点击
  like/follow/comment/save/分享 等写操作，代码层面不提供此类方法。
- **理由**：写操作的自动化是高权重封号信号；只读把风险面压到最小。

### D10：instaloader 兜底纪律（默认关闭 / 不自动回退）
- **选择**：instaloader 代码与依赖保留；默认 `ENABLE_INSTALOADER_FALLBACK=false`。仅在**人工显式开启**
  且 Playwright 采集失败（且失败**不是**限流/挑战信号——否则禁止兜底）时，**仅单帖**场景
  （`/preview`、`/download`）可走 instaloader。**Profile 接口不提供 instaloader 兜底**（那正是它失败之处）。
  绝不在一次请求内自动回退。
- **理由**：自动回退会把限流/封号风险悄悄重新引入主路径，违背本变更初衷。

### D11：与 FastAPI 集成 = 换数据源，留模型
- **选择**：新增 `collector/` 包（`browser_session.py` 浏览器生命周期+锁、`instagram_collector.py`
  采集+解析+节奏、`safety.py` 信号检测+冷却状态机、`quota.py` 计数器）。重构 `main.py` 路由：
  把 `create_loader()`/`instaloader.*` 调用替换为 `collector` 调用；Pydantic 模型、校验、`httpx`
  下载与 `/media-proxy` 保持原样。浏览器在 `lifespan` 启动/关闭。
- **下载**：仍走现有 `httpx` 直拉 CDN（`cdninstagram.com`/`fbcdn.net`），仅把"媒体 URL 从哪来"
  换成采集器结果；CDN 直链公开可取、低风险。

### D12：配置默认值（保守，可调）
实现时直接对照下表，全部经环境变量覆盖、默认值取保守档：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `XINS_BROWSER_USER_DATA_DIR` | `./.browser/profile` | 持久化浏览器配置目录 |
| `XINS_BROWSER_STORAGE_STATE` | `./.browser/state.json` | 登录态落盘路径 |
| `XINS_NAV_STABILIZE_MIN/MAX_SEC` | `2.0` / `4.0` | 首次导航后等待页面稳定 |
| `XINS_ACTION_DELAY_MIN/MAX_SEC` | `3.0` / `7.0` | 动作间随机抖动延迟 |
| `XINS_SCROLL_DELAY_MIN/MAX_SEC` | `1.5` / `3.5` | Profile 滚动步进间延迟 |
| `XINS_SESSION_WINDOW_MINUTES` | `60` | 会话级滑动窗口时长 |
| `XINS_SESSION_ACTION_LIMIT` | `30` | 窗口内最大读取动作数 |
| `XINS_DAILY_ACTION_LIMIT` | `120` | 每自然日最大读取动作数 |
| `XINS_COOLDOWN_STEPS_MIN` | `15,30,60,120` | 命中信号的阶梯退避（分钟） |
| `XINS_COOLDOWN_MAX_MINUTES` | `360` | 单次冷却上限（6 小时） |
| `PROFILE_PAGE_SIZE` | `6` | Profile 单页帖子数（沿用前端） |
| `ENABLE_INSTALOADER_FALLBACK` | `false` | instaloader 应急兜底开关（默认关闭） |

## Risks / Trade-offs

- **[非零封号风险]** → 用专用备用账号物理隔离主账号；patchright + 持久画像 + 严格只读 + 信号检测 +
  配额上限多层缓解；起步即保守阈值。仍无法承诺绝对零风险（这是任何自动化的客观现实）。
- **[采集更慢]**（真实节奏 + 滚动驱动） → 接受，安全优先；客户端已有 loading 态。
- **[浏览器进程崩溃/泄漏]** → `lifespan` 关闭时清理；采集器加守护：检测浏览器失活则重启上下文。
- **[Instagram 改版导致捕获解析失效]** → 命中查询哈希/端点的匹配保持宽松（多关键字 OR），
  解析容忍缺字段；保留 instaloader 兜底（单帖）作为应急。
- **[patchright 版本滞后于 Playwright]** → 锁定可用版本组合；必要时回退到 `playwright`+`stealth`。
- **[Profile 整型 cursor 缓存漂移]** → TTL + 滚动时以最新数据刷新；缓存仅为性能优化，可随时失效重建。
- **[Windows 有头登录依赖本机 GUI]** → 登录为一次性、由操作者在开发机执行；服务运行期无头。
- **[资源开销上升]**（常驻 Chromium） → 自用 MVP 可接受；后续如需可拆独立 worker（非目标）。

## Migration Plan

- **Phase 0（基建）**：加依赖（`patchright`/`playwright`、反检测）；`playwright install chromium`；
  建 `collector/` 骨架；写 `login.py` 完成一次性有头登录并落盘 `storage_state`/`userDataDir`；
  离线验证网络响应捕获与解析。
- **Phase 1（单帖）**：实现 `preview_post(shortcode)`（采集+解析+节奏+信号检测）；用开关隔离，
  与 instaloader 并存；用脚本比对两者返回的 `PreviewResponse` 字段一致性。
- **Phase 2（Profile 分页）**：实现滚动驱动 + 网络捕获；落地整型-offset 缓存（D6）；对接
  `/profile/preview`/`/profile/download`，校验 `ProfilePreviewResponse` 字段一致。
- **Phase 3（切换主路径）**：开关翻转，采集器为默认主路径；instaloader 默认关闭、收为受控兜底（D10）。
- **Phase 4（下载联调）**：下载改用采集器产出的媒体 URL；`httpx` CDN 下载保持。
- **回滚**：因接口未变，回滚 = 把开关翻回 instaloader 主路径；采集器隐藏于开关之后。配置级切换，
  无需改前端。

## Open Questions

- ~~反检测方案最终定 `patchright` 还是 `playwright`+`stealth`？~~ → **已定：`patchright`**（D2）。
- ~~配额/延迟/冷却的具体数值？~~ → **已定保守默认**（D5/D7/D8 + D12 配置表），后续按实际观察调优。
- 是否需要把浏览器拆成独立 worker 进程？（MVP 同进程，后续再议。）
- 阶梯退避是否需按"信号类型"分别取不同档位？（当前统一阶梯，待运行期信号样本积累后再细化。）

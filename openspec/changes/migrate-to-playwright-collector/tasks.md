## 1. 基建与依赖（Phase 0）

- [x] 1.1 锁定反检测方案为 `patchright`（API 兼容 Playwright 的打补丁 fork），记录版本于依赖文件
- [x] 1.2 新增依赖 `patchright`，执行 `patchright install chromium` 安装浏览器内核
- [x] 1.3 新增 `collector/` 包骨架：`__init__.py`、`browser_session.py`、`instagram_collector.py`、`safety.py`、`quota.py`
- [x] 1.4 新增配置项（环境变量，默认值见 design D12 配置表）：浏览器 `userDataDir`/`storage_state` 路径、节奏随机区间（动作 3–7s、滚动 1.5–3.5s、稳定 2–4s）、会话/每日配额（60min/30、120/日）、冷却退避（15,30,60,120 分钟、上限 360）、`PROFILE_PAGE_SIZE=6`、`ENABLE_INSTALOADER_FALLBACK=false`
- [x] 1.5 编写一次性登录脚本 `login.py`：有头模式打开浏览器，人工完成备用账号登录（含 2FA/挑战），退出前落盘 `storage_state` 与 `userDataDir`
- [x] 1.6 用 `login.py` 完成专用备用账号的一次性登录，确认登录态可复用（无头重启后免登录）

## 2. 账号安全层（safety.py / quota.py）

- [x] 2.1 实现 `quota.py`：会话级（60 分钟滑动窗口 ≤30）与每日（本地自然日 ≤120）动作计数器，触及上限返回 429，阈值可配置
- [x] 2.2 实现 `safety.py` 信号检测：识别登录墙、`/challenge`/"/confirm"、限流提示、`Action Blocked`、异常重定向、空/非预期响应
- [x] 2.3 实现冷却状态机：命中信号立即停手不重试，会话进入冷却（阶梯退避 15→30→60→120 分钟，上限 360 分钟，24h 无信号重置），冷却期内拒绝所有采集请求并返回 429/503 + 可读说明
- [x] 2.4 实现只读约束：采集器 API 仅暴露 导航/滚动/读网络响应/下载，代码层面不提供任何写操作（like/follow/comment/save）方法
- [x] 2.5 单元验证：信号命中即停手不重试、冷却期拒绝、配额达上限拒绝（用模拟信号/计数驱动）

## 3. 浏览器会话层（browser_session.py）

- [x] 3.1 实现 `launch_persistent_context` 常驻单浏览器上下文，复用 `userDataDir`/`storage_state`
- [x] 3.2 接入 `lifespan`：FastAPI 启动时拉起、关闭时清理浏览器进程
- [x] 3.3 实现串行化 `asyncio.Lock`：同一时刻至多一次 Instagram 导航/滚动
- [x] 3.4 实现浏览器守护：检测上下文失活/崩溃则自动重启
- [x] 3.5 验证：并发请求被串行化，单浏览器常驻、健康重启有效

## 4. 采集层 - 单帖（instagram_collector.py）

- [x] 4.1 实现 `preview_post(shortcode)`：导航帖子页 + `page.on("response")` 捕获网络响应
- [x] 4.2 实现解析器：从捕获 JSON 提取 shortcode/owner/caption/媒体节点（单图/单视频/sidecar），映射到 `PreviewResponse`/`MediaItem`
- [x] 4.3 接入人类化节奏（随机抖动延迟）与只读滚动
- [x] 4.4 字段一致性校验：与 instaloader 旧路径对同一帖子比对 `PreviewResponse` 字段，确认完全对应
- [x] 4.5 容错：链接非法→400 不导航；缺字段/解析失败→受控错误（按是否限流信号分流）
- [x] 4.6 视频帖(reel, media_type=2)实测：补一个公开 reel 样本验证 video_versions 解析与 type=video 下载链路（图/相册已在 DY6NvmAk5J5 验证通过）✅ DZlsREAz0OE 视频帖通过

## 5. 采集层 - Profile 分页

- [x] 5.1 实现 Profile 导航 + 首屏数据捕获（用户信息 + 首批帖子 + end_cursor）
- [x] 5.2 实现滚动驱动翻页：逐步滚动触发 Web 自身下一页请求并捕获（不自行重放请求）
- [x] 5.3 实现整型-offset 缓存（按 `username`，有序列表 + end_cursor + TTL + LRU 容量上限）：请求 `[cursor,cursor+limit)` 切片，`next_cursor`/`has_more` 计算正确
- [x] 5.4 实现解析器：映射到 `ProfilePostItem`（shortcode/url/caption/date_utc/type/media_count/thumbnail_url/resources[]）
- [x] 5.5 字段一致性校验：与 instaloader 旧 `/profile/preview` 比对字段，确认前端可零改动解析
- [x] 5.6 边界：触及末尾（has_more=false/next_cursor=null）、私密账号（is_private=true、不触发写操作）、缓存 TTL 失效重建（末尾/缓存逻辑已实现并验证；私密账号实测待样本）

## 6. FastAPI 集成与开关

- [x] 6.1 重构 `/preview`：数据源由 instaloader 切为采集器（保留 Pydantic 模型与校验）
- [x] 6.2 重构 `/profile/preview`、`/profile/download`：数据源切为采集器；下载改用采集器产出的 CDN 直链 + 现有 `httpx` 下载
- [x] 6.3 重构 `/download`：数据源切为采集器，`selected_indices` 语义（空=全部）不变
- [x] 6.4 实现采集器/instaloader 主路径开关：默认采集器主路径，instaloader 默认关闭
- [x] 6.5 端到端联调前端：单帖预览/下载、Profile 首屏/加载更多/勾选下载，确认契约零回归

## 7. instaloader 兜底纪律

- [x] 7.1 将 instaloader 调用收拢为受控兜底入口，受 `ENABLE_INSTALOADER_FALLBACK`（默认 false）控制
- [x] 7.2 兜底仅限单帖 `/preview`、`/download`；Profile 接口不提供 instaloader 路径
- [x] 7.3 禁止自动回退：单次请求内采集失败不自动转 instaloader；限流/挑战信号下即使开关开启也不兜底（走冷却）
- [x] 7.4 验证：默认不回退、限流时禁止兜底、Profile 无兜底（Phase 6 已实现：`use_collector` 主路径 + 单帖 `elif enable_instaloader_fallback` 兜底 + Profile 返 503 + `CollectorError` 直接映射不转兜底；对应 `account-safety` 三条兜底场景）

## 8. 验证与切换

- [x] 8.1 验证账号安全层：节奏随机抖动、串行化、信号停手+冷却、配额上限、只读无写副作用
- [x] 8.2 验证采集正确性：覆盖 spec 的单帖/Profile/下载全部 scenario
- [x] 8.3 验证接口契约逐字段不回归（前端零改动）
- [x] 8.4 翻转开关完成主路径切换（采集器为主、instaloader 兜底关闭）；记录回滚方式（开关翻回）
- [x] 8.5 运行 `openspec validate migrate-to-playwright-collector --strict` 通过

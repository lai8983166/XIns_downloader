## Context

自动任务链路（`collector/auto_job.py`）已实现平台无关的状态机/调度/熔断/清单，但把 Instagram 采集函数与 `profile_{username}` 前缀硬编码在 `AutoJob` 里。Threads 采集器（`collector/threads_collector.py`）已有 `preview_threads_profile`（GraphQL 翻页重放，浏览器滚动不触发翻页系实测结论）与手动路由，缺 auto 挂钩。安全层（`scheduler` / `cooldown` / 日信号预算）为全局单例，天然平台共享。见 proposal.md - Why。

前置事实（决定本设计的三个坑）：

1. Threads 原始节点顶层无 `code`（在 `thread_items[0].post.code`），`_Manifest` 边车按 `n["code"]` 去重会全部判无效。
2. Threads 版 `do_navigate_and_fetch` 无条件 `captured.clear()`；IG 版有 `rehydrated` 标志保护回灌数据，Threads 回灌后降级重导航会把数据清掉。
3. Threads 翻页依赖首屏 GraphQL 请求模板（`first_req` 的 post_data/headers）重放；回灌的缓存没有 `first_req`，首次续传 `_fetch_more` 会空转 → 必须走降级重导航重建。

## Goals / Non-Goals

**Goals:**

- `POST /threads/profile/auto` 提供与 IG 完全一致的自动任务能力（调度、熔断、断点续传、增量重跑）。
- `AutoJob` 单份状态机，平台差异全部收敛到一个适配对象。
- 旧 IG 清单（无 `platform` 字段）行为零变化。

**Non-Goals:**

- 不做 Threads 单帖采集兜底（沿用 threads_collector 现状：资源只从缓存取）。
- 不做多任务并发（保持全局单活跃互斥，跨平台共享）。
- 不改手动路由、审查（media-review）、调度与安全规格。
- 不解析 Threads 帖子总数（`mediacount` 保持 0，进度展示退化处理）。

## Decisions

### D1: 平台适配对象，而非继承/复制

在 `auto_job.py` 定义轻量适配 dataclass，`AutoJob` / `AutoJobManager` / `_Manifest` 只面向它编程：

```python
@dataclass(frozen=True)
class PlatformAdapter:
    name: str                                   # "instagram" | "threads"
    folder_prefix: str                          # "profile_" | "threads_"
    preview: Callable                           # preview_profile | preview_threads_profile
    rehydrate: Callable                         # 回灌缓存
    touch: Callable                             # 保温
    reset: Callable                             # 增量刷新时丢弃缓存
    cache_nodes: Callable                       # 读缓存原始 nodes
    node_code: Callable[[dict], Optional[str]]  # IG: n["code"]；Threads: thread_items[0].post.code
```

IG 与 Threads 各一个模块级实例。替代方案：复制 `ThreadsAutoJob`（两份状态机，熔断/续传逻辑修一处漏一处，否）；`AutoJob.__init__` 直接收 6 个函数参数（调用方易错、签名膨胀，否）。`node_code` 收进适配器是因为 `_Manifest` 边车三处（load/append/rewrite）都按它去重。

### D2: `_Manifest` 平台感知与旧清单兼容

- `folder = download_root / f"{adapter.folder_prefix}{username}"`（Threads 落 `threads_{username}`，与手动下载同目录，审查（review）自然兼容）。
- 构造时传入 `node_code`，边车三处去重统一走它。
- `data` 增加 `"platform"` 字段并在 `load` 后回填 `adapter.name`；读取时旧清单缺该字段 → 按 `instagram` 处理（现状唯一平台，无歧义）。
- `load(username)` 签名改为 `load(username, adapter)`；仅 `AutoJobManager.start` 调用。

### D3: threads_collector 补挂钩 + 回灌保护

对齐 `instagram_collector` 的四个 auto 挂钩（加入 `__all__`）：

```python
rehydrate_threads_cache(username, nodes, exhausted)  # navigated=False, rehydrated=True, first_req 置空
touch_threads_cache(username)                        # 保温
reset_threads_cache(username)                        # 增量刷新时丢弃
threads_cache_nodes(username)                        # 浅拷贝读
```

`preview_threads_profile` 的 `do_navigate_and_fetch` 增加 `keep_on_navigate`（entry 带 `rehydrated` 时不清 `captured` / `have_codes`），对齐 IG 版语义；`first_req` 重导航时照常重置重捕获（request listener 会从新页面拿到新模板，顺带解决 headers 过期）。同时导出 `extract_threads_username`（现私有 `_extract_threads_username`，路由层需要）。

回灌链路推演（兼答"没有 first_req 怎么续传"）：`rehydrate` 后 `navigated=False` → `can_resume=False` → 直接 `do_navigate_and_fetch`（不走续传）；因 `keep_on_navigate=True` 回灌 nodes 保留，重导航滚到的旧帖由 `have_codes` 去重、新帖追加。页池键已是 `threads:{username}`，与 IG 同名用户不冲突。

### D4: `AutoJob` 改动面

- `AutoJob.__init__(username, max_posts, download_root, adapter)`；`_run_inner` / `_snapshot_nodes` 内五个 IG 挂钩调用全部换 `adapter.*`。
- `status_dict()` 增加 `platform` 字段（前端与调试可见）。
- asyncio task 名 `auto-{platform}-{username}-{job_id}`。
- `AutoJobManager.start(username, max_posts=None, platform="instagram")`：按 name 取适配器；互斥判断不变（注册表本就跨平台单活跃）。
- 其余状态机逻辑（熔断、增量刷新停判"整页已知帖即停"、max_posts 上限）不动——Threads 新帖同样只出现在时间线头部，停判语义成立。

### D5: 路由

`POST /threads/profile/auto`：`extract_threads_username(req.profile)` 校验（支持 `@user` / `threads.net|com` URL）→ `auto_manager.start(username, req.max_posts, platform="threads")`；`AutoJobError` 映射与 IG 路由一致（`active_job` → 409 附 job_id）。请求/响应模型复用 `AutoStartRequest` / `AutoStartResponse`。状态与取消复用 `/profile/auto/jobs/{job_id}`（job_id 注册表平台无关）。

### D6: 前端

- 模式按钮：Threads 平台下显示 Profile / 自动 / 审查（无「单帖」）；`handlePlatformChange("threads")` 只在当前 mode 为 `post` 时回落 `profile`，否则保留（切到 Threads 后可直接点「自动」）。
- `startAutoJob` 路径按平台：`isThreads ? "/threads/profile/auto" : "/profile/auto"`；轮询/取消路径不变。
- `AutoJobPanel`：`status.mediacount` 为 null（Threads 拿不到总数）时进度条退化为计数展示，不显示百分比；状态里加平台标识。
- 自动模式输入框提示与启动文案按平台区分。

### D7: 下载与 CDN 不动

Threads 媒体域名 `cdninstagram.com`（已在 `ALLOWED_MEDIA_HOST_SUFFIXES` 白名单）；手动 Threads 下载已复用 `Referer: instagram.com` 实测可用，`_download_file` 保持原样。下载仍不占动作名额。

## Risks / Trade-offs

- [Threads GraphQL 重放比 IG 滚动更"程序化"，被风控概率可能更高] → 每页翻页仍受 `wait_for_slot` 日程节流（小时窗预算 + 最小间隔），页间保留 `scroll_delay` 人类化抖动；命中信号走既有冷却/日预算/熔断路径，两平台共享日信号预算，不会叠加放大风险。
- [回灌缓存无 `first_req`，且长冷却后 headers 可能过期] → D3 推演：回灌后直接走重导航重建模板，属预期路径而非故障；代价是多一次导航动作名额。
- [Threads 无帖子总数，进度百分比无法计算] → 前端退化展示已处理帖数与状态计数（D6）；不引入额外请求探测总数。
- [旧 IG 清单与新代码混跑] → D2 缺省 `instagram`，旧行为零变化；新 Threads 清单独立目录，互不干扰。
- [持久 page 可能卡在坏 SPA 态：re-goto 不再触发首屏 graphql，零捕获被误判 `not_found`（e2e 实测）] → 零捕获抛 `not_found` 前先废弃该持久 page（`close_profile_page`），下次重试用全新页面；自动任务的熔断/取消/清单保留在该场景下全部按设计工作（进程内回灌复现采集路径本身正常）。IG 侧同型弱点已知，本次不动（IG 行为零变化约束）。

## Migration Plan

无数据迁移：旧 `profile_*/.auto_state.json` 按缺省平台继续可用；新任务落 `threads_*/`。回滚即 revert 本变更，Threads 清单文件夹可留存不影响 IG 路径。

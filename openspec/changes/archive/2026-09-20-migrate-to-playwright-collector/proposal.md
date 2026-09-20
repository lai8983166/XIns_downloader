## Why

当前所有 Instagram 数据采集（单帖预览、Profile 分页预览、下载）都走 `instaloader`，
它直接向 Instagram 的私有 GraphQL 接口发请求。实测中，从 Profile 根页面读取多个帖子时
（`profile.get_posts()` 连续发起多次 GraphQL 查询）会很快触发 Instagram 限流（HTTP 429 /
"please wait a few minutes"），而为了绕过限流去重试/加快请求，恰恰是导致**账号被封禁**的
头号原因。我们已确认前端（`frontend/src/App.jsx`）所依赖的接口契约完全由 instaloader 支撑，
这套方案的可持续性和账号安全性都不可接受。

本变更把数据采集层从"伪造请求的 API 客户端（instaloader）"切换为"驱动真实浏览器的
自动化（Playwright）"——由浏览器以合法 Web 客户端的身份加载页面、捕获其自身的网络响应，
从而把请求指纹恢复成"真人浏览"，从根源上降低被风控判定为机器人的概率。**账号不被封禁是
硬约束，所有设计围绕它展开。**

## What Changes

- **新增浏览器采集层**：用 Playwright 驱动持久化的真实 Chromium，加载 Instagram 页面，
  通过拦截浏览器自身的网络响应提取数据（媒体 URL、sidecar 节点、caption、时间等），
  作为 `/preview`、`/profile/preview`、`/profile/download` 的**主采集路径**。
- **新增账号安全层（防封禁）**：专用备用账号隔离、登录态持久化与一次性手动登录、
  人类化节奏（随机延迟、逐步滚动、小批量）、风控/挑战信号检测与冷却、会话级与每日配额、
  严格只读。详见 `account-safety` 规格与 `design.md`。
- **保持接口契约不变**：`POST /preview`、`POST /profile/preview`、`POST /profile/download`、
  `POST /download`、`GET /media-proxy` 的请求/响应结构逐字段保持现状，**前端零改动**。
  迁移只发生在数据来源层。
- **保留 instaloader 作为应急兜底（默认关闭）**：instaloader 代码与依赖保留，但默认禁用，
  仅在 Playwright 失败且经人工显式开启时，用于单帖场景的临时应急。**绝不自动回退**，
  以免把限流/封号风险悄悄重新引入。
- 下载路径仍走现有 `httpx` 直拉 CDN 媒体（`cdninstagram.com`/`fbcdn.net`，公开可取、低风险），
  仅把"数据从哪来"换成浏览器采集层。

## Capabilities

### New Capabilities
- `instagram-collection`: 通过 Playwright 浏览器自动化采集 Instagram 单帖与 Profile（分页）
  内容，并解析为现有响应模型。取代 instaloader 成为主采集路径。
- `account-safety`: 账号不被封禁的硬约束规格——专用账号隔离、登录态持久化、人类化节奏、
  风控信号检测与冷却、配额上限、严格只读、以及 instaloader 兜底的"默认关闭/不自动回退"纪律。

### Modified Capabilities
<!-- 无既有规格（本仓库首批规格）。instaloader 原行为不作为正式 spec 保留，仅作为 account-safety
     下的受控兜底，故无需 Modified 条目。 -->

## Impact

- **代码**：新增浏览器采集模块（如 `collector/`，封装 Playwright 会话、网络拦截、解析、配额、
  信号检测）；重构 `main.py` 中 `/preview`、`/profile/preview`、`/profile/download`、
  `/download`，把 instaloader 调用替换为采集层调用；instaloader 路径收拢到受控兜底入口。
- **依赖**：新增 `patchright`（打了反检测补丁、API 兼容 Playwright 的 fork）作为反检测方案，
  需 `patchright install chromium`；`instaloader` 保留。
- **配置 / 环境变量**：新增浏览器配置目录 / `storage_state` 路径、节奏与配额阈值、兜底开关；
  原 Cookie 系列环境变量保留用于 instaloader 兜底。
- **运行时**：后端进程常驻一个持久化（默认无头）Chromium；首次使用需在**有头模式**下
  一次性手动登录（处理 2FA / 挑战页），之后复用登录态；内存与启动开销上升。
- **前端**：无改动（接口契约保持不变）。
- **风险**：账号封禁——通过专用账号 + `account-safety` 规格系统性缓解；Playwright 本身可被
  检测，故须配合反检测方案与真实持久化配置目录。

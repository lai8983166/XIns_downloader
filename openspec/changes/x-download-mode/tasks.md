## 1. x_collector 采集核心

- [x] 1.1 解析纯函数 + 用户名校验：`collector/x_collector.py` 实现 `extract_x_username`（`@user` / `x.com|twitter.com/{user}` / 保留字校验）、`x_node_code`（推文 id）、`x_tweet_resources`（图片 `name=orig` 直链；视频 `video/mp4` 最大 bitrate，排除 m3u8；多媒体 index 递增；`{tweet_id}_{index}.{ext}`）、推文 → `ProfilePost`（`shortcode`=推文 id、`url`=status 链接、`date_utc` 由 RFC2822 `created_at` 解析）。验证：新增 `test_x_collector.py` 纯函数单测全绿——fixture 用**手工脱敏构造**的推文节点（字段名对齐探查 summary；summary 含凭证不入库），覆盖单图/视频多码率含 m3u8/多图/纯文字跳过/日期解析。
- [x] 1.2 `preview_x_profile`：持久 page 池键 `x:{username}` + TTL 缓存 + 整型 cursor 分页 + 逐步滚动捕获（按**结构特征**识别时间线响应，不绑 operationName；遍历 entry 取推文与 bottom cursor）+ `detect_signal` 信号链（auth_token 预检给明确注入提示；suspended/不存在 → 404）+ `rehydrated` 回灌保护 + 零捕获废弃持久 page。验证：fake page 单测（复用 `test_threads_hooks.py` 模式）覆盖首屏/续传滚动/回灌重导航去重追加/零捕获 404+废弃/登录墙信号。
- [x] 1.3 auto 四挂钩：`rehydrate_x_cache` / `touch_x_cache` / `reset_x_cache` / `x_cache_nodes`（对齐 threads 版语义）加入 `__all__`。验证：import 冒烟 + 挂钩语义单测（rehydrated 标志 / touch 刷新 / reset 清空 / 空 nodes no-op）。

## 2. 下载与安全适配

- [x] 2.1 `main.py`：`ALLOWED_MEDIA_HOST_SUFFIXES` 增加 `"twimg.com"`；`download_file` 增加 `referer` 参数（X 用 `https://x.com/`，IG/Threads 调用点不变）。验证：单测/冒烟——`pbs.twimg.com`、`video.twimg.com` 通过校验，非白名单域仍 400。
- [x] 2.2 `auto_job.py`：`PlatformAdapter` 增加 `download_referer` 字段（IG/Threads 填 instagram、X 填 x.com），`_download_file` 按 adapter 取 Referer；`_ADAPTERS` 注册 `x_adapter`（`x_` 前缀、`x_node_code`、四挂钩）。验证：`python -c` 断言 `_ADAPTERS` 三平台齐 + `test_auto_job.py` 既有用例全绿（fakes 补默认 referer 字段）。
- [x] 2.3 `safety` 检测固化：新增 X URL 单测（`x.com/login`、`x.com/i/flow/login` 命中 login_wall；逻辑不改）。验证：`python test_safety_quota.py` 全绿 + 新用例通过。

## 3. 路由

- [x] 3.1 `main.py` 新增 `POST /x/profile/preview`、`POST /x/profile/download`（复用 `ProfilePreviewRequest`/`ProfileDownloadRequest` 与既有错误映射；下载落 `downloads/x_{username}/`）与 `POST /x/profile/auto`（`extract_x_username` 校验 → `auto_manager.start(..., platform="x")`，409 映射同既有）。验证：TestClient + fake manager/adapter——非法输入 400、合法 202 + job_id、状态含 `platform: "x"`、互斥 409。

## 4. 前端

- [x] 4.1 `App.jsx`：平台组加 X（platform `"x"`，无单帖：Profile/自动/审查）；`profilePath`/`profileDownloadPath`/`autoStartPath` 按 `x` 切 `/x/profile/*`；输入提示/启动文案/`AutoJobPanel` 平台标识（"X"）。验证：`npm run build` 通过，切 X 后「自动」可选。

## 5. 集成验证

- [x] 5.1 全量回归：`python test_x_collector.py && python test_auto_job.py && python test_threads_hooks.py && python test_scheduler.py && python test_safety_quota.py && python test_review.py` 全绿；`test_auto_job.py` 增加一个 x adapter 全流程用例（翻页→下载→清单落 `x_{username}`、platform=x）。
- [x] 5.2 真实端到端（需本机已 `login_x_cookies.py` 注入登录态）：先 `POST /x/profile/preview` 手动预览一个小体量用户确认采集稳定，再 `POST /x/profile/auto`（`max_posts=2`）观察推进至 `done`、文件落 `downloads/x_{username}/`、清单 `platform: "x"`、边车按推文 id 非空；再次启动验证增量；中途取消验证优雅退出。若命中登录墙/冻结页，按 401/404 语义核验提示文案。（实测 @nasa：预览 4 帖含 1920x1080 mp4/orig 图、自动任务 done（3 帖 42MB）、增量续跑 cursor 3→6 无重下、无上限任务取消优雅退出清单保留；次要观察项：预览响应 full_name 为 None——时间线响应内用户信息可能由 UserByScreenName 单独返回，后续可从该响应补齐，不影响功能）：先 `POST /x/profile/preview` 手动预览一个小体量用户确认采集稳定，再 `POST /x/profile/auto`（`max_posts=2`）观察推进至 `done`、文件落 `downloads/x_{username}/`、清单 `platform: "x"`、边车按推文 id 非空；再次启动验证增量；中途取消验证优雅退出。若命中登录墙/冻结页，按 401/404 语义核验提示文案。

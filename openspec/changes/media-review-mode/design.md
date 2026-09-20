## Context

自动模式把媒体批量落到 `downloads/profile_{username}/`（顶层为 `{shortcode}_{index}.{ext}`，
另有隐藏的 `.auto_state.json` 清单与 `.auto_nodes.jsonl` 边车）。筛选需求：逐文件三档判定
（1 保留 / 2 候选 / 3 硬删除——操作者明确选定硬删除与纯文件级粒度），候选移入同文件夹
`_candidate/` 子目录。现有前端已有模式切换（单帖/Profile/自动/Threads 平台）与灯箱交互；
后端 `/media-proxy` 只放行 CDN 域名，不能服务本地文件。

约束：单人本地自用工具；审查为纯后处理——不联网、不占动作名额、不触碰采集与风控状态。

## Goals / Non-Goals

**Goals:**
- 4 条 `/review/*` 路由：文件夹枚举、文件枚举、本地文件服务、三档动作。
- 路径安全：folder/filename 一律约束在 `DOWNLOAD_ROOT` 内，拒绝穿越。
- 审查 UI：大图单张流 + 键盘 1/2/3 + ←/→ + 网格总览跳转 + 进度实时。
- 动作即时生效（无批量缓冲、无撤销——硬删除语义由操作者拍板）。

**Non-Goals:**
- 不做回收站/撤销（用户明确选择硬删除；如将来反悔，加 `_trash` 档即可扩展）。
- 不做帖级分组判定（纯文件级；多图帖可能被拆开）。
- 不做清单联动/反向同步（审查不动 `.auto_state.json`）。
- 不做评分/标签/多档候选等更复杂的 curation。

## Decisions

### D1：实现位置——main.py 直接加路由，不建新模块

- **选择**：4 条路由 + 2 个辅助函数（`_review_folder_path` / `_review_file_path`）全部进
  `main.py`。逻辑无状态、约百余行，与现有 download 路由同层。
- **备选与否决**：独立 `review.py` 模块（多一层间接，无复用场景）；放 collector/（审查不是采集）。

### D2：路径安全——白名单字符 + resolve 归位校验

- **选择**：folder 与 filename 先过正则 `^[A-Za-z0-9._-]+$`（天然排除分隔符与 `..`——
  注意 `..` 全串匹配被 `^…$` 拒绝：`..` 只含点不匹配该正则？否——`[A-Za-z0-9._-]+` 允许
  纯点串，故再显式拒绝 `.` 开头与全点名），再 `Path.resolve()` 后校验
  `is_relative_to(DOWNLOAD_ROOT.resolve())`，双保险。
- **理由**：枚举与服务两个入口共用同一校验；正则保证无分隔符，resolve 归位兜底符号链接
  类边界（本地工具，主要防手滑与坏输入，非对抗性威胁模型）。

### D3：本地文件服务——StaticFiles 挂载，不自写流式路由

- **选择**：`app.mount("/review/file", StaticFiles(directory=DOWNLOAD_ROOT))`，即
  `GET /review/file/{folder}/{filename}`。
- **理由**：白送 Content-Type 推断、ETag/Last-Modified、**Range 请求**（视频拖动进度条依赖）；
  StaticFiles 自身拒绝 `..` 穿越。枚举路由（`/review/folders`、`/review/files`）前缀不与
  mount 冲突且先注册。
- **接受的暴露**：downloads 下隐藏文件（清单/边车）也可被该 URL 读到——本地单用户工具，
  无敏感内容，接受；不做扩展名白名单子类化。

### D4：动作语义——单文件单请求、即时执行、明确的状态码

- **选择**：`POST /review/action {folder, filename, action}`：
  - `keep` → 直接 200（无 FS 操作）；
  - `candidate` → `mkdir(_candidate, exist_ok=True)` + `os.replace(src, dst)`；目标已存在
    （同名文件先前移入）→ 409；源不存在 → 404；
  - `delete` → `os.remove`，源不存在 → 404。
  - 返回 `{status, folder, filename, action, remaining}`（remaining 为执行后顶层待审数，
    供前端进度；前端以本地列表为准亦可）。
- **理由**：单文件粒度让"按键即生效"的实现最简单（无批量事务、无回滚路径）；
  `os.replace` 同盘原子，无跨设备语义问题（同文件夹内移动必然同盘）。

### D5：前端——第四模式「审查」，大图流为主、网格为辅

- **选择**：IG 平台 modeSwitch 增加「审查」；流程：文件夹下拉（`GET /review/folders`）
  → 大图单张流：当前文件大图/内嵌视频 + 三个大按钮（保留/候选/删除）+ 键位提示；
  快捷键 `1/2/3` 判定并自动前进，`←/→` 无判定换页；`G` 或按钮切换网格总览（复用
  `MediaGrid` 风格）点击跳转。本地维护文件列表与索引：动作成功后从列表移除
  （keep 也移出"待审"队列——保留即已审）并推进索引；进度 = 总数 − 剩余。
- **样式**：复用现有暗色灯箱与按钮体系，新增 review 专属类（大图舞台、动作条、进度条）。
- **URL 构造**：`/review/file/{folder}/{filename}` 直连（开发环境经 vite 代理）。

### D6：vite 代理——`/review` 前缀加入 proxy 表

- **选择**：`vite.config.js` 的 proxy 增加 `"/review": apiTarget`。
- **理由**：与既有 5 个前缀一致；漏配则开发环境审查模式全 404。

## Risks / Trade-offs

- [硬删除不可撤销] → 操作者明确选定；UI 在删除按钮/键位提示上标注"不可恢复"，删除后
  条目即时消失给出明确反馈。
- [审查中途误关页面] → 动作已即时落盘，无丢失；重进时枚举接口天然反映剩余文件，
  "进度"概念仅存在于当前会话（已保留文件仍留在顶层，会被再次列出）——这是纯文件级+
  无状态设计的直接后果，接受（如需记忆已审需引入状态文件，Non-Goal）。
- [StaticFiles 暴露 downloads 全部内容含清单] → 本地单用户，接受（见 D3）。
- [`_candidate/` 内文件未来可能被自动任务在顶层重下] → 仅影响未进清单的手动下载遗产，
  已在 proposal/spec 记录为已知边界。

## Migration Plan

纯新增（4 路由 + 1 mount + 前端新模式 + vite proxy 一行），无存量改动；
回滚 = revert 单个提交。

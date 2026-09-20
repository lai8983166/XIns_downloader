## 1. 后端路由（main.py）

- [x] 1.1 路径安全辅助：`_review_folder_path(folder)` / `_review_file_path(folder, filename)`——正则 `^[A-Za-z0-9._-]+$` + 拒绝 `.` 开头 + `resolve().is_relative_to(DOWNLOAD_ROOT)`，非法/不存在抛对应错误。验证：`test_review.py` 中构造 `..`、`a/b`、`a\\b`、绝对路径、`_candidate` 外逃等用例全部被拒。
- [x] 1.2 `GET /review/folders` + `GET /review/files`：枚举 `profile_*`（或含媒体文件的目录）与顶层媒体文件（排序、类型/大小，排除 `.` 开头、子目录、非媒体扩展名）。验证：`test_review.py` 用临时 downloads 目录断言列表内容与排除规则、非法 folder → 400。
- [x] 1.3 `app.mount("/review/file", StaticFiles(directory=DOWNLOAD_ROOT))`（置于既有路由注册之后）。验证：TestClient 请求 jpg → 200 + `image/jpeg`，mp4 → `video/mp4`，Range 请求 → 206，`..` 穿越 → 404/400。
- [x] 1.4 `POST /review/action`：keep（空操作）/ candidate（mkdir `_candidate` + `os.replace`；目标存在 409；源缺失 404）/ delete（`os.remove`，缺失 404）；返回执行结果。验证：`test_review.py` 覆盖四类动作 + 404/409/400 + 清单文件不受影响（逐字节比对）。

## 2. 测试与集成

- [x] 2.1 `test_review.py`（脚本风格，`python test_review.py`）：TestClient + 临时目录覆盖上述全部场景；main.py 可导入无回归（`python -c "import main"`）。
- [x] 2.2 `frontend/vite.config.js`：proxy 增加 `"/review": apiTarget`。验证：dev 起服务后 `/review/folders` 可达。

## 3. 前端审查模式

- [x] 3.1 模式入口与文件夹选择：modeSwitch 增「审查」→ 拉取 `/review/folders` 下拉选择 → 加载 `/review/files`。验证：浏览器可见文件夹列表与文件数。
- [x] 3.2 大图审查流：当前文件大图/`<video controls>` 播放、三个动作大按钮（含"删除不可恢复"提示）、快捷键 `1/2/3` 判定即动作并自动前进、`←/→` 换页、网格总览（复用 MediaGrid 风格）跳转；动作成功后本地列表移除、进度（总数−剩余）实时更新。验证：浏览器实操图片与视频各判定一次，键盘与按钮两条路径均生效。
- [x] 3.3 样式（App.css）：review 舞台/动作条/进度条，沿用现有设计语言；`npm run build` 通过。

## 4. 验证

- [x] 4.1 端到端：对一个小 profile 文件夹完整过一遍（1/2/3 各用若干次）→ 核对源文件夹只剩保留文件、`_candidate/` 内为候选、已删文件不存在、`.auto_state.json` 未变。验证：资源管理器比对 + 清单哈希一致。

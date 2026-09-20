"""自动模式：给定 username 后台全量翻页采集 + 下载（design D5-D9 + specs/auto-download）。

链路：
    AutoJobManager（进程级注册表；全局至多一个活跃任务）
      → AutoJob（asyncio Task 状态机）
          scheduler.wait_for_slot → preview_profile(翻页) → 页内并发下载
          → manifest 每帖原子落盘 → touch 缓存保温 + nodes 边车增量 → report_finish
    风控信号 → 采集层已 cooldown.report → 任务 paused_cooldown / paused_signal_budget
    连续非信号失败 ≥ auto_fail_limit → paused_circuit + scheduler.invalidate（人工确认）

状态机：starting → running ⇄ paused_{cooldown,signal_budget,circuit} → done/failed/cancelled

持久化（{download_root}/profile_{username}/，design D5）：
    .auto_state.json   小清单：posts 状态映射 / cursor / exhausted / failures / 计数
    .auto_nodes.jsonl  边车：原始采集 nodes（每行一个，增量 append），恢复时回灌采集层缓存
损坏（截断/非 JSON）→ 启动报 AutoJobError 并指明文件路径，不静默重采。

下载（design D6）：CDN 直链不占动作名额；页内 Semaphore 并发；.part + os.replace 原子落盘；
单资源退避重试 2 次后记入失败清单，不阻断任务。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from collector.config import settings
from collector.instagram_collector import (
    CollectorError,
    PostResource,
    ProfilePost,
    preview_profile,
    profile_cache_nodes,
    rehydrate_profile_cache,
    reset_profile_cache,
    touch_profile_cache,
)
from collector.quota import scheduler
from collector.safety import cooldown, daily_signal_status

logger = logging.getLogger(__name__)

__all__ = ["AutoJobManager", "AutoJob", "AutoJobError"]

# CollectorError.kind 中属于风控信号的（采集层已 cooldown.report）
_SIGNAL_KINDS = {"rate_limited", "login_required"}
# 暂停态（可等待恢复或需人工介入）
_PAUSED_STATES = {"paused_cooldown", "paused_signal_budget", "paused_circuit"}
_TERMINAL_STATES = {"done", "failed", "cancelled"}
_MAX_STATE_FAILURES = 50   # 清单 failures 封顶
_MAX_RECENT_FAILURES = 20  # 状态接口 recent_failures 封顶
_DOWNLOAD_RETRY_BACKOFF = (2.0, 8.0)


class AutoJobError(Exception):
    """启动/管理自动任务失败。kind: active_job | manifest_corrupt。"""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def _next_midnight() -> float:
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).date()
    return datetime.combine(tomorrow, datetime.min.time()).timestamp()


async def _download_file(url: str, output: Path) -> None:
    """CDN 媒体下载（同 main.py download_file 的 UA/Referer 语义；不占动作名额）。"""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.instagram.com/",
    }
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=headers) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(output, "wb") as f:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        f.write(chunk)


class _Manifest:
    """任务持久化清单 + nodes 边车（design D5）。损坏即拒载并指明路径。"""

    def __init__(self, folder: Path):
        self.folder = folder
        self.state_path = folder / ".auto_state.json"
        self.nodes_path = folder / ".auto_nodes.jsonl"
        self.data: Dict[str, Any] = {
            "version": 1,
            "username": "",
            "cursor": 0,
            "exhausted": False,
            "mediacount": None,
            "posts": {},  # shortcode -> {"status": done|partial|failed, "files": [], "date_utc": str}
            "failures": [],
            "counts": {"downloaded": 0, "skipped": 0, "failed": 0},
        }
        self.nodes: List[dict] = []
        self._saved_codes: set = set()

    def load(self, username: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise AutoJobError(
                    "manifest_corrupt",
                    f"任务清单损坏，请修复或删除后重试：{self.state_path}（{exc}）",
                ) from exc
            if not isinstance(raw, dict) or "posts" not in raw or "cursor" not in raw:
                raise AutoJobError("manifest_corrupt", f"任务清单结构异常：{self.state_path}")
            self.data.update(raw)
        self.data["username"] = username
        if self.nodes_path.exists():
            try:
                raw = self.nodes_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise AutoJobError(
                    "manifest_corrupt", f"nodes 边车不可读：{self.nodes_path}（{exc}）"
                ) from exc
            # 只按真实 \n 切行：str.splitlines 会额外在 U+2028/U+0085 等 Unicode
            # 行分隔符上切分，而 json.dumps(ensure_ascii=False) 不转义它们——
            # caption 含这类字符时 splitlines 会把合法 JSONL 误判为损坏行
            lines = raw.split("\n")
            needs_normalize = bool(raw) and not raw.endswith("\n")
            for i, line in enumerate(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    node = json.loads(line)
                except ValueError:
                    if i == len(lines) - 1:
                        continue  # 末段无换行且解析失败 → 半写残留，丢弃
                    raise AutoJobError(
                        "manifest_corrupt",
                        f"nodes 边车第 {i + 1} 行损坏：{self.nodes_path}",
                    )
                code = node.get("code") if isinstance(node, dict) else None
                if code and code not in self._saved_codes:
                    self._saved_codes.add(code)
                    self.nodes.append(node)
            if needs_normalize:
                # 末尾无换行：无论末段是否半写，重写归一化，防止后续 append 粘连成损坏行
                self.rewrite_nodes(self.nodes)

    def save(self) -> None:
        """原子落盘（tmp + os.replace，防半写损坏）。"""
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def note_failure(self, detail: str, shortcode: Optional[str] = None) -> None:
        record = {"at": _iso(time.time()), "shortcode": shortcode, "detail": detail[:300]}
        self.data["failures"].append(record)
        del self.data["failures"][:-_MAX_STATE_FAILURES]

    def append_nodes(self, nodes: List[dict]) -> None:
        """把新捕获的原始 nodes 增量写入边车（按 code 去重）。"""
        fresh = [
            n for n in nodes
            if isinstance(n, dict) and n.get("code") and n["code"] not in self._saved_codes
        ]
        if not fresh:
            return
        with open(self.nodes_path, "a", encoding="utf-8") as f:
            for n in fresh:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
                self._saved_codes.add(n["code"])
                self.nodes.append(n)

    def rewrite_nodes(self, nodes: List[dict]) -> None:
        """用完整快照整写边车（任务收尾；保持时间线顺序，增量刷新后必须）。"""
        valid = [n for n in nodes if isinstance(n, dict) and n.get("code")]
        with open(self.nodes_path, "w", encoding="utf-8") as f:
            for n in valid:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
        self.nodes = valid
        self._saved_codes = {n["code"] for n in valid}


class AutoJob:
    """单用户自动任务（状态机 + 翻页-下载-落盘循环）。"""

    def __init__(self, username: str, max_posts: Optional[int], download_root: Path):
        self.job_id = uuid.uuid4().hex[:12]
        self.username = username
        self.max_posts = max_posts
        self.folder = Path(download_root) / f"profile_{username}"
        self.manifest = _Manifest(self.folder)
        self.state = "starting"
        self.paused_reason: Optional[str] = None
        self.resume_at: Optional[float] = None
        self.recent_failures: List[dict] = []
        self.updated_at = time.time()
        self._task: Optional[asyncio.Task] = None
        self._cancel_requested = False
        self._consecutive_failures = 0
        self._processed_this_run = 0
        self._refresh_mode = False  # 增量刷新：清单已采完，从头翻找新帖
        self._dl_sem = asyncio.Semaphore(settings.auto_download_concurrency)

    # ---- 生命周期 ----

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"auto-{self.username}-{self.job_id}")

    def request_cancel(self) -> None:
        self._cancel_requested = True

    async def cancel_task(self) -> None:
        """立即取消底层 Task（应用关闭时用；常规取消走 request_cancel 优雅路径）。"""
        self.request_cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ---- 状态 ----

    def _set_state(self, state: str, reason: Optional[str] = None) -> None:
        self.state = state
        self.paused_reason = reason
        self.updated_at = time.time()
        logger.info("auto job %s (%s) -> %s%s", self.job_id, self.username, state, f" ({reason})" if reason else "")

    def status_dict(self) -> dict:
        m = self.manifest.data
        sched = scheduler.status()
        next_action = sched.get("next_slot_at") if self.state == "running" else None
        return {
            "job_id": self.job_id,
            "username": self.username,
            "state": self.state,
            "paused_reason": self.paused_reason,
            "resume_at": _iso(self.resume_at) if self.state in _PAUSED_STATES else None,
            "counts": dict(m["counts"]),
            "posts_total": len(m["posts"]),
            "mediacount": m.get("mediacount"),
            "cursor": m["cursor"],
            "has_more": not m["exhausted"],
            "max_posts": self.max_posts,
            "next_action_at": _iso(next_action),
            "recent_failures": list(self.recent_failures[-10:]),
            "folder": str(self.folder).replace("\\", "/"),
            "updated_at": _iso(self.updated_at),
        }

    def _note_failure(self, detail: str, shortcode: Optional[str] = None) -> None:
        self.recent_failures.append({"at": _iso(time.time()), "shortcode": shortcode, "detail": detail[:300]})
        del self.recent_failures[:-_MAX_RECENT_FAILURES]
        self.manifest.note_failure(detail, shortcode)

    # ---- 主循环 ----

    async def _run(self) -> None:
        logger.info("auto job %s starting (username=%s)", self.job_id, self.username)
        try:
            await self._run_inner()
        except asyncio.CancelledError:
            self._set_state("cancelled")
        except Exception as exc:  # 兜底：任务崩溃不拖垮进程
            logger.exception("auto job %s crashed", self.job_id)
            self._set_state("failed", f"{type(exc).__name__}: {exc}")
            self.manifest.save()

    async def _run_inner(self) -> None:
        m = self.manifest.data
        if m["exhausted"]:
            # 增量刷新模式（specs/auto-download 增量重跑）：新帖在时间线头部 →
            # 丢弃缓存全新捕获（保证时间线顺序），从头翻，整页已知帖即停
            self._refresh_mode = True
            m["exhausted"] = False
            m["cursor"] = 0
            reset_profile_cache(self.username)
        elif self.manifest.nodes:
            # 恢复路径：缓存丢失时回灌已采集 nodes（design D7）
            rehydrate_profile_cache(self.username, self.manifest.nodes, m["exhausted"])
        self._set_state("running")

        while True:
            if self._cancel_requested:
                self._set_state("cancelled")
                return
            if m["exhausted"]:
                self._snapshot_nodes()
                self._set_state("done")
                self.manifest.save()
                return
            if self.max_posts is not None and self._processed_this_run >= self.max_posts:
                self._set_state("done", f"已达本次 max_posts={self.max_posts} 上限，可再次启动续跑")
                self.manifest.save()
                return

            await scheduler.wait_for_slot()  # 睡到日程点（可被 cancel_task 取消）
            if self._cancel_requested:
                self._set_state("cancelled")
                return

            try:
                page = await preview_profile(self.username, cursor=m["cursor"], limit=settings.auto_page_size)
                if not page.posts and page.has_more:
                    raise CollectorError("parse", "翻页返回空页（页面异常）", 502)
            except asyncio.CancelledError:
                raise
            except CollectorError as exc:
                action = self._handle_collector_error(exc)
                if action == "fail":
                    self._set_state("failed", str(exc))
                    self.manifest.save()
                    return
                if action == "park":
                    if not await self._park():
                        self._set_state("cancelled")
                        return
                    self._set_state("running")
                    self.resume_at = None
                continue

            # 成功翻页：连败清零 + 记录用户信息
            self._consecutive_failures = 0
            if page.mediacount:
                m["mediacount"] = page.mediacount

            page_skips = 0
            for post in page.posts:
                if self._cancel_requested:
                    break
                if await self._process_post(post):
                    page_skips += 1

            m["cursor"] = page.next_cursor if page.next_cursor is not None else m["cursor"] + len(page.posts)
            if not page.has_more:
                m["exhausted"] = True
            elif self._refresh_mode and page.posts and page_skips == len(page.posts):
                # 增量刷新：整页已知帖 → 已达旧覆盖区（新帖只出现在头部，连续区间）
                m["exhausted"] = True
            touch_profile_cache(self.username)  # 保温：任务活跃期不被 TTL 淘汰
            self.manifest.append_nodes(profile_cache_nodes(self.username))
            self.manifest.save()
            scheduler.report_finish()

    # ---- 失败分类（design D9） ----

    def _handle_collector_error(self, exc: CollectorError) -> str:
        """返回 "park"（暂停等待/需人工）、"fail"（终态失败）或 "retry"（下一名额重试）。"""
        if exc.kind in _SIGNAL_KINDS:
            scheduler.invalidate()  # 作废当前窗口，恢复后重排日程
            sig = daily_signal_status()
            if sig["count"] >= sig["limit"]:
                self.resume_at = _next_midnight() + random.uniform(60, 600)
                self._set_state(
                    "paused_signal_budget",
                    f"今日风控信号已达上限（{sig['count']}/{sig['limit']}），次日自动恢复",
                )
            else:
                st = cooldown.status()
                extra = random.uniform(30, settings.auto_resume_jitter_sec)
                self.resume_at = time.time() + st["remaining_seconds"] + extra
                self._set_state(
                    "paused_cooldown",
                    f"风控信号（{st.get('last_reason')}），冷却后自动恢复",
                )
            self.manifest.save()
            return "park"
        if exc.kind == "cooldown":
            st = cooldown.status()
            self.resume_at = time.time() + st["remaining_seconds"] + random.uniform(10, 60)
            self._set_state("paused_cooldown", "采集冷却中，稍后自动恢复")
            return "park"
        if exc.kind == "not_found" and not self.manifest.data["posts"]:
            return "fail"  # 首页即 404（用户不存在/私密）→ 重试无意义

        # 其余（parse/not_found 中途/invalid_url…）→ 连续失败计数（design D9 熔断）
        self._note_failure(f"{exc.kind}: {exc}")
        self._consecutive_failures += 1
        if self._consecutive_failures >= settings.auto_fail_limit:
            scheduler.invalidate()
            self.resume_at = None  # 需人工介入
            self._set_state(
                "paused_circuit",
                f"连续 {self._consecutive_failures} 次采集失败（最近：{exc.kind}），已熔断；"
                f"请排查（页面结构/登录态）后取消并重新启动任务",
            )
            self.manifest.save()
            return "park"
        return "retry"

    async def _park(self) -> bool:
        """暂停等待 resume_at（分片睡眠，及时响应取消）。返回 False 表示已请求取消。

        resume_at=None（熔断）→ 无限期等待人工取消/重启。
        """
        while not self._cancel_requested:
            if self.resume_at is None:
                await asyncio.sleep(2)
                continue
            remain = self.resume_at - time.time()
            if remain <= 0:
                return True
            await asyncio.sleep(min(remain, 2))
        return False

    # ---- 帖子处理与下载（design D6） ----

    async def _process_post(self, post: ProfilePost) -> bool:
        """处理一帖。返回 True 表示该帖为已知完成帖（skipped，用于增量刷新停判）。"""
        m = self.manifest.data
        rec = m["posts"].get(post.shortcode)
        if rec and rec.get("status") in ("done", "partial"):
            m["counts"]["skipped"] += 1
            return True

        files: List[str] = []
        errors: List[str] = []
        reused: List[str] = []
        await asyncio.gather(
            *(self._download_resource(post.shortcode, r, files, errors, reused) for r in post.resources)
        )
        if files:
            status = "done" if not errors else "partial"
            if len(reused) == len(post.resources) and not errors:
                # 全部资源为磁盘已有文件（如手动下载遗产）：未发生实际下载，计入跳过
                # （specs/auto-download"已存在文件跳过"语义）；仍写入清单供后续增量停判
                m["counts"]["skipped"] += 1
            else:
                m["counts"]["downloaded"] += 1
        else:
            status = "failed"
            m["counts"]["failed"] += 1
            self._note_failure("; ".join(errors) or "无媒体资源", post.shortcode)
        m["posts"][post.shortcode] = {
            "status": status,
            "files": files,
            "date_utc": post.date_utc,
        }
        self._processed_this_run += 1
        self.manifest.save()  # 每帖落盘（design D5）
        return False

    def _snapshot_nodes(self) -> None:
        """收尾：用最终缓存快照整写边车（保持时间线顺序；增量刷新模式必须）。"""
        try:
            nodes = profile_cache_nodes(self.username)
        except Exception:
            return
        if nodes:
            self.manifest.rewrite_nodes(nodes)

    async def _download_resource(self, code: str, res: PostResource, files: List[str], errors: List[str], reused: List[str]) -> None:
        """下载单个媒体资源：exists&size>0 跳过；.part 原子落盘；退避重试 2 次。

        下载与重试不占动作名额（CDN 与 instagram.com 风控相互独立，design D6）。
        文件已存在时记入 reused（供帖级"跳过/下载"计数区分）。
        """
        async with self._dl_sem:
            final = self.folder / res.filename
            try:
                if final.exists() and final.stat().st_size > 0:
                    files.append(str(final).replace("\\", "/"))
                    reused.append(res.filename)
                    return
            except OSError:
                pass
            part = final.with_suffix(final.suffix + ".part")
            last_exc: Optional[Exception] = None
            for attempt in range(3):  # 1 + 2 重试
                try:
                    await _download_file(res.url, part)
                    os.replace(part, final)
                    files.append(str(final).replace("\\", "/"))
                    return
                except Exception as exc:
                    last_exc = exc
                    if attempt < 2:
                        await asyncio.sleep(_DOWNLOAD_RETRY_BACKOFF[attempt])
            errors.append(f"{res.filename}: {type(last_exc).__name__}: {last_exc}")
            self._note_failure(f"{res.filename}: {type(last_exc).__name__}: {last_exc}", code)


class AutoJobManager:
    """进程级注册表；全局至多一个活跃任务（design D5 Non-Goal：单任务串行）。"""

    def __init__(self, download_root: Path):
        self._download_root = Path(download_root)
        self._jobs: Dict[str, AutoJob] = {}
        self._lock = asyncio.Lock()

    async def start(self, username: str, max_posts: Optional[int] = None) -> AutoJob:
        async with self._lock:
            active = self.active_job()
            if active is not None:
                raise AutoJobError(
                    "active_job",
                    f"已有活跃任务（{active.username}，job_id={active.job_id}），请等待完成或先取消",
                )
            job = AutoJob(username, max_posts, self._download_root)
            job.manifest.load(username)  # 损坏 → AutoJobError，任务不创建
            job.start()
            self._jobs[job.job_id] = job
            return job

    def get(self, job_id: str) -> Optional[AutoJob]:
        return self._jobs.get(job_id)

    def active_job(self) -> Optional[AutoJob]:
        for job in self._jobs.values():
            if not job.is_terminal:
                return job
        return None

    async def shutdown(self) -> None:
        """应用关闭：取消全部任务并等待退出。"""
        for job in list(self._jobs.values()):
            await job.cancel_task()

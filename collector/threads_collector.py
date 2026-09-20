"""Threads（threads.com）用户主页采集器。

数据来源（probe 实测 2026-07）：
- timeline: data.mediaData.edges[].node（每个 node = 一个 thread）
- 帖子媒体: node.thread_items[0].post（结构同 IG media 节点 → 复用 IG 解析）
- 分页: mediaData.page_info.{end_cursor, has_next_page}
- 用户信息: post.user（username/full_name/profile_pic_url）
- 媒体域名: cdninstagram.com（已在 IG 白名单）
- URL: threads.com/@user（threads.net 会重定向到 .com），帖子 /@user/post/{code}

采集链路同 IG preview_profile（持久 page 续传 + cooldown/信号检测；名额由调用方负责）。
复用 IG 的 _scroll_until / _node_to_profile_post / _SIGNAL_KIND / dataclass（post 结构一致）。
page 池键用 `threads:{username}` 前缀，避免与 IG 同名用户冲突。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Dict, Optional
from urllib.parse import parse_qs, urlencode

from collector.browser_session import session
from collector.config import settings
from collector.instagram_collector import (
    CollectorError,
    ProfilePost,
    ProfilePreview,
    _SIGNAL_KIND,
    _best_image_url,
    _best_video_url,
    _node_to_profile_post,
)
from collector.safety import CoolDown, cooldown, detect_signal

__all__ = ["preview_threads_profile", "threads_post_resources", "ProfilePost", "ProfilePreview"]

THREADS_PROFILE_URL = "https://www.threads.com/@{username}"
THREADS_GRAPHQL_URL = "https://www.threads.com/graphql/query"
_THREADS_USERNAME_RE = re.compile(r"[A-Za-z0-9._]{1,30}")
_THREADS_RESERVED = {"p", "post", "login", "search", "explore", "settings", "account"}
_THREADS_MAX_LIMIT = 24
_THREADS_CACHE_TTL = 600
_threads_cache: Dict[str, dict] = {}
_TIMELINE_CONN_KEY = "mediaData"


def _extract_threads_username(value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise CollectorError("invalid_url", "Threads username is required", 400)
    if "threads.net" in v or "threads.com" in v:
        m = re.search(r"threads\.(?:net|com)/@?([^/?#]+)/?", v)
        if not m:
            raise CollectorError("invalid_url", "Unable to read username from URL", 400)
        v = m.group(1)
    v = v.strip().lstrip("@")
    if v in _THREADS_RESERVED:
        raise CollectorError("invalid_url", "请输入用户主页而非其他页面", 400)
    if not _THREADS_USERNAME_RE.fullmatch(v):
        raise CollectorError("invalid_url", "Invalid Threads username", 400)
    return v


def _thread_post(node: dict) -> dict:
    """threads edge.node → thread_items[0].post（媒体节点，结构同 IG media）。"""
    tis = node.get("thread_items") if isinstance(node, dict) else None
    if not tis:
        return {}
    first = tis[0] if isinstance(tis, list) else {}
    return (first.get("post") or {}) if isinstance(first, dict) else {}


def _thread_code(node: dict) -> Optional[str]:
    return _thread_post(node).get("code")


def _has_media(post: dict) -> bool:
    """是否有可下载的有效媒体（跳过纯文字帖 / media_type=19 无图引用帖）。"""
    if not isinstance(post, dict):
        return False
    return bool(_best_image_url(post) or _best_video_url(post) or post.get("carousel_media"))


def _thread_node_to_post(node: dict, fallback_owner: str, index: int) -> Optional[ProfilePost]:
    post = _thread_post(node)
    if not post.get("code"):
        return None
    if not _has_media(post):  # 跳过纯文字帖 / 无效媒体
        return None
    p = _node_to_profile_post(post, fallback_owner, index)  # 复用 IG 解析（shortcode/date/type/thumbnail/resources）
    owner = p.owner_username or fallback_owner
    p.url = f"https://www.threads.com/@{owner}/post/{post.get('code')}"  # 覆盖为 threads URL
    return p


def _parse_threads_meta(html: str) -> int:
    """threads 不直接暴露帖子总数，首版返回 0（前端容错；后续可从 og:description 解析）。"""
    return 0


def _build_threads_response(username: str, entry: dict, cursor: int, limit: int) -> ProfilePreview:
    nodes = entry["nodes"]
    slice_nodes = nodes[cursor:cursor + limit]
    posts = [p for p in (_thread_node_to_post(n, username, cursor + i + 1) for i, n in enumerate(slice_nodes)) if p]
    exhausted = entry["exhausted"]
    returned = len(slice_nodes)
    end_offset = cursor + returned
    # threads 翻页用 graphql 重放（_fetch_more），has_more 基于是否还有更多
    has_more = (not exhausted) or (end_offset < len(nodes))
    if exhausted and end_offset >= len(nodes):
        has_more = False
    next_cursor = end_offset if has_more else None
    ui = entry["user_info"]
    return ProfilePreview(
        username=username,
        full_name=ui.get("full_name"),
        profile_pic_url=ui.get("profile_pic_url"),
        is_private=ui.get("is_private", False),
        mediacount=ui.get("mediacount") or (len(nodes) if exhausted else 0),
        cursor=cursor,
        next_cursor=next_cursor,
        has_more=has_more,
        posts=posts,
    )


async def _fetch_more(page, first_req, captured, have_codes, state, need, scroll_lo, scroll_hi):
    """重放 threads graphql 翻页（after=end_cursor），直到 captured>=need 或 exhausted。

    threads 浏览器滚动不触发翻页 graphql（实测），故用 page.context.request 重放
    （继承 cookies + 首屏 headers），variables.after 推进。
    """
    if not first_req.get("post_data") or not first_req.get("headers"):
        return
    attempts = 0
    while len(captured) < need and not state["exhausted"] and state["end_cursor"] and attempts < 30:
        try:
            pd = parse_qs(first_req["post_data"])
            variables = json.loads(pd["variables"][0])
            variables["after"] = state["end_cursor"]
            pd["variables"] = [json.dumps(variables)]
            body = urlencode({k: v[0] for k, v in pd.items()})
            resp = await page.context.request.post(THREADS_GRAPHQL_URL, data=body, headers=first_req["headers"])
            data = await resp.json()
        except Exception:
            break
        md = (data.get("data") or {}).get(_TIMELINE_CONN_KEY) or {}
        for edge in md.get("edges") or []:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict):
                continue
            code = _thread_code(node)
            if code and code not in have_codes and _has_media(_thread_post(node)):
                captured.append(node)
                have_codes.add(code)
        pi = md.get("page_info") or {}
        if pi.get("end_cursor"):
            state["end_cursor"] = pi["end_cursor"]
        if "has_next_page" in pi:
            state["exhausted"] = not bool(pi.get("has_next_page"))
        await asyncio.sleep(random.uniform(scroll_lo, scroll_hi))
        attempts += 1


async def preview_threads_profile(
    profile_value: str,
    cursor: int = 0,
    limit: Optional[int] = None,
) -> ProfilePreview:
    """采集 Threads 用户主页帖子列表（async，整型 cursor 偏移分页 + 持久 page 续传）。
    名额由调用方负责（手动路由 acquire_now）。"""
    username = _extract_threads_username(profile_value)
    bounded_limit = max(1, min(limit or settings.profile_page_size, _THREADS_MAX_LIMIT))
    if cursor < 0:
        raise CollectorError("invalid_url", "cursor 必须 >= 0", 400)

    try:
        cooldown.check()
    except CoolDown as exc:
        raise CollectorError(
            "cooldown",
            f"采集冷却中，约 {int(exc.remaining_seconds)}s（原因：{exc.reason}）",
            429,
        ) from exc

    pool_key = f"threads:{username}"
    need = cursor + bounded_limit
    now = time.time()
    entry = _threads_cache.get(username)

    if entry and (now - entry["fetched_at"] >= _THREADS_CACHE_TTL):
        await session.close_profile_page(pool_key)
        entry = None

    if entry and (len(entry["nodes"]) >= need or entry["exhausted"]):
        return _build_threads_response(username, entry, cursor, bounded_limit)

    url = THREADS_PROFILE_URL.format(username=username)
    nav_lo, nav_hi = settings.nav_stabilize
    scroll_lo, scroll_hi = settings.scroll_delay

    captured: list = list(entry["nodes"]) if entry else []
    have_codes = {_thread_code(n) for n in captured}
    user_info = dict(entry["user_info"]) if entry and entry.get("user_info") else {}
    state = {
        "end_cursor": entry["end_cursor"] if entry else None,
        "exhausted": entry["exhausted"] if entry else False,
    }
    can_resume = bool(entry and entry.get("navigated"))
    first_req = {"post_data": None, "headers": None}
    if entry and entry.get("first_req"):
        first_req["post_data"] = entry["first_req"].get("post_data")
        first_req["headers"] = entry["first_req"].get("headers")

    async with session.profile_page(pool_key) as page:
        def on_request(req):
            if first_req["post_data"] is None and "graphql/query" in req.url:
                pd = req.post_data or ""
                if "BarcelonaProfileThreadsTab" in pd:
                    first_req["post_data"] = pd
                    try:
                        first_req["headers"] = dict(req.headers)
                    except Exception:
                        pass

        async def on_response(resp):
            try:
                if "json" not in resp.headers.get("content-type", ""):
                    return
                body = await resp.text()
                data = json.loads(body)
            except Exception:
                return
            conn = (data.get("data") or {}).get(_TIMELINE_CONN_KEY)
            if not conn:
                return
            for edge in conn.get("edges") or []:
                node = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict):
                    continue
                code = _thread_code(node)
                if code and code not in have_codes and _has_media(_thread_post(node)):
                    captured.append(node)
                    have_codes.add(code)
                    if not user_info:
                        u = _thread_post(node).get("user") or {}
                        if u.get("username"):
                            user_info["full_name"] = u.get("full_name")
                            user_info["profile_pic_url"] = u.get("profile_pic_url")
                            user_info["is_private"] = False
            pi = conn.get("page_info") or {}
            if pi.get("end_cursor"):
                state["end_cursor"] = pi.get("end_cursor")
            if "has_next_page" in pi:
                state["exhausted"] = not bool(pi.get("has_next_page"))

        page.on("request", on_request)
        # 持久 page 跨调用复用 → 移除上次 response listener、注册本次
        prev_listener = getattr(page, "_xins_listener", None)
        if prev_listener is not None:
            try:
                page.remove_listener("response", prev_listener)
            except Exception:
                pass
        page.on("response", on_response)
        page._xins_listener = on_response

        async def do_navigate_and_fetch():
            nonlocal user_info
            captured.clear()
            have_codes.clear()
            first_req["post_data"] = None
            first_req["headers"] = None
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(random.uniform(nav_lo, nav_hi))
            signal = detect_signal(url=page.url)
            if signal:
                cooldown.report(signal)
                kind, status = _SIGNAL_KIND.get(signal, ("parse", 502))
                raise CollectorError(kind, f"Threads 风控信号：{signal}", status)
            html = await page.content()
            await asyncio.sleep(1.0)  # 等首屏 mediaData 响应
            await _fetch_more(page, first_req, captured, have_codes, state, need, scroll_lo, scroll_hi)
            if not user_info.get("mediacount"):
                user_info["mediacount"] = _parse_threads_meta(html)

        async def do_resume_fetch() -> bool:
            if "threads.com" not in (page.url or ""):
                return False
            before = len(captured)
            await _fetch_more(page, first_req, captured, have_codes, state, need, scroll_lo, scroll_hi)
            signal = detect_signal(url=page.url)
            if signal:
                cooldown.report(signal)
                kind, status = _SIGNAL_KIND.get(signal, ("parse", 502))
                raise CollectorError(kind, f"Threads 风控信号：{signal}", status)
            if len(captured) == before and not state["exhausted"]:
                return False
            return True

        if can_resume:
            ok = await do_resume_fetch()
            if not ok:
                await do_navigate_and_fetch()
        else:
            await do_navigate_and_fetch()

    entry = {
        "nodes": captured,
        "end_cursor": state["end_cursor"],
        "exhausted": state["exhausted"],
        "fetched_at": time.time(),
        "user_info": user_info,
        "navigated": True,
        "first_req": first_req,
    }
    _threads_cache[username] = entry

    if not captured and not user_info.get("mediacount"):
        raise CollectorError(
            "not_found",
            "未找到 Threads 内容（用户不存在/私密/页面结构变更）",
            404,
        )
    return _build_threads_response(username, entry, cursor, bounded_limit)


def _filter_resources(resources, selected_indices):
    if not selected_indices:
        return resources
    return [r for r in resources if r.index in selected_indices]


async def threads_post_resources(username: str, code: str, selected_indices=None):
    """从 Threads 缓存取某帖媒体资源；缓存未命中报错（threads 无单帖采集兜底）。"""
    entry = _threads_cache.get(username)
    if entry:
        for node in entry["nodes"]:
            if _thread_code(node) == code:
                post = _thread_node_to_post(node, username, 1)
                return _filter_resources(post.resources if post else [], selected_indices)
    raise CollectorError("not_found", "该 Threads 帖子不在缓存中，请先预览该用户主页", 404)

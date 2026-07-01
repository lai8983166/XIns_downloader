"""Instagram 采集器（async）。

数据来源（实测 2026-06，见 Phase 1 调研）：
- 单帖：主帖完整数据内联在帖子页 SSR HTML 的 relay store（<script data-sjs>）。
  preview_post 从 page.content() 解析 relay store，按 code==shortcode 定位 media 节点
  （media_type 1/2/8 + image_versions2.candidates 取最大 + video_versions 取最大）。
- Profile：滚动 + 捕获浏览器自身网络响应（Phase 5 实现）。

采集链路：
    preview_post → cooldown.check → quota.acquire → session.page（asyncio.Lock 串行）
                 → 导航（随机抖动）→ detect_signal（命中即停手 + 进入冷却）
                 → 解析 relay store。
所有错误统一为 CollectorError(kind, status)，路由层按 kind 映射 HTTP（Phase 6）。

只读契约：__all__ 仅公开读取/解析 API，禁止任何写操作（点赞/关注/评论/收藏/分享）。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from collector.browser_session import session
from collector.config import settings
from collector.quota import QuotaExceeded, quota
from collector.safety import CoolDown, cooldown, detect_signal

__all__ = [
    "preview_post",
    "PostPreview",
    "PostResource",
    "CollectorError",
    "preview_profile",
    "ProfilePost",
    "ProfilePreview",
    "profile_post_resources",
]

INSTAGRAM_POST_URL = "https://www.instagram.com/p/{shortcode}/"
_SHORTCODE_RE = re.compile(r"[A-Za-z0-9_-]+")
_DATA_SJS_RE = re.compile(r"<script[^>]*data-sjs[^>]*>(.*?)</script>", re.DOTALL)

# 风控信号 → (CollectorError kind, HTTP status)
_SIGNAL_KIND = {
    "rate_limited": ("rate_limited", 429),
    "action_blocked": ("rate_limited", 429),
    "challenge": ("login_required", 401),
    "login_wall": ("login_required", 401),
    "unexpected": ("parse", 502),
}


class CollectorError(Exception):
    """采集层错误。kind: invalid_url | not_found | login_required | rate_limited |
    quota | cooldown | parse。status: 建议的 HTTP 状态码。"""

    def __init__(self, kind: str, message: str, status: int = 500):
        self.kind = kind
        self.status = status
        super().__init__(message)


@dataclass
class PostResource:
    index: int
    type: str  # "image" | "video"
    url: str
    thumbnail_url: Optional[str] = None
    filename: str = ""


@dataclass
class PostPreview:
    shortcode: str
    owner_username: Optional[str] = None
    caption: Optional[str] = None
    resources: List[PostResource] = field(default_factory=list)


# ----------------------------- 媒体解析（纯函数） -----------------------------


def _best_image_url(media: dict) -> Optional[str]:
    """取 image_versions2.candidates 中面积最大的（原图）。"""
    cands = ((media.get("image_versions2") or {}).get("candidates")) or []
    if not cands:
        return None
    best = max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
    return best.get("url")


def _best_video_url(media: dict) -> Optional[str]:
    """取 video_versions 中宽度最大的（最高清）。"""
    versions = media.get("video_versions") or []
    if not versions:
        return None
    best = max(versions, key=lambda v: (v.get("width") or 0))
    return best.get("url")


def _build_filename(shortcode: str, index: int, media_type: str, media_url: str) -> str:
    suffix = ".mp4" if media_type == "video" else ".jpg"
    clean_url = media_url.split("?")[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"):
        if clean_url.endswith(ext):
            suffix = ext
            break
    return f"{shortcode}_{index}{suffix}"


def _child_to_resource(child: dict, shortcode: str, index: int) -> PostResource:
    video_url = _best_video_url(child)
    if video_url:
        media_type = "video"
        url = video_url
        thumbnail = _best_image_url(child) or video_url
    else:
        media_type = "image"
        url = _best_image_url(child)
        thumbnail = url
    if not url:
        raise CollectorError("parse", f"第 {index} 个资源缺少媒体 URL", 502)
    return PostResource(
        index=index,
        type=media_type,
        url=url,
        thumbnail_url=thumbnail,
        filename=_build_filename(shortcode, index, media_type, url),
    )


def _media_to_resources(media: dict, shortcode: str) -> List[PostResource]:
    """media_type: 1=单图, 2=单视频, 8=相册(carousel)。"""
    if media.get("media_type") == 8:
        children = media.get("carousel_media") or []
        return [_child_to_resource(ch, shortcode, i) for i, ch in enumerate(children, start=1)]
    return [_child_to_resource(media, shortcode, 1)]


def _find_media_by_code(obj, shortcode: str) -> Optional[dict]:
    if isinstance(obj, dict):
        if obj.get("code") == shortcode and ("image_versions2" in obj or "carousel_media" in obj):
            return obj
        for v in obj.values():
            found = _find_media_by_code(v, shortcode)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_media_by_code(v, shortcode)
            if found is not None:
                return found
    return None


def _extract_media_node(html: str, shortcode: str) -> Optional[dict]:
    """从 SSR HTML 的 data-sjs relay store 中提取目标帖子的 media 节点。"""
    for script_body in _DATA_SJS_RE.findall(html):
        try:
            data = json.loads(script_body)
        except (ValueError, TypeError):
            continue
        node = _find_media_by_code(data, shortcode)
        if node is not None:
            return node
    return None


# ----------------------------- 采集入口（async） -----------------------------


async def preview_post(shortcode: str) -> PostPreview:
    """采集单帖预览（async）。

    Raises CollectorError(kind, status)：
      invalid_url(400) / login_required(401) / rate_limited(429) /
      quota(429) / cooldown(429) / not_found(404) / parse(502)
    """
    if not shortcode or not _SHORTCODE_RE.fullmatch(shortcode):
        raise CollectorError("invalid_url", "Invalid shortcode", 400)

    # 冷却期直接拒（不消耗配额）
    try:
        cooldown.check()
    except CoolDown as exc:
        raise CollectorError(
            "cooldown",
            f"采集冷却中，约 {int(exc.remaining_seconds)}s 后恢复（原因：{exc.reason}）",
            429,
        ) from exc

    # 配额
    try:
        quota.acquire()
    except QuotaExceeded as exc:
        raise CollectorError("quota", str(exc), 429) from exc

    url = INSTAGRAM_POST_URL.format(shortcode=shortcode)
    nav_lo, nav_hi = settings.nav_stabilize

    async with session.page() as page:  # 串行 + 懒启动 + 守护
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # 人类化节奏（design D5）
        await asyncio.sleep(random.uniform(nav_lo, nav_hi))

        # 信号检测：命中即停手 + 进入冷却（design D7）
        signal = detect_signal(url=page.url)
        if signal:
            cooldown.report(signal)
            kind, status = _SIGNAL_KIND.get(signal, ("parse", 502))
            raise CollectorError(kind, f"Instagram 风控信号：{signal}", status)

        html = await page.content()

    media = _extract_media_node(html, shortcode)
    if media is None:
        raise CollectorError(
            "not_found",
            "未找到帖子数据（帖子可能已删除、为私密账号或页面结构变更）",
            404,
        )

    caption = media.get("caption")
    if isinstance(caption, dict):
        caption_text = caption.get("text")
    elif isinstance(caption, str):
        caption_text = caption
    else:
        caption_text = None

    user = media.get("user") or {}

    return PostPreview(
        shortcode=media.get("code") or shortcode,
        owner_username=user.get("username"),
        caption=caption_text,
        resources=_media_to_resources(media, shortcode),
    )


# ----------------------------- Profile 采集 -----------------------------

INSTAGRAM_PROFILE_URL = "https://www.instagram.com/{username}/"
_PROFILE_USERNAME_RE = re.compile(r"[A-Za-z0-9._]{1,30}")
_PROFILE_RESERVED = {"p", "reel", "tv", "stories", "explore", "accounts", "api"}
_PROFILE_MAX_LIMIT = 24
_PROFILE_CACHE_TTL = 600  # 秒；design D6 缓存
_profile_cache: Dict[str, dict] = {}

# user_timeline connection 的 GraphQL data 键
_TIMELINE_CONN_KEY = "xdt_api__v1__feed__user_timeline_graphql_connection"


@dataclass
class ProfilePost:
    shortcode: str
    index: int
    url: str
    owner_username: Optional[str] = None
    caption: Optional[str] = None
    date_utc: str = ""
    type: str = "image"  # image | video | sidecar
    media_count: int = 0
    thumbnail_url: str = ""
    resources: List[PostResource] = field(default_factory=list)


@dataclass
class ProfilePreview:
    username: str
    full_name: Optional[str] = None
    profile_pic_url: Optional[str] = None
    is_private: bool = False
    mediacount: int = 0
    cursor: int = 0
    next_cursor: Optional[int] = None
    has_more: bool = False
    posts: List[ProfilePost] = field(default_factory=list)


def _extract_profile_username(value: str) -> str:
    profile = (value or "").strip()
    if not profile:
        raise CollectorError("invalid_url", "Profile username is required", 400)
    if "instagram.com" in profile:
        m = re.search(r"instagram\.com/([^/?#]+)/?", profile)
        if not m:
            raise CollectorError("invalid_url", "Unable to read username from URL", 400)
        profile = m.group(1)
    profile = profile.strip().lstrip("@")
    if profile in _PROFILE_RESERVED:
        raise CollectorError("invalid_url", "请输入用户主页而非帖子/其他页面", 400)
    if not _PROFILE_USERNAME_RE.fullmatch(profile):
        raise CollectorError("invalid_url", "Invalid Instagram username", 400)
    return profile


def _parse_profile_meta(html: str) -> dict:
    """从 SSR HTML 的 og/meta 解析用户基本信息（full_name / 头像 / 帖子总数）。"""

    def _meta(prop: str) -> Optional[str]:
        m = re.search(rf'<meta property="{prop}" content="([^"]*)"', html)
        return m.group(1) if m else None

    title = _meta("og:title") or ""
    if " (@" in title:
        full_name = title.split(" (@")[0].strip()
    elif " •" in title:
        full_name = title.split(" •")[0].strip()
    else:
        full_name = None
    mediacount = 0
    m = re.search(r"([\d.,]+)\s+Posts", _meta("og:description") or "")
    if m:
        try:
            mediacount = int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return {
        "full_name": full_name or None,
        "profile_pic_url": _meta("og:image"),
        "mediacount": mediacount,
        "is_private": False,
    }


def _node_to_profile_post(node: dict, fallback_owner: str, index: int) -> ProfilePost:
    code = node.get("code") or ""
    resources = _media_to_resources(node, code)  # 复用单帖媒体解析
    media_type = node.get("media_type")
    post_type = "sidecar" if media_type == 8 else ("video" if media_type == 2 else "image")
    caption = node.get("caption")
    if isinstance(caption, dict):
        caption_text = caption.get("text")
    elif isinstance(caption, str):
        caption_text = caption
    else:
        caption_text = None
    taken_at = node.get("taken_at")
    date_utc = (
        datetime.fromtimestamp(taken_at, tz=timezone.utc).isoformat()
        if isinstance(taken_at, (int, float)) else ""
    )
    user = node.get("user") or {}
    return ProfilePost(
        shortcode=code,
        index=index,
        url=f"https://www.instagram.com/p/{code}/" if code else "",
        owner_username=user.get("username") or fallback_owner,
        caption=caption_text,
        date_utc=date_utc,
        type=post_type,
        media_count=len(resources),
        thumbnail_url=_best_image_url(node) or "",
        resources=resources,
    )


def _build_profile_response(username: str, entry: dict, cursor: int, limit: int) -> ProfilePreview:
    nodes = entry["nodes"]
    slice_nodes = nodes[cursor:cursor + limit]
    posts = [_node_to_profile_post(n, username, cursor + i + 1) for i, n in enumerate(slice_nodes)]
    exhausted = entry["exhausted"]
    returned = len(slice_nodes)
    end_offset = cursor + returned
    has_more = (end_offset < len(nodes)) or (not exhausted and returned == limit and returned > 0)
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


async def preview_profile(
    profile_value: str,
    cursor: int = 0,
    limit: Optional[int] = None,
) -> ProfilePreview:
    """采集 Profile 分页预览（async，整型 cursor 偏移分页）。

    - 冷却/配额/信号检测同 preview_post。
    - 内存缓存（TTL，design D6）：已加载范围内的翻页直接切片，不重复导航；
      超出已加载范围才重新导航 + 逐步滚动捕获更多。

    Raises CollectorError：invalid_url(400) / login_required(401) / rate_limited(429) /
      quota(429) / cooldown(429) / not_found(404) / parse(502)
    """
    username = _extract_profile_username(profile_value)
    bounded_limit = max(1, min(limit or settings.profile_page_size, _PROFILE_MAX_LIMIT))
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
    try:
        quota.acquire()
    except QuotaExceeded as exc:
        raise CollectorError("quota", str(exc), 429) from exc

    need = cursor + bounded_limit
    now = time.time()
    entry = _profile_cache.get(username)
    if entry and (now - entry["fetched_at"] < _PROFILE_CACHE_TTL) and (
        len(entry["nodes"]) >= need or entry["exhausted"]
    ):
        return _build_profile_response(username, entry, cursor, bounded_limit)

    captured: list = []
    state = {"end_cursor": None, "exhausted": False}
    url = INSTAGRAM_PROFILE_URL.format(username=username)
    nav_lo, nav_hi = settings.nav_stabilize
    scroll_lo, scroll_hi = settings.scroll_delay

    async with session.page() as page:
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
                if isinstance(node, dict) and node.get("code"):
                    captured.append(node)
            pi = conn.get("page_info") or {}
            state["end_cursor"] = pi.get("end_cursor")
            state["exhausted"] = not bool(pi.get("has_next_page"))

        page.on("response", on_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(random.uniform(nav_lo, nav_hi))

        signal = detect_signal(url=page.url)
        if signal:
            cooldown.report(signal)
            kind, status = _SIGNAL_KIND.get(signal, ("parse", 502))
            raise CollectorError(kind, f"Instagram 风控信号：{signal}", status)

        html = await page.content()
        await asyncio.sleep(1.0)  # 等首屏 user_timeline 响应

        # 逐步滚动，捕获更多帖子，直至够数或翻完（design D1/D5）
        attempts = 0
        while len(captured) < need and not state["exhausted"] and attempts < 40:
            await page.mouse.wheel(0, 4000)
            await asyncio.sleep(random.uniform(scroll_lo, scroll_hi))
            attempts += 1

        user_info = _parse_profile_meta(html)
        if captured:
            u = captured[0].get("user") or {}
            if "is_private" in u:
                user_info["is_private"] = bool(u.get("is_private"))

    entry = {
        "nodes": captured,
        "end_cursor": state["end_cursor"],
        "exhausted": state["exhausted"],
        "fetched_at": time.time(),
        "user_info": user_info,
    }
    _profile_cache[username] = entry

    if not captured and not user_info["mediacount"]:
        raise CollectorError(
            "not_found",
            "未找到 Profile 内容（用户不存在/私密未关注/页面结构变更）",
            404,
        )
    return _build_profile_response(username, entry, cursor, bounded_limit)


def _filter_resources(
    resources: List[PostResource], selected_indices: Optional[List[int]]
) -> List[PostResource]:
    if not selected_indices:
        return resources
    return [r for r in resources if r.index in selected_indices]


async def profile_post_resources(
    username: str,
    shortcode: str,
    selected_indices: Optional[List[int]] = None,
) -> List[PostResource]:
    """从 Profile 缓存取某帖媒体资源；缓存未命中则单帖采集兜底。

    供 /profile/download 复用：用户刚 preview 过的帖子缓存命中（快、不消耗配额、不联网）；
    未命中则 preview_post 单帖采集（消耗一次配额）。
    """
    entry = _profile_cache.get(username)
    if entry:
        for node in entry["nodes"]:
            if node.get("code") == shortcode:
                return _filter_resources(_media_to_resources(node, shortcode), selected_indices)
    preview = await preview_post(shortcode)  # 缓存未命中兜底（内部已含配额/冷却/信号检测）
    return _filter_resources(preview.resources, selected_indices)

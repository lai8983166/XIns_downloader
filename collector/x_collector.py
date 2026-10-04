"""X（原推特）用户主页采集器。

数据来源（probe 实测 2026-10，openspec/probe_x_auth_summary.json，含凭证不入库）：
- 登录态：人工在日常浏览器登录小号 + login_x_cookies.py 注入 auth_token/ct0。
  X 对自动化浏览器的【登录环节】检测最严（login_x.py 实测被限制），注入
  cookie 跳过该关卡；采集期探查 16/16 请求 200，不受影响。
- 时间线：浏览器自身发出的 graphql 响应（UserOriginalsTimeline / UserTweets 等），
  按【结构特征】（tweet_results 数组）识别而非 operationName（防前端改版）；
  滚动即触发翻页（浏览器自动生成 x-client-transaction-id 反爬头，无需重放）。
- 媒体：图片 pbs.twimg.com 直链（?format=..&name=orig 原图）；视频
  video_info.variants 取 content_type=video/mp4 的最大码率（跳过 HLS m3u8）。
- URL：x.com/{user}（twitter.com 兼容），推文 x.com/{user}/status/{id}。

采集链路同 IG/threads（持久 page 续传 + cooldown/信号检测；名额由调用方负责）。
page 池键 `x:{username}`。纯文字推文跳过（无有效媒体）。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from email.utils import parsedate_to_datetime
from typing import Dict, Iterator, List, Optional

from collector.browser_session import session
from collector.config import settings
from collector.instagram_collector import (
    CollectorError,
    PostResource,
    ProfilePost,
    ProfilePreview,
    _SIGNAL_KIND,
    _build_filename,
)
from collector.safety import CoolDown, cooldown, detect_signal

__all__ = [
    "preview_x_profile",
    "x_post_resources",
    "extract_x_username",
    "x_node_code",
    "x_tweet_resources",
    "rehydrate_x_cache",
    "touch_x_cache",
    "reset_x_cache",
    "x_cache_nodes",
    "ProfilePost",
    "ProfilePreview",
]

X_PROFILE_URL = "https://x.com/{username}"
_X_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{1,15}")
_X_RESERVED = {
    "home", "explore", "i", "search", "settings", "messages", "notifications",
    "compose", "intent", "hashtag", "share", "login", "signup",
}
_X_MAX_LIMIT = 24
_X_CACHE_TTL = 600  # 秒；对齐 IG/threads 缓存
_x_cache: Dict[str, dict] = {}


# ----------------------------- 用户名/解析（纯函数） -----------------------------


def extract_x_username(value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise CollectorError("invalid_url", "X username is required", 400)
    if "x.com" in v or "twitter.com" in v:
        m = re.search(r"(?:x|twitter)\.com/(?:#!)?@?([^/?#]+)/?", v)
        if not m:
            raise CollectorError("invalid_url", "Unable to read username from URL", 400)
        v = m.group(1)
    v = v.strip().lstrip("@")
    if v in _X_RESERVED:
        raise CollectorError("invalid_url", "请输入用户主页而非其他页面", 400)
    if not _X_USERNAME_RE.fullmatch(v):
        raise CollectorError("invalid_url", "Invalid X username", 400)
    return v


def x_node_code(node: dict) -> Optional[str]:
    """时间线推文节点 → 推文 id（rest_id / legacy.id_str；边车与 adapter 去重用）。"""
    if not isinstance(node, dict):
        return None
    tid = node.get("rest_id") or (node.get("legacy") or {}).get("id_str")
    return str(tid) if tid else None


def _tweet_media(tweet: dict) -> list:
    ext = ((tweet.get("legacy") or {}).get("extended_entities") or {})
    media = ext.get("media")
    return media if isinstance(media, list) else []


def _orig_image_url(url: Optional[str]) -> Optional[str]:
    """pbs 直链追加 ?format=..&name=orig 取原图。"""
    if not url or "?" in url:
        return url or None
    path = url.rsplit("/", 1)[-1]
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else "jpg"
    return f"{url}?format={ext}&name=orig"


def _best_video_variant(media: dict) -> Optional[str]:
    """video_info.variants 中 content_type=video/mp4 的最大码率直链（排除 HLS 等）。"""
    variants = (media.get("video_info") or {}).get("variants") or []
    mp4s = [v for v in variants if isinstance(v, dict) and v.get("content_type") == "video/mp4" and v.get("url")]
    if not mp4s:
        return None
    return max(mp4s, key=lambda v: v.get("bitrate") or 0).get("url")


def x_tweet_resources(tweet: dict, tweet_id: str) -> List[PostResource]:
    """推文节点 → 媒体资源列表（图片 orig 直链；视频最大码率 mp4；index 从 1 递增）。"""
    resources: List[PostResource] = []
    for i, m in enumerate(_tweet_media(tweet), start=1):
        if not isinstance(m, dict):
            continue
        video_url = _best_video_variant(m)
        if video_url:
            thumb = _orig_image_url(m.get("media_url_https")) or video_url
            resources.append(PostResource(
                index=i, type="video", url=video_url, thumbnail_url=thumb,
                filename=_build_filename(tweet_id, i, "video", video_url),
            ))
            continue
        image_url = _orig_image_url(m.get("media_url_https"))
        if image_url:
            resources.append(PostResource(
                index=i, type="image", url=image_url, thumbnail_url=image_url,
                filename=_build_filename(tweet_id, i, "image", image_url),
            ))
    return resources


def _has_media_x(tweet: dict) -> bool:
    if not isinstance(tweet, dict):
        return False
    return any(
        isinstance(m, dict) and (m.get("media_url_https") or _best_video_variant(m))
        for m in _tweet_media(tweet)
    )


def _tweet_owner(tweet: dict) -> Optional[str]:
    core = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
    return (core.get("legacy") or {}).get("screen_name")


def _tweet_to_profile_post(tweet: dict, fallback_owner: str, index: int) -> Optional[ProfilePost]:
    tid = x_node_code(tweet)
    if not tid:
        return None
    resources = x_tweet_resources(tweet, tid)
    if not resources:  # 跳过纯文字/无有效媒体推文
        return None
    legacy = tweet.get("legacy") or {}
    date_utc = ""
    try:
        date_utc = parsedate_to_datetime(legacy.get("created_at")).isoformat()
    except (TypeError, ValueError):
        pass
    post_type = "video" if any(r.type == "video" for r in resources) else ("sidecar" if len(resources) > 1 else "image")
    owner = _tweet_owner(tweet) or fallback_owner
    return ProfilePost(
        shortcode=tid,
        index=index,
        url=f"https://x.com/{owner}/status/{tid}",
        owner_username=owner,
        caption=legacy.get("full_text"),
        date_utc=date_utc,
        type=post_type,
        media_count=len(resources),
        thumbnail_url=resources[0].thumbnail_url or "",
        resources=resources,
    )


# ----------------------------- 时间线响应识别（结构特征，不绑 operationName） -----------------------------


def _iter_tweet_results(obj) -> Iterator[dict]:
    """递归遍历响应 JSON，产出所有 tweet_results.result 推文节点。

    兼容 __typename=Tweet / TweetWithVisibilityResults（后者媒体在 .tweet 内层）。
    """
    if isinstance(obj, dict):
        tr = obj.get("tweet_results")
        if isinstance(tr, dict):
            res = tr.get("result")
            if isinstance(res, dict):
                inner = res.get("tweet") if res.get("__typename") == "TweetWithVisibilityResults" else res
                if isinstance(inner, dict) and x_node_code(inner):
                    yield inner
        for v in obj.values():
            yield from _iter_tweet_results(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_tweet_results(v)


def _find_bottom_cursor(obj) -> Optional[str]:
    """递归找 TimelineTimelineCursor(cursorType=Bottom) 的 value（滚动续传游标）。"""
    if isinstance(obj, dict):
        if obj.get("entryType") == "TimelineTimelineCursor" and obj.get("cursorType") == "Bottom":
            v = obj.get("value")
            if v:
                return v
        for v in obj.values():
            c = _find_bottom_cursor(v)
            if c:
                return c
    elif isinstance(obj, list):
        for v in obj:
            c = _find_bottom_cursor(v)
            if c:
                return c
    return None


def _capture_timeline(data: dict, captured: list, have_codes: set, user_info: dict) -> None:
    """从一条时间线响应捕获推文节点（去重 + 媒体过滤）与用户信息。"""
    for tweet in _iter_tweet_results(data):
        tid = x_node_code(tweet)
        if tid and tid not in have_codes and _has_media_x(tweet):
            captured.append(tweet)
            have_codes.add(tid)
            if not user_info:
                core = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
                ul = core.get("legacy") or {}
                if ul.get("screen_name"):
                    user_info["full_name"] = ul.get("name")
                    user_info["profile_pic_url"] = ul.get("profile_image_url_https")
                    user_info["is_private"] = bool(core.get("protected"))


def _build_x_response(username: str, entry: dict, cursor: int, limit: int) -> ProfilePreview:
    nodes = entry["nodes"]
    slice_nodes = nodes[cursor:cursor + limit]
    posts = [p for p in (_tweet_to_profile_post(n, username, cursor + i + 1) for i, n in enumerate(slice_nodes)) if p]
    exhausted = entry["exhausted"]
    end_offset = cursor + len(slice_nodes)
    has_more = (not exhausted) or (end_offset < len(nodes))
    if exhausted and end_offset >= len(nodes):
        has_more = False
    ui = entry["user_info"]
    return ProfilePreview(
        username=username,
        full_name=ui.get("full_name"),
        profile_pic_url=ui.get("profile_pic_url"),
        is_private=ui.get("is_private", False),
        mediacount=ui.get("mediacount") or (len(nodes) if exhausted else 0),
        cursor=cursor,
        next_cursor=end_offset if has_more else None,
        has_more=has_more,
        posts=posts,
    )


async def _x_scroll_until(page, captured: list, have_codes: set, need: int, state: dict,
                          scroll_lo: float, scroll_hi: float) -> None:
    """逐步滚动触发时间线翻页；连续 2 轮零新帖（每轮内已轮询等待）视为翻尽。

    X 响应无 has_next_page 显式标记，翻尽以"滚到底无新推文"推断。
    """
    zero_rounds = 0
    attempts = 0
    while len(captured) < need and zero_rounds < 2 and attempts < 30:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(scroll_lo, scroll_hi))
        prev = len(captured)
        waited = 0
        while len(captured) == prev and waited < 8:
            await asyncio.sleep(0.5)
            waited += 1
        zero_rounds = zero_rounds + 1 if len(captured) == prev else 0
        attempts += 1
    if zero_rounds >= 2:
        state["exhausted"] = True


# ----------------------------- 采集入口（async） -----------------------------


async def preview_x_profile(
    profile_value: str,
    cursor: int = 0,
    limit: Optional[int] = None,
) -> ProfilePreview:
    """采集 X 用户主页推文列表（async，整型 cursor 偏移分页 + 持久 page 续传）。

    名额由调用方负责（手动路由 acquire_now / 自动任务 wait_for_slot）。
    Raises CollectorError：invalid_url(400) / login_required(401) / rate_limited(429) /
      cooldown(429) / not_found(404) / parse(502)
    """
    username = extract_x_username(profile_value)
    bounded_limit = max(1, min(limit or settings.profile_page_size, _X_MAX_LIMIT))
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

    pool_key = f"x:{username}"
    need = cursor + bounded_limit
    now = time.time()
    entry = _x_cache.get(username)

    if entry and (now - entry["fetched_at"] >= _X_CACHE_TTL):
        await session.close_profile_page(pool_key)
        entry = None

    if entry and (len(entry["nodes"]) >= need or entry["exhausted"]):
        return _build_x_response(username, entry, cursor, bounded_limit)

    url = X_PROFILE_URL.format(username=username)
    nav_lo, nav_hi = settings.nav_stabilize
    scroll_lo, scroll_hi = settings.scroll_delay

    captured: list = list(entry["nodes"]) if entry else []
    have_codes = {x_node_code(n) for n in captured}
    user_info = dict(entry["user_info"]) if entry and entry.get("user_info") else {}
    state = {"end_cursor": entry["end_cursor"] if entry else None, "exhausted": entry["exhausted"] if entry else False}
    can_resume = bool(entry and entry.get("navigated"))
    # 回灌缓存（自动任务恢复）：重新导航时不丢弃已回灌 nodes，重滚到的旧帖由 have_codes 去重
    keep_on_navigate = bool(entry and entry.get("rehydrated"))

    async with session.profile_page(pool_key) as page:
        # 登录态预检：给出明确的注入提示（导航后命中 /login 由信号链兜底）
        try:
            cookies = await page.context.cookies(["https://x.com"])
        except Exception:
            cookies = []
        if not any(c.get("name") == "auth_token" for c in cookies):
            raise CollectorError(
                "login_required",
                "X 未登录：请先在日常浏览器登录小号，再运行 python login_x_cookies.py 注入登录态",
                401,
            )

        async def on_response(resp):
            try:
                if "json" not in resp.headers.get("content-type", ""):
                    return
                body = await resp.text()
                data = json.loads(body)
            except Exception:
                return
            if "x.com" not in (resp.url or "") and "twitter.com" not in (resp.url or ""):
                return
            _capture_timeline(data, captured, have_codes, user_info)
            cursor_value = _find_bottom_cursor(data)
            if cursor_value:
                state["end_cursor"] = cursor_value

        # 持久 page 跨调用复用 → 移除上次 listener、注册本次
        prev_listener = getattr(page, "_xins_listener", None)
        if prev_listener is not None:
            try:
                page.remove_listener("response", prev_listener)
            except Exception:
                pass
        page.on("response", on_response)
        page._xins_listener = on_response

        async def do_navigate_and_scroll():
            if not keep_on_navigate:  # 回灌缓存时不丢弃（同 IG/threads rehydrated 保护）
                captured.clear()
                have_codes.clear()
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(random.uniform(nav_lo, nav_hi))
            signal = detect_signal(url=page.url)
            if signal:
                cooldown.report(signal)
                kind, status = _SIGNAL_KIND.get(signal, ("parse", 502))
                raise CollectorError(kind, f"X 风控信号：{signal}", status)
            await page.content()
            await asyncio.sleep(1.0)  # 等首屏时间线响应
            await _x_scroll_until(page, captured, have_codes, need, state, scroll_lo, scroll_hi)

        async def do_resume_scroll() -> bool:
            if "x.com" not in (page.url or ""):
                return False
            before = len(captured)
            await _x_scroll_until(page, captured, have_codes, need, state, scroll_lo, scroll_hi)
            signal = detect_signal(url=page.url)
            if signal:
                cooldown.report(signal)
                kind, status = _SIGNAL_KIND.get(signal, ("parse", 502))
                raise CollectorError(kind, f"X 风控信号：{signal}", status)
            if len(captured) == before and not state["exhausted"]:
                return False  # 零增长 → page 状态坏 → 降级重导航
            return True

        if can_resume:
            ok = await do_resume_scroll()
            if not ok:
                await do_navigate_and_scroll()
        else:
            await do_navigate_and_scroll()

    entry = {
        "nodes": captured,
        "end_cursor": state["end_cursor"],
        "exhausted": state["exhausted"],
        "fetched_at": time.time(),
        "user_info": user_info,
        "navigated": True,
    }
    _x_cache[username] = entry

    if not captured:
        # 零捕获（用户不存在/冻结/页面结构变更/坏 SPA 态）：废弃持久 page，下次重试用全新页面
        await session.close_profile_page(pool_key)
        raise CollectorError(
            "not_found",
            "未找到 X 内容（用户不存在/账号冻结/页面结构变更）",
            404,
        )
    return _build_x_response(username, entry, cursor, bounded_limit)


# ----------------------------- 自动任务挂钩（对齐 IG/threads） -----------------------------


def rehydrate_x_cache(username: str, nodes: List[dict], exhausted: bool) -> None:
    """把持久化的原始推文 nodes 回灌 X 缓存（自动任务恢复路径）。

    回灌条目 navigated=False → 下次 preview 重新导航；rehydrated 标志保证
    重导航不丢弃回灌数据，重滚到的旧帖由 have_codes 去重、新帖追加尾部。
    """
    if not nodes:
        return
    _x_cache[username] = {
        "nodes": list(nodes),
        "end_cursor": None,
        "exhausted": bool(exhausted),
        "fetched_at": time.time(),
        "user_info": {},
        "navigated": False,
        "rehydrated": True,
    }


def touch_x_cache(username: str) -> None:
    """刷新缓存 TTL（自动任务活跃期保温）。"""
    entry = _x_cache.get(username)
    if entry is not None:
        entry["fetched_at"] = time.time()


def reset_x_cache(username: str) -> None:
    """丢弃该 username 的 X 缓存（自动任务增量刷新：强制全新捕获保证时间线顺序）。"""
    _x_cache.pop(username, None)


def x_cache_nodes(username: str) -> List[dict]:
    """读取当前缓存原始 nodes 的浅拷贝（自动任务 nodes 边车增量落盘用）。"""
    entry = _x_cache.get(username)
    return list(entry["nodes"]) if entry else []


# ----------------------------- 手动下载资源（缓存取，同 threads 语义） -----------------------------


def _filter_resources(resources, selected_indices):
    if not selected_indices:
        return resources
    return [r for r in resources if r.index in selected_indices]


def x_post_resources(username: str, tweet_id: str, selected_indices=None):
    """从 X 缓存取某推文媒体资源；缓存未命中报错（X 无单帖采集兜底）。"""
    entry = _x_cache.get(username)
    if entry:
        for node in entry["nodes"]:
            if x_node_code(node) == tweet_id:
                post = _tweet_to_profile_post(node, username, 1)
                return _filter_resources(post.resources if post else [], selected_indices)
    raise CollectorError("not_found", "该 X 推文不在缓存中，请先预览该用户主页", 404)

"""x-download-mode 单测：推文解析纯函数 + 滚动捕获采集 + auto 挂钩（fake page，不联网）。

运行：python test_x_collector.py
fixture 为手工脱敏构造的推文节点（字段名对齐 probe 摘要；摘要含凭证不入库）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from contextlib import asynccontextmanager

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from collector import x_collector as xc
from collector.instagram_collector import CollectorError
from collector.safety import detect_signal
from collector.x_collector import (
    extract_x_username,
    rehydrate_x_cache,
    reset_x_cache,
    touch_x_cache,
    x_cache_nodes,
    x_node_code,
    x_tweet_resources,
)

_passed = 0
_failed = 0


def ok(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}")


# ----------------------------- fixture：真实结构推文节点 -----------------------------


def media_photo(mid: str = "HTvBaF1XoAA4RE6") -> dict:
    return {
        "id_str": "111",
        "type": "photo",
        "media_url_https": f"https://pbs.twimg.com/media/{mid}.jpg",
    }


def media_video(mid: str = "2106093156399726593") -> dict:
    return {
        "id_str": "222",
        "type": "video",
        "media_url_https": "https://pbs.twimg.com/ext_tw_video_thumb/222/pu/img/thumb.jpg",
        "video_info": {
            "variants": [
                {"bitrate": 256000, "content_type": "video/mp4",
                 "url": f"https://video.twimg.com/amplify_video/{mid}/vid/avc1/480x270/low.mp4?tag=29"},
                {"bitrate": 2176000, "content_type": "video/mp4",
                 "url": f"https://video.twimg.com/amplify_video/{mid}/vid/avc1/1280x720/high.mp4?tag=29"},
                {"bitrate": None, "content_type": "application/x-mpegURL",
                 "url": f"https://video.twimg.com/amplify_video/{mid}/pl/play.m3u8?tag=29"},
            ]
        },
    }


def mk_tweet(tid: str, medias=None, owner="elonmusk", created="Tue Sep 30 08:00:00 +0000 2026",
             text="hello world") -> dict:
    return {
        "__typename": "Tweet",
        "rest_id": tid,
        "legacy": {
            "id_str": tid,
            "full_text": text,
            "created_at": created,
            "extended_entities": {"media": medias if medias is not None else [media_photo()]},
        },
        "core": {"user_results": {"result": {
            "__typename": "User",
            "id": "20",
            "rest_id": "20",
            "legacy": {"screen_name": owner, "name": "Elon Musk",
                       "profile_image_url_https": "https://pbs.twimg.com/profile_images/u_400x400.jpg"},
        }}},
    }


def timeline_payload(tweets, cursor="HBgWbm90LXRoZS1jdXJzb3IAAA=="):
    """UserOriginalsTimeline 响应形状（脱敏）：instructions → entries → tweet_results。"""
    entries = []
    for t in tweets:
        entries.append({
            "entryId": f"tweet-{x_node_code(t)}",
            "content": {"entryType": "TimelineTimelineItem",
                        "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": t}}},
        })
    if cursor:
        entries.append({"entryId": "cursor-bottom-0",
                        "content": {"entryType": "TimelineTimelineCursor",
                                    "cursorType": "Bottom", "value": cursor}})
    return {"data": {"user": {"result": {"timeline": {"timeline": {
        "instructions": [{"type": "TimelineAddEntries", "entries": entries}],
    }}}}}}


# ----------------------------- fake 基础设施（滚动触发响应） -----------------------------


class FakeResponse:
    def __init__(self, data, url="https://x.com/i/api/graphql/abc/UserOriginalsTimeline"):
        self._data = data
        self.url = url
        self.headers = {"content-type": "application/json"}

    async def text(self) -> str:
        return json.dumps(self._data)


class FakePage:
    """最小 page：goto 发首屏时间线；evaluate(scrollTo) 弹出下一条脚本化时间线响应。"""

    def __init__(self, screens):
        self._screens = list(screens)  # 每次 goto/scroll 弹一个
        self._listeners = {}
        self.url = ""
        self.context = types.SimpleNamespace(cookies=self._cookies)

    async def _cookies(self, _urls):
        return [{"name": "auth_token", "value": "tok"}, {"name": "ct0", "value": "csrf"}]

    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        try:
            self._listeners.get(event, []).remove(handler)
        except ValueError:
            pass

    async def _dispatch(self, data):
        resp = FakeResponse(data)
        for handler in list(self._listeners.get("response", [])):
            await handler(resp)

    async def goto(self, url, **kwargs):
        self.url = url
        if self._screens:
            await self._dispatch(self._screens.pop(0))

    async def evaluate(self, script, *args):
        if "scrollTo" in script and self._screens:
            await self._dispatch(self._screens.pop(0))
        return None

    async def content(self) -> str:
        return "<html></html>"


class FakeSession:
    def __init__(self, page):
        self._page = page
        self.closed = []

    @asynccontextmanager
    async def profile_page(self, key):
        yield self._page

    async def close_profile_page(self, key):
        self.closed.append(key)


class FakeCooldown:
    def check(self):
        return None

    def status(self):
        return {"cooling_down": False, "remaining_seconds": 0.0, "last_reason": None}


def install_fakes(page):
    xc.session = FakeSession(page)
    xc.cooldown = FakeCooldown()
    xc.detect_signal = lambda url=None: None
    xc.settings = types.SimpleNamespace(
        nav_stabilize=(0, 0), scroll_delay=(0, 0), profile_page_size=6
    )


# ----------------------------- 1.1 纯函数 -----------------------------


def test_pure() -> None:
    print("=== extract_x_username ===")
    ok("@user / x.com / twitter.com 解析",
       extract_x_username("@elonmusk") == "elonmusk"
       and extract_x_username("https://x.com/elonmusk") == "elonmusk"
       and extract_x_username("https://twitter.com/elonmusk/with_replies") == "elonmusk")
    for bad in ("https://x.com/", "home", "", "invalid-name!"):
        try:
            extract_x_username(bad)
            ok(f"非法输入被拒：{bad!r}", False)
        except CollectorError as e:
            ok(f"非法输入被拒：{bad!r}", e.kind == "invalid_url")

    print("\n=== 推文媒体解析 ===")
    t = mk_tweet("2105718806769512812", medias=[media_photo()])
    rs = x_tweet_resources(t, "2105718806769512812")
    ok("单图：orig 直链 + filename",
       len(rs) == 1 and rs[0].type == "image"
       and rs[0].url == "https://pbs.twimg.com/media/HTvBaF1XoAA4RE6.jpg?format=jpg&name=orig"
       and rs[0].filename == "2105718806769512812_1.jpg")

    t = mk_tweet("999", medias=[media_video()])
    rs = x_tweet_resources(t, "999")
    ok("视频：取最大码率 mp4",
       len(rs) == 1 and rs[0].type == "video"
       and rs[0].url.endswith("/1280x720/high.mp4?tag=29")
       and rs[0].filename == "999_1.mp4")
    ok("视频：排除 m3u8", "m3u8" not in json.dumps([r.url for r in rs]))
    ok("视频缩略图来自图片直链", rs[0].thumbnail_url.startswith("https://pbs.twimg.com/"))

    t = mk_tweet("888", medias=[media_photo("A1"), media_video(), media_photo("A3")])
    rs = x_tweet_resources(t, "888")
    ok("多媒体：index 1..3 且类型一致",
       [r.index for r in rs] == [1, 2, 3] and [r.type for r in rs] == ["image", "video", "image"])

    t = mk_tweet("777", medias=[], text="纯文字推文")
    ok("纯文字推文 → 无资源", x_tweet_resources(t, "777") == [] and not xc._has_media_x(t))

    print("\n=== 推文 → ProfilePost ===")
    post = xc._tweet_to_profile_post(mk_tweet("123", medias=[media_photo()]), "fallback", 3)
    ok("shortcode=推文 id / status URL / 日期 RFC2822 解析",
       post.shortcode == "123"
       and post.url == "https://x.com/elonmusk/status/123"
       and post.date_utc.startswith("2026-09-30T08:00:00")
       and post.owner_username == "elonmusk")
    post2 = xc._tweet_to_profile_post(mk_tweet("124", medias=[media_photo(), media_photo("B2")]), "fb", 1)
    ok("双图 → sidecar", post2.type == "sidecar" and post2.media_count == 2)
    ok("纯文字推文被跳过（None）", xc._tweet_to_profile_post(mk_tweet("125", medias=[]), "fb", 1) is None)

    print("\n=== 结构特征识别（不绑 operationName） ===")
    payload = timeline_payload([mk_tweet("T1"), mk_tweet("T2")])
    found = list(xc._iter_tweet_results(payload))
    ok("递归找到 tweet_results（2 条）", [x_node_code(t) for t in found] == ["T1", "T2"])
    twvr = {"tweet_results": {"result": {
        "__typename": "TweetWithVisibilityResults",
        "tweet": mk_tweet("T3"),
    }}}
    ok("TweetWithVisibilityResults 内层展开", [x_node_code(t) for t in xc._iter_tweet_results(twvr)] == ["T3"])
    ok("bottom cursor 提取", xc._find_bottom_cursor(payload) == "HBgWbm90LXRoZS1jdXJzb3IAAA==")

    print("\n=== X 登录墙信号（既有 detect_signal 覆盖，逻辑不改） ===")
    ok("x.com/login 命中", detect_signal(url="https://x.com/login") == "login_wall")
    ok("x.com/i/flow/login 命中", detect_signal(url="https://x.com/i/flow/login") == "login_wall")


# ----------------------------- 1.2/1.3 采集与挂钩 -----------------------------


async def test_collect() -> None:
    print("\n=== 首屏 + 滚动捕获 → cursor 分页 ===")
    # 首屏 2 帖（1 条纯文字会被跳过），滚动 1 次再出 2 帖
    install_fakes(FakePage([
        timeline_payload([mk_tweet("T1"), mk_tweet("T2"), mk_tweet("TXT", medias=[])]),
        timeline_payload([mk_tweet("T3"), mk_tweet("T4")]),
        timeline_payload([mk_tweet("T5")]),
    ]))
    resp = await xc.preview_x_profile("elonmusk", cursor=0, limit=4)
    ok("纯文字跳过，返回 4 条含媒体推文", [p.shortcode for p in resp.posts] == ["T1", "T2", "T3", "T4"])
    ok("next_cursor=4 / has_more=True", resp.next_cursor == 4 and resp.has_more is True)
    ok("用户信息来自推文节点", resp.full_name == "Elon Musk" and resp.profile_pic_url.endswith("u_400x400.jpg"))
    entry = xc._x_cache["elonmusk"]
    ok("缓存 4 nodes 且 navigated=True / rehydrated 消失",
       len(entry["nodes"]) == 4 and entry["navigated"] is True and "rehydrated" not in entry)
    ok("滚动触发过翻页（FakePage 消耗了第 2 屏）", len(xc.session._page._screens) == 1)

    print("\n=== 翻尽：连续零新帖 → exhausted / has_more=False ===")
    resp2 = await xc.preview_x_profile("elonmusk", cursor=4, limit=4)  # 复用缓存续滚：T5 后再无新帖
    ok("剩余 1 条 + 翻尽", [p.shortcode for p in resp2.posts] == ["T5"] and resp2.has_more is False)
    ok("翻尽后 mediacount=已知帖数", resp2.mediacount == 5)

    print("\n=== 回灌 → 重导航：旧帖去重、新帖追加、captured 不清空 ===")
    install_fakes(FakePage([
        timeline_payload([mk_tweet("N1"), mk_tweet("T2"), mk_tweet("T1")]),  # 首屏：1 新 2 旧
        timeline_payload([mk_tweet("N2")]),
    ]))
    rehydrate_x_cache("resume_user", [mk_tweet("T1"), mk_tweet("T2")], False)
    rh = xc._x_cache["resume_user"]
    ok("回灌条目 rehydrated=True / navigated=False",
       rh.get("rehydrated") is True and rh.get("navigated") is False)
    resp3 = await xc.preview_x_profile("@resume_user", cursor=2, limit=2)
    ok("返回切片为新帖 [N1, N2]", [p.shortcode for p in resp3.posts] == ["N1", "N2"])
    ok("回灌 nodes 保留（未清空）+ 去重",
       [x_node_code(n) for n in x_cache_nodes("resume_user")] == ["T1", "T2", "N1", "N2"])
    ok("重导航后 rehydrated 消失", "rehydrated" not in xc._x_cache["resume_user"])

    print("\n=== 登录态缺失 → login_required(401) 注入提示 ===")
    page = FakePage([timeline_payload([mk_tweet("T1")])])
    async def no_cookies(_urls):
        return [{"name": "ct0", "value": "x"}]  # 无 auth_token
    page.context = types.SimpleNamespace(cookies=no_cookies)
    install_fakes(page)
    try:
        await xc.preview_x_profile("logged_out", cursor=0, limit=2)
        ok("未注入 → 401 login_required", False)
    except CollectorError as e:
        ok("未注入 → 401 login_required（提示注入工具）",
           e.kind == "login_required" and e.status == 401 and "login_x_cookies" in str(e))

    print("\n=== 零捕获 → not_found(404) + 废弃持久 page ===")
    install_fakes(FakePage([timeline_payload([], cursor=None)]))  # 空时间线
    try:
        await xc.preview_x_profile("stuck_user", cursor=0, limit=2)
        ok("零捕获 → 404", False)
    except CollectorError as e:
        ok("零捕获 → 404 not_found", e.kind == "not_found" and e.status == 404)
    ok("持久 page 被废弃", xc.session.closed == ["x:stuck_user"])

    print("\n=== 挂钩语义 ===")
    before = xc._x_cache["resume_user"]["fetched_at"]
    time.sleep(0.02)
    touch_x_cache("resume_user")
    ok("touch 刷新 TTL", xc._x_cache["resume_user"]["fetched_at"] > before)
    reset_x_cache("resume_user")
    ok("reset 丢弃缓存", x_cache_nodes("resume_user") == [])
    rehydrate_x_cache("empty_x", [], False)
    ok("空 nodes 回灌 no-op", "empty_x" not in xc._x_cache)
    xc._x_cache.pop("elonmusk", None)

    print("\n=== x_post_resources（手动下载，缓存取） ===")
    xc._x_cache["dl_user"] = {"nodes": [mk_tweet("D1", medias=[media_photo("P1"), media_photo("P2")])]}
    rs = xc.x_post_resources("dl_user", "D1")
    ok("取全部资源", [r.index for r in rs] == [1, 2])
    rs = xc.x_post_resources("dl_user", "D1", selected_indices=[2])
    ok("selected_indices 过滤", [r.index for r in rs] == [2])
    try:
        xc.x_post_resources("dl_user", "MISSING")
        ok("缓存未命中 → 404", False)
    except CollectorError as e:
        ok("缓存未命中 → 404", e.kind == "not_found")
    xc._x_cache.pop("dl_user", None)


def main() -> int:
    test_pure()
    asyncio.run(test_collect())
    print(f"\n===== 结果：{_passed} 通过，{_failed} 失败 =====")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

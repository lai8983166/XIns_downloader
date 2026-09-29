"""threads-auto-download 单测：auto 挂钩 + 回灌后重导航不丢数据（fake page 注入，不联网）。

运行：python test_threads_hooks.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from collector import threads_collector as tc
from collector.threads_collector import (
    CollectorError,
    extract_threads_username,
    rehydrate_threads_cache,
    reset_threads_cache,
    threads_cache_nodes,
    threads_node_code,
    touch_threads_cache,
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


# ----------------------------- fake 基础设施 -----------------------------


def mk_thread_node(code: str, username: str = "zuck") -> dict:
    """threads 原始节点：顶层无 code（在 thread_items[0].post.code）。"""
    return {
        "thread_items": [
            {
                "post": {
                    "code": code,
                    "media_type": 1,
                    "user": {"username": username, "full_name": "Z", "profile_pic_url": "pic"},
                    "image_versions2": {
                        "candidates": [{"url": f"https://cdn.test/{code}.jpg", "width": 100, "height": 100}]
                    },
                }
            }
        ]
    }


def media_data(codes, end_cursor=None, has_next=None) -> dict:
    pi = {}
    if end_cursor is not None:
        pi["end_cursor"] = end_cursor
    if has_next is not None:
        pi["has_next_page"] = has_next
    return {"edges": [{"node": mk_thread_node(c)} for c in codes], "page_info": pi}


class FakeResponse:
    def __init__(self, data):
        self._data = data
        self.headers = {"content-type": "application/json"}

    async def text(self) -> str:
        return json.dumps(self._data)


class FakeAPIResponse:
    def __init__(self, data):
        self._data = data

    async def json(self) -> dict:
        return self._data


class FakeContextRequest:
    """page.context.request.post：_fetch_more graphql 重放，按脚本逐页弹出。"""

    def __init__(self, replay_pages):
        self.pages = list(replay_pages)
        self.calls = []

    async def post(self, url, data=None, headers=None):
        self.calls.append((url, data, headers))
        if self.pages:
            return FakeAPIResponse({"data": {"mediaData": self.pages.pop(0)}})
        return FakeAPIResponse({"data": {"mediaData": media_data([], has_next=False)}})


class FakePage:
    """最小 page：goto 触发 request（graphql 模板）+ response（首屏 mediaData）。"""

    def __init__(self, first_screen, replay_pages):
        self._first_screen = first_screen
        self._listeners = {}
        self.url = ""
        self.request = FakeContextRequest(replay_pages)
        self.context = types.SimpleNamespace(request=self.request)

    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        try:
            self._listeners.get(event, []).remove(handler)
        except ValueError:
            pass

    async def goto(self, url, **kwargs):
        self.url = url
        req = types.SimpleNamespace(
            url="https://www.threads.com/graphql/query",
            post_data='variables={"userID":"1","__typename":"BarcelonaProfileThreadsTab"}',
            headers={"x-fb-friendly-name": "BarcelonaProfileThreadsTab"},
        )
        for handler in list(self._listeners.get("request", [])):
            handler(req)
        resp = FakeResponse({"data": {"mediaData": self._first_screen()}})
        for handler in list(self._listeners.get("response", [])):
            await handler(resp)

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
    tc.session = FakeSession(page)
    tc.cooldown = FakeCooldown()
    tc.detect_signal = lambda url=None: None
    tc.settings = types.SimpleNamespace(
        nav_stabilize=(0, 0), scroll_delay=(0, 0), profile_page_size=6
    )


async def run_scenarios() -> None:
    print("=== 回灌 → 重导航：旧帖去重、新帖追加、captured 不清空 ===")
    install_fakes(FakePage(
        lambda: media_data(["N1", "T2", "T1"], end_cursor="c1", has_next=True),  # 首屏：1 新 2 旧
        [media_data(["N2"], has_next=False)],                                    # 重放翻页：1 新，末页
    ))
    rehydrate_threads_cache("zuck", [mk_thread_node("T1"), mk_thread_node("T2")], False)
    entry = tc._threads_cache["zuck"]
    ok("回灌条目 rehydrated=True / navigated=False / first_req 空",
       entry.get("rehydrated") is True and entry.get("navigated") is False
       and entry["first_req"]["post_data"] is None)
    ok("threads_cache_nodes 读取回灌 nodes", [threads_node_code(n) for n in threads_cache_nodes("zuck")] == ["T1", "T2"])

    resp = await tc.preview_threads_profile("zuck", cursor=2, limit=2)
    codes = [p.shortcode for p in resp.posts]
    ok("返回切片为新帖 [N1, N2]（cursor=2）", codes == ["N1", "N2"])
    ok("旧帖未重复（去重生效）", len(threads_cache_nodes("zuck")) == 4)
    ok("回灌 nodes 保留（重导航未清空 captured）",
       [threads_node_code(n) for n in threads_cache_nodes("zuck")] == ["T1", "T2", "N1", "N2"])
    ok("重导航后条目 navigated=True 且 rehydrated 消失",
       tc._threads_cache["zuck"]["navigated"] is True and "rehydrated" not in tc._threads_cache["zuck"])
    ok("graphql 重放发生且推进 after=c1",
       tc._threads_cache["zuck"]["first_req"]["post_data"] and len(tc.session._page.request.calls) == 1
       and json.loads(parse_qs(tc.session._page.request.calls[0][1])["variables"][0]).get("after") == "c1")
    ok("用户信息来自帖子节点", resp.username == "zuck" and resp.profile_pic_url == "pic")
    ok("翻尽 → has_more=False", resp.has_more is False and resp.next_cursor is None)

    print("\n=== 无回灌的降级重导航：captured 清空（原语义保持） ===")
    install_fakes(FakePage(
        lambda: media_data(["N1"], end_cursor="c9", has_next=True),
        [media_data(["N2"], has_next=False)],
    ))
    # 模拟上次会话遗留的常规缓存（navigated=True、非 rehydrated、page 已失活）
    tc._threads_cache["stale"] = {
        "nodes": [mk_thread_node("T1"), mk_thread_node("T2")],
        "end_cursor": "c0", "exhausted": False, "fetched_at": time.time(),
        "user_info": {}, "navigated": True,
        "first_req": {"post_data": 'variables={"userID":"1"}', "headers": {"x": "y"}},
    }
    resp2 = await tc.preview_threads_profile("stale", cursor=0, limit=3)  # need=3 > 缓存 2 → 强制翻页
    ok("降级重导航后旧 nodes 被清空",
       [threads_node_code(n) for n in threads_cache_nodes("stale")] == ["N1", "N2"])
    ok("返回首屏两帖（翻尽）", [p.shortcode for p in resp2.posts] == ["N1", "N2"] and resp2.has_more is False)

    print("\n=== 挂钩语义 ===")
    before = tc._threads_cache["zuck"]["fetched_at"]
    time.sleep(0.02)
    touch_threads_cache("zuck")
    ok("touch 刷新 TTL", tc._threads_cache["zuck"]["fetched_at"] > before)
    reset_threads_cache("zuck")
    ok("reset 丢弃缓存", threads_cache_nodes("zuck") == [])
    rehydrate_threads_cache("empty", [], False)
    ok("空 nodes 回灌 no-op", "empty" not in tc._threads_cache)
    tc._threads_cache.pop("stale", None)

    print("\n=== 零捕获 not_found → 废弃持久 page（e2e 修复：坏 SPA 页不再复用） ===")
    install_fakes(FakePage(
        lambda: media_data([], end_cursor=None, has_next=True),  # 首屏空（SPA 卡死不发 graphql）
        [],
    ))
    try:
        await tc.preview_threads_profile("stuck", cursor=0, limit=6)
        ok("零捕获 → not_found", False)
    except CollectorError as e:
        ok("零捕获 → not_found", e.kind == "not_found")
    ok("持久 page 被废弃（下次重试用全新页面）", tc.session.closed == ["threads:stuck"])

    print("\n=== extract_threads_username 校验 ===")
    ok("@user / URL 解析", extract_threads_username("@zuck") == "zuck"
       and extract_threads_username("https://www.threads.net/@zuck") == "zuck")
    for bad in ("https://www.threads.com/", "post", ""):
        try:
            extract_threads_username(bad)
            ok(f"非法输入被拒：{bad!r}", False)
        except CollectorError as e:
            ok(f"非法输入被拒：{bad!r}", e.kind == "invalid_url")


def main() -> int:
    asyncio.run(run_scenarios())
    print(f"\n===== 结果：{_passed} 通过，{_failed} 失败 =====")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

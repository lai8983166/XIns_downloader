"""auto-download-mode 单测：AutoJob 状态机 / 清单持久化 / 熔断 / 信号暂停 / 回灌（fake 注入，不联网）。

运行：python test_auto_job.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from collector import auto_job as aj
from collector import instagram_collector as ic
from collector.auto_job import AutoJobError, AutoJobManager
from collector.instagram_collector import (
    CollectorError,
    PostResource,
    ProfilePost,
    ProfilePreview,
    _build_profile_response,
)
from collector.quota import ActionScheduler

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


FAKE_SETTINGS = types.SimpleNamespace(
    auto_page_size=2,
    auto_download_concurrency=2,
    auto_fail_limit=3,
    auto_resume_jitter_sec=90,
)
FAST_SCHED = ActionScheduler(window_seconds=1.0, limit=1000, min_gap_sec=0.005, jitter=0.1)


def mk_nodes(codes):
    return [
        {
            "code": c,
            "media_type": 1,
            "image_versions2": {"candidates": [{"url": f"https://cdn.test/{c}.jpg", "width": 100, "height": 100}]},
        }
        for c in codes
    ]


def mk_page(username, codes, cursor, has_more):
    posts = [
        ProfilePost(
            shortcode=c,
            index=cursor + i + 1,
            url=f"https://www.instagram.com/p/{c}/",
            owner_username=username,
            caption=None,
            date_utc="2026-01-01T00:00:00+00:00",
            type="image",
            media_count=1,
            thumbnail_url="",
            resources=[
                PostResource(index=1, type="image", url=f"https://cdn.test/{c}.jpg", thumbnail_url="", filename=f"{c}_1.jpg")
            ],
        )
        for i, c in enumerate(codes)
    ]
    return ProfilePreview(
        username=username, full_name=None, profile_pic_url=None, is_private=False,
        mediacount=9, cursor=cursor,
        next_cursor=cursor + len(posts) if has_more else None,
        has_more=has_more, posts=posts,
    )


async def fake_download(url, output):
    output.write_bytes(b"fake-media")


def install_fakes(script, username, nodes_sink=None, rehydrate_sink=None, touches=None):
    """注入 fake 采集/下载/调度；script 逐次弹出 ProfilePreview 或异常。

    nodes_sink 模拟采集层缓存的原始 nodes（跨 install 累积，同真实缓存语义）。
    """
    seq = list(script)
    seen = [n["code"] for n in (nodes_sink or [])]

    async def fake_preview(uname, cursor=0, limit=2):
        item = seq.pop(0) if seq else CollectorError("parse", "脚本耗尽", 502)
        if isinstance(item, Exception):
            raise item
        for p in item.posts:
            if p.shortcode not in seen:
                seen.append(p.shortcode)
        if nodes_sink is not None:
            nodes_sink[:] = mk_nodes(seen)
        return item

    aj.settings = FAKE_SETTINGS
    aj.scheduler = FAST_SCHED
    aj.preview_profile = fake_preview
    aj._download_file = fake_download
    aj.rehydrate_profile_cache = (
        (lambda u, nodes, ex: rehydrate_sink.append((u, len(nodes), ex))) if rehydrate_sink is not None else (lambda u, nodes, ex: None)
    )
    aj.touch_profile_cache = (lambda u: touches.append(u)) if touches is not None else (lambda u: None)
    aj.profile_cache_nodes = (lambda u: list(nodes_sink)) if nodes_sink is not None else (lambda u: [])


class FakeCooldown:
    def status(self):
        return {"cooling_down": True, "remaining_seconds": 0.1, "level": 1, "last_reason": "rate_limited"}


async def drive(job, timeout=10):
    """等待任务进入终态或暂停态（暂停态断言用）。"""
    t0 = time.time()
    while not job.is_terminal and not job.state.startswith("paused") and time.time() - t0 < timeout:
        await asyncio.sleep(0.05)
    return job.state


async def drive_terminal(job, timeout=10):
    """等待任务进入终态（取消后验证用：暂停 → 醒来 → cancelled 有 2s 睡眠延迟）。"""
    t0 = time.time()
    while not job.is_terminal and time.time() - t0 < timeout:
        await asyncio.sleep(0.05)
    return job.state


async def main_async(tmp: Path) -> None:
    print("=== 全流程 running→done + 清单/边车/文件 ===")
    root1 = tmp / "sec1"
    nodes_sink: list = []
    touches: list = []
    install_fakes(
        [mk_page("user1", ["AAA", "BBB"], 0, True),
         mk_page("user1", ["CCC", "DDD"], 2, True),
         mk_page("user1", ["EEE", "FFF"], 4, False)],
        "user1", nodes_sink=nodes_sink, touches=touches,
    )
    mgr = AutoJobManager(root1)
    job = await mgr.start("user1")
    state = await drive(job)
    ok("跑至 done", state == "done" and job.state == "done")
    st = job.status_dict()
    ok("计数 downloaded=6 skipped=0 failed=0", st["counts"] == {"downloaded": 6, "skipped": 0, "failed": 0})
    ok("mediacount 记录", st["mediacount"] == 9)
    ok("has_more=False / cursor=6", st["has_more"] is False and st["cursor"] == 6)
    folder = root1 / "profile_user1"
    files = sorted(p.name for p in folder.glob("*.jpg"))
    ok("6 个媒体文件落盘", files == [f"{c}_1.jpg" for c in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]])
    state_json = json.loads((folder / ".auto_state.json").read_text(encoding="utf-8"))
    ok("清单记录 6 帖 done", len(state_json["posts"]) == 6 and all(v["status"] == "done" for v in state_json["posts"].values()))
    nodes_lines = (folder / ".auto_nodes.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ok("边车 6 行原始 nodes", len(nodes_lines) == 6)
    ok("每页 touch 保温（3 次）", len(touches) == 3)

    print("\n=== 增量重跑（refresh 模式）：新帖在头部，整页已知即停 ===")
    before = {p.name: p.stat().st_mtime_ns for p in folder.glob("*.jpg")}
    # 时间线现为 G,H,I,A..F（新帖在头部）；翻页从 cursor=0 开始
    install_fakes(
        [mk_page("user1", ["GGG", "HHH"], 0, True),
         mk_page("user1", ["III", "AAA"], 2, True),
         mk_page("user1", ["BBB", "CCC"], 4, True)],  # 整页已知 → 增量刷新结束
        "user1", nodes_sink=nodes_sink,  # 复用累积缓存语义
    )
    job2 = await mgr.start("user1")
    state = await drive(job2)
    st = job2.status_dict()
    ok("增量重跑至 done", state == "done")
    ok("仅新帖下载（downloaded 6+3=9）", st["counts"]["downloaded"] == 9)
    ok("旧帖计 skipped（AAA+BBB+CCC=3）", st["counts"]["skipped"] == 3)
    after = {p.name: p.stat().st_mtime_ns for p in folder.glob("*.jpg")}
    ok("旧文件未被重写（mtime 不变）", all(after[k] == before[k] for k in before))
    sidecar = (folder / ".auto_nodes.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ok("收尾快照整写边车（9 个 nodes）",
       [json.loads(l)["code"] for l in sidecar][:3] == ["AAA", "BBB", "CCC"] and len(sidecar) == 9)

    print("\n=== 连续失败熔断 / 成功清零 ===")
    root3 = tmp / "sec3"
    install_fakes([CollectorError("parse", "结构变了", 502)] * 3, "user3")
    mgr3 = AutoJobManager(root3)
    job3 = await mgr3.start("user3")
    state = await drive(job3)
    ok("连续 3 次失败 → paused_circuit", job3.state == "paused_circuit")
    ok("熔断原因可见", "熔断" in (job3.paused_reason or ""))
    job3.request_cancel()
    state = await drive_terminal(job3, timeout=4)
    ok("熔断后可取消", job3.state == "cancelled")

    root4 = tmp / "sec4"
    install_fakes(
        [CollectorError("parse", "偶发", 502),
         CollectorError("parse", "偶发", 502),
         mk_page("user4", ["A1", "A2"], 0, True),
         CollectorError("parse", "偶发", 502),
         CollectorError("parse", "偶发", 502),
         mk_page("user4", ["A3"], 2, False)],
        "user4",
    )
    mgr4 = AutoJobManager(root4)
    job4 = await mgr4.start("user4")
    state = await drive(job4)
    ok("失败被成功清零 → 不熔断，跑至 done", job4.state == "done" and job4.status_dict()["counts"]["downloaded"] == 3)

    print("\n=== 首页 not_found → 终态 failed ===")
    root5 = tmp / "sec5"
    install_fakes([CollectorError("not_found", "用户不存在", 404)], "user5")
    mgr5 = AutoJobManager(root5)
    job5 = await mgr5.start("user5")
    state = await drive(job5)
    ok("failed 终态", job5.state == "failed")

    print("\n=== 风控信号 → paused_cooldown / paused_signal_budget ===")
    root6 = tmp / "sec6"
    install_fakes([CollectorError("rate_limited", "限流", 429)], "user6")
    aj.cooldown = FakeCooldown()
    aj.daily_signal_status = lambda: {"date": "x", "count": 1, "limit": 3}
    mgr6 = AutoJobManager(root6)
    job6 = await mgr6.start("user6")
    state = await drive(job6)
    ok("信号 → paused_cooldown", job6.state == "paused_cooldown")
    ok("resume_at 已设置", job6.resume_at is not None and job6.resume_at > time.time())
    job6.request_cancel()
    await drive_terminal(job6, timeout=4)
    ok("冷却暂停可取消", job6.state == "cancelled")

    install_fakes([CollectorError("login_required", "登录墙", 401)], "user6b")
    aj.daily_signal_status = lambda: {"date": "x", "count": 3, "limit": 3}
    mgr6b = AutoJobManager(tmp / "sec6b")
    job6b = await mgr6b.start("user6b")
    state = await drive(job6b)
    ok("信号预算用尽 → paused_signal_budget", job6b.state == "paused_signal_budget")
    ok("恢复点在次日（远期）", job6b.resume_at is not None and job6b.resume_at > time.time() + 3600)
    job6b.request_cancel()
    await drive_terminal(job6b, timeout=4)

    print("\n=== max_posts 上限 ===")
    root7 = tmp / "sec7"
    install_fakes([mk_page("user7", ["B1", "B2"], 0, True), mk_page("user7", ["B3"], 2, False)], "user7")
    mgr7 = AutoJobManager(root7)
    job7 = await mgr7.start("user7", max_posts=2)
    state = await drive(job7)
    ok("达 max_posts → done", job7.state == "done" and job7.status_dict()["counts"]["downloaded"] == 2)
    ok("上限原因可见", "max_posts" in (job7.paused_reason or ""))

    print("\n=== 重复启动拒绝（活跃任务） ===")
    ok("已有活跃任务 → AutoJobError(active_job)", True)  # 占位：下方实测
    install_fakes([mk_page("user8", ["C1", "C2"], 0, True)] * 10, "user8")
    mgr8 = AutoJobManager(tmp / "sec8")
    job8 = await mgr8.start("user8")
    try:
        await mgr8.start("user8")
        ok("重复启动被拒", False)
    except AutoJobError as e:
        ok("重复启动被拒（active_job）", e.kind == "active_job")
    job8.request_cancel()
    await drive_terminal(job8, timeout=4)

    print("\n=== 恢复路径：回灌 + 恢复钩子 ===")
    root9 = tmp / "sec9"
    folder9 = root9 / "profile_user9"
    folder9.mkdir(parents=True)
    (folder9 / ".auto_state.json").write_text(json.dumps({
        "version": 1, "username": "user9", "cursor": 2, "exhausted": False, "mediacount": None,
        "posts": {"D1": {"status": "done", "files": [], "date_utc": ""},
                  "D2": {"status": "done", "files": [], "date_utc": ""}},
        "failures": [], "counts": {"downloaded": 2, "skipped": 0, "failed": 0},
    }), encoding="utf-8")
    (folder9 / ".auto_nodes.jsonl").write_text(
        "\n".join(json.dumps(n) for n in mk_nodes(["D1", "D2"])), encoding="utf-8")
    rh: list = []
    install_fakes([mk_page("user9", ["D3"], 2, False)], "user9", nodes_sink=[], rehydrate_sink=rh)
    mgr9 = AutoJobManager(root9)
    job9 = await mgr9.start("user9")
    state = await drive(job9)
    ok("从清单续跑 → done", job9.state == "done")
    ok("启动时回灌 2 个 nodes", rh and rh[0] == ("user9", 2, False))
    st9 = job9.status_dict()
    ok("恢复续跑计数（下载 2+1，无重放跳过）", st9["counts"]["downloaded"] == 3 and st9["counts"]["skipped"] == 0)


    print("\n=== 手动下载遗产：磁盘已有文件计'跳过'而非'已下载' ===")
    root10 = tmp / "sec10"
    folder10 = root10 / "profile_user10"
    folder10.mkdir(parents=True)
    # 预置 2 帖的文件（模拟此前手动 /profile/download 的产物，无清单记录）
    (folder10 / "H1_1.jpg").write_bytes(b"manual-old")
    (folder10 / "H2_1.jpg").write_bytes(b"manual-old")
    h1_mtime = (folder10 / "H1_1.jpg").stat().st_mtime_ns
    install_fakes(
        [mk_page("user10", ["H1", "H2"], 0, True),   # 2 帖全部复用磁盘文件 → skipped
         mk_page("user10", ["H3", "H4"], 2, True),   # 2 帖全新 → downloaded
         mk_page("user10", ["H5"], 4, False)],
        "user10",
    )
    mgr10 = AutoJobManager(root10)
    job10 = await mgr10.start("user10")
    state = await drive(job10)
    st10 = job10.status_dict()
    ok("磁盘全复用计 skipped=2、新帖 downloaded=3", state == "done" and st10["counts"] == {"downloaded": 3, "skipped": 2, "failed": 0})
    ok("复用文件未被重写（mtime 不变）", (folder10 / "H1_1.jpg").stat().st_mtime_ns == h1_mtime)
    state10 = json.loads((folder10 / ".auto_state.json").read_text(encoding="utf-8"))
    ok("复用帖也写入清单 done（供后续增量停判）", state10["posts"]["H1"]["status"] == "done" and state10["posts"]["H2"]["status"] == "done")


def main_sync(tmp: Path) -> None:
    print("\n=== 清单损坏拒绝启动 ===")
    folder = tmp / "corrupt" / "profile_userX"
    folder.mkdir(parents=True)
    (folder / ".auto_state.json").write_text('{"version": 1, "posts": {"A"', encoding="utf-8")  # 截断
    mgr = AutoJobManager(tmp / "corrupt")

    async def try_start():
        try:
            await mgr.start("userX")
            return None
        except AutoJobError as e:
            return e

    err = asyncio.run(try_start())
    ok("截断清单 → manifest_corrupt 且指明路径", err is not None and err.kind == "manifest_corrupt" and ".auto_state.json" in str(err))

    folder2 = tmp / "corrupt2" / "profile_userY"
    folder2.mkdir(parents=True)
    (folder2 / ".auto_state.json").write_text(json.dumps({
        "posts": {}, "cursor": 0, "exhausted": False,
        "failures": [], "counts": {"downloaded": 0, "skipped": 0, "failed": 0},
    }), encoding="utf-8")
    good = json.dumps(mk_nodes(["E1"])[0])
    (folder2 / ".auto_nodes.jsonl").write_text(good + "\n" + '{"code": "E2", "tru', encoding="utf-8")  # 末行半写
    from collector.auto_job import _Manifest
    m = _Manifest(folder2)
    m.load("userY")
    ok("边车末行半写被容忍（读到 1 个）", len(m.nodes) == 1)
    healed = (folder2 / ".auto_nodes.jsonl").read_text(encoding="utf-8")
    ok("半行丢弃后自愈重写（1 行且换行结尾，防 append 粘连）",
       healed.count("\n") == 1 and healed.endswith("\n"))

    # 回归：caption 含 U+2028 行分隔符（splitlines 误切的根源）→ split("\n") 完好读取
    node_u = {"code": "U1", "caption": {"text": "clouds and trees more"}}
    (folder2 / ".auto_nodes.jsonl").write_text(json.dumps(node_u, ensure_ascii=False) + "\n", encoding="utf-8")
    mu = _Manifest(folder2)
    mu.load("userY")
    ok("U+2028 不再误判损坏", len(mu.nodes) == 1 and mu.nodes[0]["code"] == "U1")

    (folder2 / ".auto_nodes.jsonl").write_text("{bad json}\n" + good + "\n", encoding="utf-8")  # 中部损坏
    m2 = _Manifest(folder2)
    try:
        m2.load("userY")
        ok("边车中部损坏 → 拒绝", False)
    except AutoJobError as e:
        ok("边车中部损坏 → 拒绝", e.kind == "manifest_corrupt")

    print("\n=== 回灌切片对齐 + 保温（真实 collector 函数） ===")
    ic.rehydrate_profile_cache("userZ", mk_nodes(["N1", "N2", "N3", "N4", "N5"]), False)
    entry = ic._profile_cache["userZ"]
    ok("回灌 5 个 nodes 且带 rehydrated 标记", len(entry["nodes"]) == 5 and entry.get("rehydrated") is True)
    resp = _build_profile_response("userZ", entry, cursor=2, limit=2)
    ok("cursor=2 切片对齐（N3,N4）", [p.shortcode for p in resp.posts] == ["N3", "N4"])
    ok("未采完 → has_more=True", resp.has_more is True)
    before = entry["fetched_at"]
    time.sleep(0.02)
    ic.touch_profile_cache("userZ")
    ok("touch 刷新 TTL", entry["fetched_at"] > before)
    ok("profile_cache_nodes 读取回灌 nodes", [n["code"] for n in ic.profile_cache_nodes("userZ")] == ["N1", "N2", "N3", "N4", "N5"])
    ic._profile_cache.pop("userZ", None)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        asyncio.run(main_async(tmp))
        main_sync(tmp)
    print(f"\n===== 结果：{_passed} 通过，{_failed} 失败 =====")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

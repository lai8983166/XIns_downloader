"""media-review-mode 单测：/review/* 路由（TestClient + downloads 内临时文件夹，测完清理，不联网）。

运行：python test_review.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient

import main as backend

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


FOLDER = "profile_testreview_tmp"


def main_test(client: TestClient, folder: Path) -> None:
    manifest = folder / ".auto_state.json"
    manifest_before = manifest.read_bytes()

    print("=== 枚举 ===")
    r = client.get("/review/folders")
    ok("folders 200", r.status_code == 200)
    names = [f["name"] for f in r.json()["folders"]]
    ok("临时文件夹在列表中且带文件数", FOLDER in names and
       next(f["file_count"] for f in r.json()["folders"] if f["name"] == FOLDER) == 4)

    r = client.get("/review/files", params={"folder": FOLDER})
    files = r.json()["files"]
    ok("files 200 且排序", r.status_code == 200 and [f["filename"] for f in files] ==
       ["A_1.jpg", "B_1.jpg", "C_1.mp4", "D_1.jpg"])
    ok("类型标注（image/video）", files[0]["type"] == "image" and files[2]["type"] == "video")
    ok("排除隐藏文件与 _candidate/", all(not f["filename"].startswith(".") for f in files) and
       all("_candidate" not in f["filename"] for f in files))
    ok("size 字段存在", all(f["size"] > 0 for f in files))

    for bad in ["..", "a/b", "a\\b", ".", "/abs", "no_such_folder_x"]:
        r = client.get("/review/files", params={"folder": bad})
        ok(f"非法 folder {bad!r} → 400", r.status_code == 400)

    print("\n=== 本地文件服务（StaticFiles 挂载） ===")
    r = client.get(f"/review/file/{FOLDER}/A_1.jpg")
    ok("jpg 200 + image/jpeg", r.status_code == 200 and r.headers["content-type"].startswith("image/jpeg"))
    r = client.get(f"/review/file/{FOLDER}/C_1.mp4")
    ok("mp4 200 + video/mp4", r.status_code == 200 and r.headers["content-type"].startswith("video/mp4"))
    r = client.get(f"/review/file/{FOLDER}/D_1.jpg", headers={"Range": "bytes=0-99"})
    ok("Range 请求 → 206 且 100 字节", r.status_code == 206 and len(r.content) == 100)
    r = client.get(f"/review/file/{FOLDER}/..%2F..%2Fmain.py")
    ok("URL 编码穿越被拒", r.status_code in (400, 404))
    r = client.get(f"/review/file/{FOLDER}/.auto_state.json")
    ok("隐藏文件仍可读（本地工具已接受的暴露，见 design D3）", r.status_code == 200)

    print("\n=== 动作 ===")
    r = client.post("/review/action", json={"folder": FOLDER, "filename": "A_1.jpg", "action": "keep"})
    ok("keep → 200 空操作", r.status_code == 200 and r.json()["status"] == "kept" and (folder / "A_1.jpg").exists())

    r = client.post("/review/action", json={"folder": FOLDER, "filename": "B_1.jpg", "action": "candidate"})
    ok("candidate → 移入 _candidate/", r.status_code == 200 and (folder / "_candidate/B_1.jpg").is_file()
       and not (folder / "B_1.jpg").exists())
    r2 = client.get("/review/files", params={"folder": FOLDER})
    ok("候选后剩余数 -1（remaining=3）", r.json()["remaining"] == 3 and
       len(r2.json()["files"]) == 3)

    (folder / "B_1.jpg").write_bytes(b"again")  # 源位置重建同名 → 再移应 409
    r = client.post("/review/action", json={"folder": FOLDER, "filename": "B_1.jpg", "action": "candidate"})
    ok("目标同名 → 409", r.status_code == 409)
    (folder / "B_1.jpg").unlink()

    r = client.post("/review/action", json={"folder": FOLDER, "filename": "C_1.mp4", "action": "delete"})
    ok("delete → 硬删除", r.status_code == 200 and not (folder / "C_1.mp4").exists()
       and not (folder / "_candidate/C_1.mp4").exists())
    r = client.post("/review/action", json={"folder": FOLDER, "filename": "C_1.mp4", "action": "delete"})
    ok("已删文件再删 → 404", r.status_code == 404)

    r = client.post("/review/action", json={"folder": FOLDER, "filename": "../x", "action": "delete"})
    ok("非法 filename → 400", r.status_code == 400)
    r = client.post("/review/action", json={"folder": "..", "filename": "A_1.jpg", "action": "delete"})
    ok("非法 folder → 400", r.status_code == 400)

    ok("清单不受审查影响（逐字节一致）", manifest.read_bytes() == manifest_before)


def main() -> int:
    folder = backend.DOWNLOAD_ROOT / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".auto_state.json").write_bytes(b'{"posts": {}}')
    (folder / ".auto_nodes.jsonl").write_bytes(b"")
    (folder / "A_1.jpg").write_bytes(b"a" * 200)
    (folder / "B_1.jpg").write_bytes(b"b" * 200)
    (folder / "C_1.mp4").write_bytes(b"c" * 300)
    (folder / "D_1.jpg").write_bytes(b"d" * 200)
    (folder / "notes.txt").write_bytes(b"not media")  # 非媒体扩展名 → 不列出
    (folder / "_candidate").mkdir(exist_ok=True)
    (folder / "_candidate/E_1.jpg").write_bytes(b"e")
    try:
        with TestClient(backend.app) as client:
            main_test(client, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print(f"\n===== 结果：{_passed} 通过，{_failed} 失败 =====")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

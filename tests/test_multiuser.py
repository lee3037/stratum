"""多人同时使用：并发修改、任务上限、磁盘空间保护、健康检查。"""
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STRATUM_WORKERS", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app import fastgpt, server, storage  # noqa: E402
from app.server import app  # noqa: E402

KB = "共享库"
SAMPLE = (ROOT / "samples" / "text_cn.pdf").read_bytes()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATUM_DATA_DIR", str(tmp_path / "lib"))
    with TestClient(app) as c:
        c.post("/api/kbs", json={"name": KB})
        r = c.post(f"/api/kbs/{KB}/files", files=[("files", ("a.pdf", SAMPLE, "application/pdf"))])
        assert r.json()["results"][0]["status"] == "added"
        yield c


def run_together(fn, n):
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = fn(i)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_concurrent_edits_of_same_version_only_one_wins(client):
    saved = client.post(f"/api/kbs/{KB}/versions", json={"pdf_path": "a.pdf", "markdown": "原文"}).json()
    etag = client.get(f"/api/kbs/{KB}/markdown", params={"path": saved["path"]}).json()["etag"]
    codes = run_together(lambda i: client.put(f"/api/kbs/{KB}/markdown", json={
        "path": saved["path"], "markdown": f"修改 {i}", "etag": etag}).status_code, 8)
    assert sorted(codes) == [200] + [409] * 7


def test_concurrent_settings_and_binding_do_not_overwrite_each_other(client):
    kb = storage.kb_dir(KB)

    def work(i):
        if i % 2:
            storage.write_settings(kb, {"chunk": {"max_chars": 1000 + i}, "convert": {}})
        else:
            fastgpt.set_binding(kb, "ds1", "安全规程")
    run_together(work, 10)
    settings = storage.read_settings(kb)
    assert settings["fastgpt"]["dataset_id"] == "ds1"
    assert settings["chunk"]["max_chars"] in {1001, 1003, 1005, 1007, 1009}


def test_too_many_jobs_returns_429(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_JOBS", 1)
    monkeypatch.setitem(server._jobs, "busy", {"id": "busy", "status": "running", "updated": 0})
    r = client.post(f"/api/kbs/{KB}/jobs", json={"path": "a.pdf"})
    assert r.status_code == 429 and "稍后再试" in r.json()["detail"]
    health = client.get("/api/health").json()
    assert health["active_jobs"] == 1 and health["max_jobs"] == 1


def test_low_disk_space_blocks_writes(client, monkeypatch):
    monkeypatch.setenv("STRATUM_MIN_FREE_MB", str(1 << 40))
    r = client.post(f"/api/kbs/{KB}/files", files=[("files", ("b.pdf", SAMPLE, "application/pdf"))])
    result = r.json()["results"][0]
    assert result["status"] == "error" and "磁盘" in result["message"]
    r = client.post(f"/api/kbs/{KB}/versions", json={"pdf_path": "a.pdf", "markdown": "x"})
    assert r.status_code == 507
    r = client.post(f"/api/kbs/{KB}/exports", json={"mode": "latest"})
    assert r.status_code == 507
    storage_status = client.get("/api/health").json()["storage"]
    assert storage_status["low_space"] is True and storage_status["free_mb"] > 0
    # 只读操作不受影响
    assert client.get(f"/api/kbs/{KB}/files").status_code == 200


def test_health_reports_pool_and_batches(client):
    health = client.get("/api/health").json()
    assert health["pool"]["slots"] == server.WORKERS
    assert set(health["pool"]["pending"]) == {"interactive", "batch"}
    assert health["batches"]["concurrency"] == server.BATCH_CONCURRENCY


def test_single_parse_and_batch_share_workers(client):
    """真实解析：单个解析与批量解析同时进行时都能完成。"""
    import time
    for name in ("b.pdf", "c.pdf"):
        client.post(f"/api/kbs/{KB}/files", files=[("files", (name, SAMPLE, "application/pdf"))])
    batch = client.post(f"/api/kbs/{KB}/batches", json={"paths": ["a.pdf", "b.pdf"], "skip_converted": False}).json()
    # 正在批量解析的文件不能再单独解析；单个解析另一个文件
    assert client.post(f"/api/kbs/{KB}/jobs", json={"path": "a.pdf"}).status_code == 409
    job = client.post(f"/api/kbs/{KB}/jobs", json={"path": "c.pdf"}).json()
    end = time.time() + 180
    while time.time() < end:
        j = client.get(f"/api/jobs/{job['id']}").json()
        b = client.get(f"/api/kbs/{KB}/batches/{batch['id']}").json()
        if j["status"] in ("done", "error") and b["status"] != "running":
            break
        time.sleep(0.3)
    assert j["status"] == "done", j
    assert b["counts"]["done"] == 2, b

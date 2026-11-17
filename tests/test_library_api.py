import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STRATUM_WORKERS", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app import parsers  # noqa: E402
from app.server import app  # noqa: E402

KB = "安全规程库"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATUM_DATA_DIR", str(tmp_path / "lib"))
    with TestClient(app) as c:
        assert c.post("/api/kbs", json={"name": KB}).status_code == 200
        yield c


def wait_done(client, job_id, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.3)
    raise TimeoutError(job_id)


def upload(client, *items):
    files = [("files", (Path(rel).name, data, "application/pdf")) for rel, data in items]
    data = {"paths": [rel for rel, _ in items]}
    r = client.post(f"/api/kbs/{KB}/files", files=files, data=data)
    assert r.status_code == 200, r.text
    return r.json()["results"]


def test_kb_management(client):
    kbs = client.get("/api/kbs").json()
    assert [k["name"] for k in kbs["kbs"]] == [KB] and kbs["storage"]["writable"]
    assert client.post("/api/kbs", json={"name": KB}).status_code == 409
    assert client.post("/api/kbs", json={"name": "../x"}).status_code == 400
    assert client.get("/api/kbs/不存在/files").status_code == 404
    assert client.get("/api/health").json()["storage"]["writable"]

    settings = client.get(f"/api/kbs/{KB}/settings").json()
    assert settings["chunk"]["separator"] == "===CHUNK==="
    put = client.put(f"/api/kbs/{KB}/settings", json={"chunk": {"separator": "@@@", "max_chars": 1200}})
    assert put.status_code == 200 and put.json()["chunk"]["max_chars"] == 1200
    assert client.put(f"/api/kbs/{KB}/settings", json={"convert": {"parser": "nope"}}).status_code == 400


def test_upload_parse_save_and_edit(client):
    pdf = (ROOT / "samples" / "complex_cn.pdf").read_bytes()
    results = upload(client, ("预算/预算.pdf", pdf), ("预算/预算.pdf", pdf), ("坏文件.pdf", b"hello"))
    assert [r["status"] for r in results] == ["added", "skipped", "error"]

    files = client.get(f"/api/kbs/{KB}/files").json()
    assert [f["path"] for f in files["files"]] == ["预算/预算.pdf"] and files["artifacts"] == []
    r = client.get(f"/api/kbs/{KB}/pdf", params={"path": "预算/预算.pdf"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"

    created = client.post(f"/api/kbs/{KB}/jobs", json={"path": "预算/预算.pdf", "options": {"max_chars": 800}})
    assert created.status_code == 200, created.text
    job = wait_done(client, created.json()["id"])
    assert job["status"] == "done" and job["kb"] == KB and job["path"] == "预算/预算.pdf"

    saved = client.post(f"/api/kbs/{KB}/versions", json={
        "pdf_path": "预算/预算.pdf", "markdown": job["result"]["markdown"], "job_id": job["id"]}).json()
    assert saved["path"] == "预算/预算_v1.md" and saved["version"] == 1
    second = client.post(f"/api/kbs/{KB}/versions", json={"pdf_path": "预算/预算.pdf", "markdown": "v2"}).json()
    assert second["version"] == 2

    read = client.get(f"/api/kbs/{KB}/markdown", params={"path": saved["path"]}).json()
    assert read["markdown"] == job["result"]["markdown"]
    assert read["meta"]["origin"] == "manual" and read["meta"]["doc_stats"]["pages"] >= 1
    assert read["meta"]["chunk"]["max_chars"] == 800

    edit = client.put(f"/api/kbs/{KB}/markdown", json={"path": saved["path"], "markdown": "edited", "etag": read["etag"]})
    assert edit.status_code == 200
    stale = client.put(f"/api/kbs/{KB}/markdown", json={"path": saved["path"], "markdown": "late", "etag": read["etag"]})
    assert stale.status_code == 409

    files = client.get(f"/api/kbs/{KB}/files").json()
    assert files["files"][0]["converted"]
    assert [a["version"] for a in files["artifacts"]] == [1, 2]


@pytest.mark.parametrize("path", ["../x.pdf", "/etc/passwd", ".stratum/settings.json", "a/../../b.pdf"])
def test_path_traversal_rejected(client, path):
    assert client.get(f"/api/kbs/{KB}/pdf", params={"path": path}).status_code == 400
    assert client.get(f"/api/kbs/{KB}/markdown", params={"path": path}).status_code == 400
    assert client.post(f"/api/kbs/{KB}/jobs", json={"path": path}).status_code == 400


def test_missing_files_and_unknown_parser(client):
    assert client.get(f"/api/kbs/{KB}/pdf", params={"path": "nope.pdf"}).status_code == 404
    assert client.post(f"/api/kbs/{KB}/versions", json={"pdf_path": "nope.pdf", "markdown": "x"}).status_code == 404
    assert [p["id"] for p in client.get("/api/parsers").json()["parsers"]] == ["pymupdf"]
    pdf = (ROOT / "samples" / "text_cn.pdf").read_bytes()
    upload(client, ("a.pdf", pdf))
    r = client.post(f"/api/kbs/{KB}/jobs", json={"path": "a.pdf", "options": {"parser": "oxr"}})
    assert r.status_code == 400 and "未知解析后端" in r.json()["detail"]


def wait_batch(client, batch_id, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        batch = client.get(f"/api/kbs/{KB}/batches/{batch_id}").json()
        if batch["status"] != "running":
            return batch
        time.sleep(0.5)
    raise TimeoutError(batch_id)


def test_batch_parse_and_zip_download(client):
    upload(client, ("规程/text_cn.pdf", (ROOT / "samples" / "text_cn.pdf").read_bytes()),
           ("预算.pdf", (ROOT / "samples" / "complex_cn.pdf").read_bytes()))
    client.put(f"/api/kbs/{KB}/settings", json={"chunk": {"separator": "@@@"}})
    r = client.post(f"/api/kbs/{KB}/batches", json={"paths": ["规程/text_cn.pdf", "预算.pdf"]})
    assert r.status_code == 200, r.text
    batch_id = r.json()["id"]
    assert client.get(f"/api/kbs/{KB}/files").json()["active"]  # 列表能看到排队/解析中的文件
    batch = wait_batch(client, batch_id)
    assert batch["status"] == "done" and batch["counts"]["done"] == 2, batch
    files = client.get(f"/api/kbs/{KB}/files").json()
    assert all(f["converted"] for f in files["files"]) and files["active"] == {}
    md = client.get(f"/api/kbs/{KB}/markdown", params={"path": "预算_v1.md"}).json()
    assert "@@@" in md["markdown"] and md["meta"]["origin"] == "batch"  # 批量使用知识库默认参数
    assert [b["id"] for b in client.get(f"/api/kbs/{KB}/batches").json()["batches"]] == [batch_id]

    # 已转换的文件默认跳过
    again = client.post(f"/api/kbs/{KB}/batches", json={"paths": ["预算.pdf"]}).json()
    assert wait_batch(client, again["id"])["counts"]["skipped"] == 1

    export = client.post(f"/api/kbs/{KB}/exports", json={"mode": "fastgpt", "batch_id": batch_id})
    assert export.status_code == 200, export.text
    info = export.json()
    assert info["markdown"] == 2 and info["name"].endswith(".zip")
    dl = client.get(f"/api/kbs/{KB}/exports/{info['id']}")
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert set(zf.namelist()) == {"规程/text_cn.md", "预算.md", "汇总.csv"}
    assert client.get(f"/api/kbs/{KB}/exports/{info['id']}").status_code == 404  # 一次性下载

    empty = client.post(f"/api/kbs/{KB}/exports", json={"paths": ["坏文件.pdf"]})
    assert empty.status_code == 400
    assert client.post(f"/api/kbs/{KB}/batches", json={"paths": []}).status_code == 400
    assert client.get(f"/api/kbs/{KB}/batches/nope").status_code == 404


def test_kb_job_joins_running_and_refuses_batch_files(client, monkeypatch):
    from app import server

    pdf = (ROOT / "samples" / "text_cn.pdf").read_bytes()
    upload(client, ("a.pdf", pdf))
    # 同一文件、同样参数的单个解析正在进行：返回那个任务，不重复解析
    opts = {"max_chars": 800}
    fake = {"id": "running1", "kb": KB, "path": "a.pdf", "status": "running", "convert_options": opts,
            "page_count": 3, "updated": time.time()}
    monkeypatch.setitem(server._jobs, "running1", fake)
    submitted = []
    monkeypatch.setattr(server, "_submit", lambda *a, **k: submitted.append(a) or "new1")
    r = client.post(f"/api/kbs/{KB}/jobs", json={"path": "a.pdf", "options": opts}).json()
    assert r == {"id": "running1", "page_count": 3, "joined": True} and not submitted
    # 参数不同：新建任务
    r = client.post(f"/api/kbs/{KB}/jobs", json={"path": "a.pdf", "options": {"max_chars": 900}}).json()
    assert r["id"] == "new1" and len(submitted) == 1
    # 正在批量解析：拒绝
    monkeypatch.setattr(server.batches, "active_paths", lambda kb: {"a.pdf": "queued"})
    r = client.post(f"/api/kbs/{KB}/jobs", json={"path": "a.pdf", "options": opts})
    assert r.status_code == 409 and "批量解析" in r.json()["detail"]


def test_image_endpoint_and_public_base(client, tmp_path, monkeypatch):
    from app import images

    monkeypatch.delenv("STRATUM_PUBLIC_URL", raising=False)
    monkeypatch.setattr(images, "_seen_base", "")
    name = "b" * 40 + ".png"
    path = images.path_for(name)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x89PNG fake")
    r = client.get(f"/api/images/{name}")
    assert r.status_code == 200 and r.content == b"\x89PNG fake"
    assert r.headers["content-type"] == "image/png" and "immutable" in r.headers["cache-control"]
    for bad in ("../settings.json", "b" * 40 + ".svg", "x.png"):
        assert client.get(f"/api/images/{bad}").status_code == 404
    # 图片目录不是知识库
    assert [k["name"] for k in client.get("/api/kbs").json()["kbs"]] == [KB]
    # 用户访问 Stratum 的地址用来生成图片链接
    client.get("/api/kbs", headers={"host": "192.162.10.41:18080"})
    assert images.public_base() == "http://192.162.10.41:18080"
    monkeypatch.setenv("STRATUM_PUBLIC_URL", "http://stratum.example:8000/")
    assert images.url_for(name) == f"http://stratum.example:8000/api/images/{name}"
    monkeypatch.setattr(images, "_seen_base", "")


def test_delete_skips_files_being_parsed(client, monkeypatch):
    from app import server

    pdf = (ROOT / "samples" / "text_cn.pdf").read_bytes()
    upload(client, ("a.pdf", pdf), ("b.pdf", pdf))
    monkeypatch.setattr(server.batches, "active_paths", lambda kb: {"a.pdf": "running"})
    r = client.post(f"/api/kbs/{KB}/files/delete", json={"paths": ["a.pdf", "b.pdf"]}).json()["results"]
    assert [(x["path"], x["status"]) for x in r] == [("a.pdf", "skipped"), ("b.pdf", "deleted")]
    assert [f["path"] for f in client.get(f"/api/kbs/{KB}/files").json()["files"]] == ["a.pdf"]

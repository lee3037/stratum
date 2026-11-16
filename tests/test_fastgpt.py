import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STRATUM_WORKERS", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app import fastgpt, server, storage  # noqa: E402
from app.server import app  # noqa: E402

KB = "规程库"
PDF = b"%PDF-1.4\n%fake\n"


class FakeFastGPT:
    """内存中的 FastGPT：只实现 Stratum 用到的接口，并记录收到的请求。"""

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.fail_create = False
        self.fail_delete = False
        self.next_id = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.requests.append((request.method, path, body))
        if request.headers.get("authorization") != "Bearer secret":
            return httpx.Response(401, json={"code": 401, "message": "unauthorized"})
        ok = lambda data: httpx.Response(200, json={"code": 200, "statusText": "", "message": "", "data": data})  # noqa: E731
        if path == "/api/core/dataset/listV2":
            if body.get("parentId") is None:
                return ok({"list": [{"_id": "ds1", "name": "安全规程", "type": "dataset", "vectorModel": {"name": "bge-m3"}},
                                    {"_id": "f1", "name": "部门", "type": "folder"}], "total": 2})
            return ok({"list": [{"_id": "ds2", "name": "设备手册", "type": "dataset"}], "total": 1})
        if path == "/api/core/dataset/detail":
            names = {"ds1": "安全规程", "ds2": "设备手册"}
            if request.url.params.get("id") not in names:
                return httpx.Response(500, json={"code": 500, "message": "Dataset not found"})
            return ok({"_id": request.url.params["id"], "name": names[request.url.params["id"]]})
        if path == "/api/core/dataset/collection/listV2":
            items = [c for c in self.collections.values()
                     if c["datasetId"] == body["datasetId"] and body.get("searchText", "") in c["name"]]
            return ok({"list": [{"_id": c["_id"], "name": c["name"], "type": "virtual"} for c in items], "total": len(items)})
        if path == "/api/core/dataset/collection/create/text":
            if self.fail_create:
                return httpx.Response(500, json={"code": 500, "message": "embedding model unavailable"})
            self.next_id += 1
            cid = f"c{self.next_id}"
            chunks = [c for c in body["text"].split(body["chunkSplitter"]) if c.strip()]
            self.collections[cid] = {"_id": cid, **body}
            return ok({"collectionId": cid, "results": {"insertLen": len(chunks)}})
        if path == "/api/core/dataset/collection/delete":
            if self.fail_delete:
                return httpx.Response(500, json={"code": 500, "message": "busy"})
            for cid in body.get("collectionIds", []):
                self.collections.pop(cid, None)
            return ok(None)
        return httpx.Response(404, json={"code": 404, "message": "not found"})


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATUM_DATA_DIR", str(tmp_path / "lib"))
    monkeypatch.setenv("STRATUM_FASTGPT_URL", "http://fastgpt.local:3000/")
    monkeypatch.setenv("STRATUM_FASTGPT_API_KEY", "secret")
    fake = FakeFastGPT()
    monkeypatch.setattr(server.fastgpt_client, "_transport", httpx.MockTransport(fake))
    return fake


@pytest.fixture()
def client(fake):
    with TestClient(app) as c:
        c.post("/api/kbs", json={"name": KB})
        kb = storage.kb_dir(KB)
        for rel in ("a.pdf", "sub/b.pdf", "c.pdf"):
            (kb / rel).parent.mkdir(parents=True, exist_ok=True)
            (kb / rel).write_bytes(PDF)
        yield c


def save(pdf, text, separator="===CHUNK==="):
    return storage.save_new_version(storage.kb_dir(KB), pdf, text, {"chunk": {"separator": separator, "max_chars": 800}})


def text(*chunks, sep="===CHUNK==="):
    return f"\n\n{sep}\n\n".join(chunks)


def test_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATUM_DATA_DIR", str(tmp_path / "lib"))
    monkeypatch.delenv("STRATUM_FASTGPT_URL", raising=False)
    with TestClient(app) as c:
        assert c.get("/api/fastgpt").json()["configured"] is False
        r = c.get("/api/fastgpt/datasets")
        assert r.status_code == 400 and "未配置" in r.json()["detail"]


def test_list_and_bind(client, fake):
    datasets = client.get("/api/fastgpt/datasets").json()["datasets"]
    assert [(d["id"], d["name"]) for d in datasets] == [("ds1", "安全规程"), ("ds2", "部门 / 设备手册")]
    assert client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": "nope"}).status_code == 502
    bound = client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": "ds1"}).json()
    assert bound["dataset"] == {"id": "ds1", "name": "安全规程"}
    files = client.get(f"/api/kbs/{KB}/files").json()
    assert files["fastgpt"]["dataset"]["id"] == "ds1" and files["fastgpt"]["configured"]
    # 修改默认参数不影响绑定
    client.put(f"/api/kbs/{KB}/settings", json={"chunk": {"separator": "@@@"}})
    assert client.get(f"/api/kbs/{KB}/fastgpt").json()["dataset"]["id"] == "ds1"
    assert client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": None}).json()["dataset"] is None


def test_kb_from_dataset(client, fake):
    # 新建同名知识库并绑定
    r = client.post("/api/fastgpt/datasets/ds1/kb")
    assert r.status_code == 200 and r.json() == {"name": "安全规程", "created": True}
    kbs = {kb["name"]: kb for kb in client.get("/api/kbs").json()["kbs"]}
    assert kbs["安全规程"]["fastgpt"] == {"dataset_id": "ds1", "dataset_name": "安全规程"}
    assert kbs[KB]["fastgpt"] is None
    # 再次点击：直接返回已绑定的知识库
    assert client.post("/api/fastgpt/datasets/ds1/kb").json() == {"name": "安全规程", "created": False}
    # 同名但未绑定的知识库：绑定它，不新建
    client.post("/api/kbs", json={"name": "设备手册"})
    assert client.post("/api/fastgpt/datasets/ds2/kb").json() == {"name": "设备手册", "created": False}
    assert client.get("/api/kbs/设备手册/fastgpt").json()["dataset"]["id"] == "ds2"
    # 不存在的知识库
    assert client.post("/api/fastgpt/datasets/nope/kb").status_code >= 400


def test_kb_from_dataset_name_taken(client, fake):
    client.post("/api/kbs", json={"name": "安全规程"})
    client.put("/api/kbs/安全规程/fastgpt", json={"dataset_id": "ds2"})
    assert client.post("/api/fastgpt/datasets/ds1/kb").json() == {"name": "安全规程(2)", "created": True}


def test_auto_import_toggle_and_run(client, fake):
    kb = storage.kb_dir(KB)
    assert client.get(f"/api/kbs/{KB}/fastgpt").json()["auto_import"] is False
    save("a.pdf", text("一", "二"))
    # 未开启：不导入
    assert fastgpt.auto_import(server.fastgpt_client, kb, "a.pdf") is None
    r = client.put(f"/api/kbs/{KB}/fastgpt/auto-import", json={"enabled": True})
    assert r.status_code == 200 and r.json()["auto_import"] is True
    # 开启但未绑定：仍不导入
    assert fastgpt.auto_import(server.fastgpt_client, kb, "a.pdf") is None
    client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": "ds1"})
    assert fastgpt.auto_import(server.fastgpt_client, kb, "a.pdf") == {"status": "imported", "message": None, "chunks": 2}
    # 修改默认参数不会丢掉开关
    client.put(f"/api/kbs/{KB}/settings", json=storage.default_settings())
    assert client.get(f"/api/kbs/{KB}/fastgpt").json()["auto_import"] is True
    # 别人手动导入的同名集合：无人值守时跳过，不删除
    fake.collections["x"] = {"_id": "x", "datasetId": "ds1", "name": "c.md"}
    save("c.pdf", text("三"))
    assert fastgpt.auto_import(server.fastgpt_client, kb, "c.pdf")["status"] == "skipped"
    assert "x" in fake.collections
    client.put(f"/api/kbs/{KB}/fastgpt/auto-import", json={"enabled": False})
    assert fastgpt.auto_import(server.fastgpt_client, kb, "c.pdf") is None


def test_kb_name_from():
    assert storage.kb_name_from("部门 / 规程") == "部门 - 规程"
    assert storage.kb_name_from("..x") == "x"
    assert storage.kb_name_from("  ") == "知识库"
    assert len(storage.kb_name_from("长" * 100)) == 64


def test_wrong_key(client, monkeypatch):
    monkeypatch.setenv("STRATUM_FASTGPT_API_KEY", "wrong")
    r = client.get("/api/fastgpt/datasets")
    assert r.status_code == 502 and "API 密钥" in r.json()["detail"]


def test_import_requires_binding_and_version(client):
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "a.pdf"})
    assert r.status_code == 400 and "绑定" in r.json()["detail"]
    client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": "ds1"})
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "a.pdf"}).json()
    assert r["status"] == "error" and "还没有" in r["message"]


def test_import_replace_and_unchanged(client, fake):
    client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": "ds1"})
    v1 = save("sub/b.pdf", text("第一块", "第二块", "第三块"))
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "sub/b.pdf"}).json()
    assert r["status"] == "imported" and r["chunks"] == 3 and r["md_path"] == v1["path"]
    sent = next(b for m, p, b in fake.requests if p.endswith("/create/text"))
    assert sent["name"] == "sub/b.md" and sent["datasetId"] == "ds1"
    assert sent["chunkSettingMode"] == "custom" and sent["chunkSplitMode"] == "char"
    assert sent["chunkSplitter"] == "===CHUNK===" and sent["chunkSize"] == 1600
    assert sent["metadata"] == {"source": "stratum", "pdf": "sub/b.pdf", "version": 1}

    again = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "sub/b.pdf"}).json()
    assert again["status"] == "unchanged"

    # 新版本：先建新集合，再删除旧集合；使用该版本记录的分隔符
    save("sub/b.pdf", text("新一", "新二", sep="@@@"), separator="@@@")
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "sub/b.pdf"}).json()
    assert r["status"] == "imported" and r["chunks"] == 2 and r["replaced"] == 1
    assert list(fake.collections) == ["c2"] and fake.collections["c2"]["chunkSplitter"] == "@@@"
    status = client.get(f"/api/kbs/{KB}/fastgpt").json()["imports"]["sub/b.pdf"]
    assert status["version"] == 2 and status["chunks"] == 2

    # 修改已导入的版本后，再导入会重新创建
    kb = storage.kb_dir(KB)
    doc = storage.read_markdown(kb, "sub/b_v2.md")
    storage.overwrite_markdown(kb, "sub/b_v2.md", text("改一", "改二", "改三", sep="@@@"), doc["etag"])
    assert fastgpt.needs_import(kb) == ["sub/b.pdf"]
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "sub/b.pdf"}).json()
    assert r["status"] == "imported" and r["chunks"] == 3 and list(fake.collections) == ["c3"]
    assert fastgpt.needs_import(kb) == []

    # 指定导入旧版本
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "sub/b.pdf", "md_path": v1["path"]}).json()
    assert r["status"] == "imported" and fastgpt.needs_import(kb) == ["sub/b.pdf"]


def test_conflict_with_manual_collection(client, fake):
    client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": "ds1"})
    fake.collections["manual"] = {"_id": "manual", "datasetId": "ds1", "name": "a.md"}
    save("a.pdf", text("一", "二"))
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "a.pdf"}).json()
    assert r["status"] == "conflict" and "a.md" in r["message"] and len(fake.collections) == 1
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "a.pdf", "on_conflict": "keep"}).json()
    assert r["status"] == "imported" and "manual" in fake.collections and len(fake.collections) == 2
    fastgpt._update_record(storage.kb_dir(KB), "ds1", "a.pdf", None)
    for cid in [c for c in fake.collections if c != "manual"]:
        fake.collections.pop(cid)
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "a.pdf", "on_conflict": "replace"}).json()
    assert r["status"] == "imported" and "manual" not in fake.collections and len(fake.collections) == 1


def test_failures_are_recorded(client, fake):
    client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": "ds1"})
    save("a.pdf", text("一", "二"))
    fake.fail_create = True
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "a.pdf"}).json()
    assert r["status"] == "error" and "embedding model unavailable" in r["message"]
    assert client.get(f"/api/kbs/{KB}/fastgpt").json()["imports"]["a.pdf"]["error"]
    fake.fail_create = False
    assert client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "a.pdf"}).json()["status"] == "imported"
    # 删除旧集合失败：新集合保留，给出提示
    save("a.pdf", text("三", "四"))
    fake.fail_delete = True
    r = client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "a.pdf"}).json()
    assert r["status"] == "imported" and "删除旧集合" in r["message"] and len(fake.collections) == 2


def test_batch_import(client, fake):
    client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": "ds1"})
    save("a.pdf", text("一", "二"))
    save("sub/b.pdf", text("三"))
    fake.collections["manual"] = {"_id": "manual", "datasetId": "ds1", "name": "sub/b.md"}
    job = client.post(f"/api/kbs/{KB}/fastgpt/imports", json={}).json()
    assert job["total"] == 2  # c.pdf 没有解析结果，不在待导入列表中
    end = time.time() + 10
    while job["status"] != "done" and time.time() < end:
        time.sleep(0.05)
        job = client.get(f"/api/kbs/{KB}/fastgpt/imports/{job['id']}").json()
    by_path = {i["pdf_path"]: i for i in job["items"]}
    assert by_path["a.pdf"]["status"] == "imported"
    assert by_path["sub/b.pdf"]["status"] == "skipped"  # 批量默认不替换手动导入的同名集合
    job = client.post(f"/api/kbs/{KB}/fastgpt/imports", json={"paths": ["sub/b.pdf"], "on_conflict": "replace"}).json()
    end = time.time() + 10
    while job["status"] != "done" and time.time() < end:
        time.sleep(0.05)
        job = client.get(f"/api/kbs/{KB}/fastgpt/imports/{job['id']}").json()
    assert job["items"][0]["status"] == "imported" and "manual" not in fake.collections
    assert client.post(f"/api/kbs/{KB}/fastgpt/imports", json={"on_conflict": "bad"}).status_code == 400


def test_concurrent_imports_of_same_pdf_create_one_collection(client, fake):
    import threading
    client.put(f"/api/kbs/{KB}/fastgpt", json={"dataset_id": "ds1"})
    save("a.pdf", text("一", "二"))
    original = fake.__call__
    slow = threading.Event()

    def slow_create(request):
        if request.url.path.endswith("/create/text"):
            slow.wait(0.3)
        return original(request)
    server.fastgpt_client._transport = httpx.MockTransport(slow_create)
    barrier = threading.Barrier(4)
    results = []

    def run():
        barrier.wait()
        results.append(client.post(f"/api/kbs/{KB}/fastgpt/import", json={"pdf_path": "a.pdf"}).json()["status"])
    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == ["imported", "unchanged", "unchanged", "unchanged"]
    assert len(fake.collections) == 1

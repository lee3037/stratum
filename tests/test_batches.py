import csv
import io
import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STRATUM_WORKERS", "1")

from app import exports, storage  # noqa: E402
from app.batches import BatchManager  # noqa: E402
from app.storage import StorageError  # noqa: E402

PDF = b"%PDF-1.4\n%fake\n"


def fake_result(text="# 标题\n\n正文"):
    return {"result": {"markdown": text, "stats": {"chunks": 1},
                       "doc_stats": {"pages": 2, "ocr_pages": 1, "tables": 0}, "warnings": []}}


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATUM_DATA_DIR", str(tmp_path / "lib"))
    kb = storage.create_kb("库A")
    for rel in ("a.pdf", "b.pdf", "sub/c.pdf", "bad.pdf"):
        (kb / rel).parent.mkdir(parents=True, exist_ok=True)
        (kb / rel).write_bytes(PDF)
    return kb


def wait(manager, kb_name, batch_id, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        batch = manager.public(manager.get(kb_name, batch_id))
        if batch["status"] != "running":
            return batch
        time.sleep(0.05)
    raise TimeoutError(batch_id)


def settings():
    return storage.default_settings()


def test_batch_runs_saves_and_reports(kb):
    calls = []

    def convert(pdf, options):
        calls.append((Path(pdf).name, options["separator"]))
        if pdf.endswith("bad.pdf"):
            raise ValueError("无法打开 PDF")
        return fake_result(f"# {Path(pdf).stem}")

    storage.save_new_version(kb, "b.pdf", "old")
    m = BatchManager(convert, 2)
    m.start()
    try:
        created = m.create("库A", ["a.pdf", "b.pdf", "sub/c.pdf", "bad.pdf", "a.pdf"], settings(), skip_converted=True)
        assert created["total"] == 4  # 去重
        batch = wait(m, "库A", created["id"])
    finally:
        m.stop()
    by_path = {item["path"]: item for item in batch["items"]}
    assert by_path["b.pdf"]["status"] == "skipped"
    assert by_path["a.pdf"]["status"] == "done" and by_path["a.pdf"]["version_path"] == "a_v1.md"
    assert by_path["sub/c.pdf"]["version_path"] == "sub/c_v1.md" and by_path["sub/c.pdf"]["pages"] == 2
    assert by_path["bad.pdf"]["status"] == "error" and "无法打开" in by_path["bad.pdf"]["error"]
    assert batch["status"] == "done" and batch["counts"]["done"] == 2 and batch["counts"]["error"] == 1
    assert (kb / "a_v1.md").read_text() == "# a"
    meta = storage.read_meta(kb, "a_v1.md")
    assert meta["origin"] == "batch" and meta["batch_id"] == created["id"] and meta["chunks"] == 1
    assert all(sep == "===CHUNK===" for _, sep in calls)
    assert (kb / ".stratum" / "batches" / f"{created['id']}.json").is_file()


def test_validation(kb):
    m = BatchManager(lambda *_: fake_result(), 1)
    with pytest.raises(StorageError):
        m.create("库A", [], settings(), True)
    with pytest.raises(StorageError):
        m.create("库A", ["missing.pdf"], settings(), True)
    with pytest.raises(StorageError):
        m.create("库A", ["../x.pdf"], settings(), True)
    with pytest.raises(StorageError, match="未知"):
        m.create("库A", ["a.pdf"], {"convert": {"parser": "oxr"}}, True)


def test_cancel_and_retry(kb):
    gate = threading.Event()

    def convert(pdf, options):
        gate.wait(5)
        return fake_result()

    m = BatchManager(convert, 1)
    m.start()
    try:
        created = m.create("库A", ["a.pdf", "b.pdf", "sub/c.pdf"], settings(), True)
        time.sleep(0.2)  # 第一个文件进入解析
        cancelled = m.cancel("库A", created["id"])
        assert cancelled["counts"]["cancelled"] == 2 and cancelled["counts"]["running"] == 1
        gate.set()
        batch = wait(m, "库A", created["id"])
        assert batch["status"] == "cancelled" and batch["counts"]["done"] == 1
        retried = m.retry("库A", created["id"])
        assert retried["status"] == "running"
        batch = wait(m, "库A", created["id"])
        assert batch["status"] == "done" and batch["counts"]["done"] == 3
    finally:
        gate.set()
        m.stop()


def test_round_robin_between_batches(kb):
    order = []
    lock = threading.Lock()

    def convert(pdf, options):
        with lock:
            order.append(Path(pdf).name)
        time.sleep(0.02)
        return fake_result()

    other = storage.create_kb("库B")
    for i in range(3):
        (other / f"x{i}.pdf").write_bytes(PDF)
    m = BatchManager(convert, 1)
    first = m.create("库A", ["a.pdf", "b.pdf", "sub/c.pdf"], settings(), True)
    second = m.create("库B", ["x0.pdf", "x1.pdf", "x2.pdf"], settings(), True)
    m.start()
    try:
        wait(m, "库A", first["id"])
        wait(m, "库B", second["id"])
    finally:
        m.stop()
    # 两个批次交替执行，后提交的批次不用等前一个全部完成
    assert order[:4] == ["a.pdf", "x0.pdf", "b.pdf", "x1.pdf"]


def test_resume_after_restart(kb):
    m = BatchManager(lambda *_: fake_result(), 1)
    created = m.create("库A", ["a.pdf", "b.pdf"], settings(), True)
    # 模拟上次运行到一半时进程退出
    record = kb / ".stratum" / "batches" / f"{created['id']}.json"
    data = json.loads(record.read_text())
    data["items"][0]["status"] = "running"
    record.write_text(json.dumps(data))
    restarted = BatchManager(lambda *_: fake_result(), 1)
    restarted.start()
    try:
        batch = wait(restarted, "库A", created["id"])
    finally:
        restarted.stop()
    assert batch["counts"]["done"] == 2
    assert [b["id"] for b in restarted.list("库A")] == [created["id"]]


def read_zip(path):
    with zipfile.ZipFile(path) as zf:
        names = sorted(zf.namelist())
        summary = zf.read("汇总.csv").decode("utf-8-sig")
        contents = {n: zf.read(n) for n in names}
    return names, list(csv.DictReader(io.StringIO(summary))), contents


def test_export_modes(kb, tmp_path):
    storage.save_new_version(kb, "a.pdf", "a1", {"origin": "manual", "doc_stats": {"pages": 3}, "chunks": 5})
    storage.save_new_version(kb, "a.pdf", "a2", {"origin": "batch"})
    storage.save_new_version(kb, "sub/c.pdf", "c1")

    target = tmp_path / "latest.zip"
    stats = exports.build_zip(kb, target, "latest")
    names, rows, _ = read_zip(target)
    assert set(names) == {"a_v2.md", "sub/c_v1.md", "汇总.csv"}
    assert stats["markdown"] == 2
    assert {r["PDF"]: r["说明"] for r in rows if not r["Markdown"]} == {"b.pdf": "尚未解析", "bad.pdf": "尚未解析"}

    exports.build_zip(kb, target, "all", paths=["a.pdf"])
    names, rows, _ = read_zip(target)
    assert set(names) == {"a_v1.md", "a_v2.md", "汇总.csv"}
    assert next(r for r in rows if r["Markdown"] == "a_v1.md")["页数"] == "3"

    exports.build_zip(kb, target, "fastgpt", with_pdf=True, paths=["a.pdf", "sub/c.pdf"])
    names, _, contents = read_zip(target)
    assert set(names) == {"a.md", "sub/c.md", "a.pdf", "sub/c.pdf", "汇总.csv"}
    assert contents["a.md"] == b"a2" and contents["a.pdf"] == PDF

    with pytest.raises(StorageError):
        exports.build_zip(kb, target, "nope")
    with pytest.raises(StorageError):
        exports.build_zip(kb, target, "latest", paths=["../x.pdf"])


def test_export_batch_uses_batch_versions(kb, tmp_path):
    m = BatchManager(lambda pdf, _o: fake_result(Path(pdf).stem + "-batch"), 1)
    m.start()
    try:
        created = m.create("库A", ["a.pdf", "b.pdf"], settings(), True)
        batch = m.get("库A", wait(m, "库A", created["id"])["id"])
    finally:
        m.stop()
    storage.save_new_version(kb, "a.pdf", "人工改过的新版本")
    target = tmp_path / "batch.zip"
    exports.build_zip(kb, target, "latest", batch=batch)
    names, rows, contents = read_zip(target)
    assert set(names) == {"a_v1.md", "b_v1.md", "汇总.csv"}
    assert contents["a_v1.md"] == b"a-batch"
    assert {r["批量状态"] for r in rows} == {"完成"}


def test_files_already_in_another_batch_are_not_parsed_twice(kb):
    gate = threading.Event()
    calls = []

    def convert(pdf, options):
        calls.append(Path(pdf).name)
        gate.wait(5)
        return fake_result()

    m = BatchManager(convert, 1)
    m.start()
    try:
        first = m.create("库A", ["a.pdf", "b.pdf"], settings(), False)
        second = m.create("库A", ["b.pdf", "sub/c.pdf"], settings(), False)
        items = {i["path"]: i for i in m.get("库A", second["id"])["items"]}
        assert items["b.pdf"]["status"] == "skipped" and "其他批次" in items["b.pdf"]["message"]
        assert items["sub/c.pdf"]["status"] == "queued"
        assert m.stats() == {"batches": 2, "concurrency": 1, "queued": 2, "running": 1}
        gate.set()
        wait(m, "库A", first["id"])
        wait(m, "库A", second["id"])
        assert sorted(calls) == ["a.pdf", "b.pdf", "c.pdf"]
    finally:
        gate.set()
        m.stop()


def test_worker_survives_unexpected_errors(kb, monkeypatch):
    m = BatchManager(lambda *_: fake_result(), 1)
    original = storage.save_new_version
    calls = {"n": 0}

    def flaky_save(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("意外")  # 不是普通的解析错误
        return original(*args, **kwargs)
    monkeypatch.setattr(storage, "save_new_version", flaky_save)
    m.start()
    try:
        created = m.create("库A", ["a.pdf", "b.pdf"], settings(), False)
        batch = wait(m, "库A", created["id"])
        assert batch["counts"]["error"] == 1 and batch["counts"]["done"] == 1
    finally:
        m.stop()


def test_disk_full_marks_items_as_errors(kb, monkeypatch):
    monkeypatch.setenv("STRATUM_MIN_FREE_MB", str(1 << 40))
    m = BatchManager(lambda *_: fake_result(), 1)
    m.start()
    try:
        created = m.create("库A", ["a.pdf"], settings(), False)
        batch = wait(m, "库A", created["id"])
        assert batch["counts"]["error"] == 1 and "磁盘" in batch["items"][0]["error"]
    finally:
        m.stop()


def test_after_save_hook_records_outcome(kb):
    seen = []

    def after_save(kb_name, pdf):
        seen.append((kb_name, pdf))
        if pdf == "b.pdf":
            raise RuntimeError("FastGPT 不可用")
        return {"status": "imported", "chunks": 1} if pdf == "a.pdf" else None

    m = BatchManager(lambda pdf, options: fake_result(), 1)
    m.after_save = after_save
    m.start()
    try:
        created = m.create("库A", ["a.pdf", "b.pdf", "sub/c.pdf"], settings(), skip_converted=False)
        batch = wait(m, "库A", created["id"])
    finally:
        m.stop()
    by_path = {item["path"]: item for item in batch["items"]}
    assert sorted(seen) == [("库A", "a.pdf"), ("库A", "b.pdf"), ("库A", "sub/c.pdf")]
    # 钩子失败不影响解析状态，结果记入 fastgpt
    assert all(item["status"] == "done" for item in by_path.values())
    assert by_path["a.pdf"]["fastgpt"] == {"status": "imported", "chunks": 1}
    assert by_path["b.pdf"]["fastgpt"]["status"] == "error" and "不可用" in by_path["b.pdf"]["fastgpt"]["message"]
    assert "fastgpt" not in by_path["sub/c.pdf"]

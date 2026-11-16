import io
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import storage  # noqa: E402
from app.storage import StorageError  # noqa: E402

PDF = b"%PDF-1.4\n%fake\n"


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATUM_DATA_DIR", str(tmp_path / "lib"))
    return storage.create_kb("安全规程库")


def put(kb: Path, rel: str, data: bytes = PDF):
    path = kb / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.mark.parametrize("rel", [
    "../x.pdf", "a/../../x.pdf", "/etc/passwd", "C:/x.pdf", "a\\b.pdf", ".stratum/settings.json",
    "a/.hidden/b.pdf", "", "a//b.pdf", "a/./b.pdf", "a\x00b.pdf",
])
def test_rejects_unsafe_paths(kb, rel):
    with pytest.raises(StorageError):
        storage.resolve(kb, rel)


def test_rejects_symlink_escape(kb, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(PDF)
    os.symlink(outside, kb / "link")
    with pytest.raises(StorageError):
        storage.resolve(kb, "link/secret.pdf")
    assert storage.scan(kb)["files"] == []


@pytest.mark.parametrize("name", ["", " ", ".hidden", "a/b", "a\\b", "x" * 65, ".."])
def test_rejects_bad_kb_names(tmp_path, monkeypatch, name):
    monkeypatch.setenv("STRATUM_DATA_DIR", str(tmp_path))
    with pytest.raises(StorageError):
        storage.create_kb(name)


def test_kb_create_list_and_duplicate(kb):
    with pytest.raises(StorageError) as exc:
        storage.create_kb("安全规程库")
    assert exc.value.status == 409
    put(kb, "a.pdf")
    storage.save_new_version(kb, "a.pdf", "# a")
    [info] = storage.list_kbs()
    assert info["name"] == "安全规程库" and info["pdfs"] == 1 and info["converted"] == 1
    with pytest.raises(StorageError) as exc:
        storage.kb_dir("不存在")
    assert exc.value.status == 404


def test_scan_indexes_versions_and_legacy(kb):
    for rel in ("合同/a.pdf", "合同/a.md", "合同/a_v2.md", "b.pdf", "b_v1.md", "README.md", "c_v3.md", ".stratum/x.pdf"):
        put(kb, rel)
    inv = storage.scan(kb)
    assert [f["path"] for f in inv["files"]] == ["b.pdf", "合同/a.pdf"]
    assert all(f["converted"] for f in inv["files"])
    by_path = {a["path"]: (a["pdf_path"], a["version"]) for a in inv["artifacts"]}
    assert by_path == {
        "合同/a.md": ("合同/a.pdf", 1),
        "合同/a_v2.md": ("合同/a.pdf", 2),
        "b_v1.md": ("b.pdf", 1),
        "c_v3.md": ("c.pdf", 3),  # 孤立产物仍然列出，PDF 缺失由前端提示
    }


def test_versions_increment_and_never_overwrite(kb):
    put(kb, "x/a.pdf")
    put(kb, "x/a_v4.md", b"keep")
    first = storage.save_new_version(kb, "x/a.pdf", "new", {"origin": "manual"})
    assert first["path"] == "x/a_v5.md" and first["version"] == 5
    assert (kb / "x/a_v4.md").read_bytes() == b"keep"
    meta = storage.read_meta(kb, "x/a_v5.md")
    assert meta["origin"] == "manual" and meta["version"] == 5
    with pytest.raises(StorageError):
        storage.save_new_version(kb, "x/missing.pdf", "x")


def test_requested_version_used_when_free(kb):
    put(kb, "a.pdf")
    assert storage.save_new_version(kb, "a.pdf", "v2", min_version=2)["version"] == 2
    # 预定的 v1 已低于目录中的最大版本号，取下一个空闲版本
    assert storage.save_new_version(kb, "a.pdf", "v1", min_version=1)["version"] == 3


def test_concurrent_saves_get_distinct_versions(kb):
    put(kb, "a.pdf")
    results, errors = [], []

    def save(i):
        try:
            results.append(storage.save_new_version(kb, "a.pdf", f"content {i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(r["version"] for r in results) == list(range(1, 13))
    contents = {(kb / r["path"]).read_text() for r in results}
    assert len(contents) == 12


def test_overwrite_detects_conflicts(kb):
    put(kb, "a.pdf")
    saved = storage.save_new_version(kb, "a.pdf", "v1")
    read = storage.read_markdown(kb, saved["path"])
    assert read["markdown"] == "v1" and read["etag"] == saved["etag"]
    updated = storage.overwrite_markdown(kb, saved["path"], "edited by A", read["etag"])
    with pytest.raises(StorageError) as exc:
        storage.overwrite_markdown(kb, saved["path"], "edited by B", read["etag"])
    assert exc.value.status == 409
    assert (kb / saved["path"]).read_text() == "edited by A"
    storage.overwrite_markdown(kb, saved["path"], "B again", updated["etag"])
    assert "edited" in storage.read_meta(kb, saved["path"])


def test_upload_dedup_and_rename(kb):
    a = storage.store_upload(kb, "规程/a.pdf", io.BytesIO(PDF), 1 << 20)
    assert a == {"path": "规程/a.pdf", "status": "added", "size": len(PDF)}
    same = storage.store_upload(kb, "规程/A.PDF", io.BytesIO(PDF), 1 << 20)
    assert same["status"] == "skipped" and same["path"] == "规程/a.pdf"
    other = storage.store_upload(kb, "规程/a.pdf", io.BytesIO(PDF + b"2"), 1 << 20)
    assert other["status"] == "renamed" and other["path"] == "规程/a(2).pdf"
    third = storage.store_upload(kb, "规程/a.pdf", io.BytesIO(PDF + b"3"), 1 << 20)
    assert third["path"] == "规程/a(3).pdf"
    assert not list((kb / ".stratum" / "tmp").iterdir())


def test_upload_sanitizes_and_validates(kb):
    r = storage.store_upload(kb, "..\\..\\evil/../.x.pdf", io.BytesIO(PDF), 1 << 20)
    assert r["path"] == "evil/x.pdf"
    with pytest.raises(StorageError, match="不是 PDF"):
        storage.store_upload(kb, "a.pdf", io.BytesIO(b"hello"), 1 << 20)
    with pytest.raises(StorageError, match="不是 PDF 文件"):
        storage.store_upload(kb, "a.txt", io.BytesIO(PDF), 1 << 20)
    with pytest.raises(StorageError) as exc:
        storage.store_upload(kb, "big.pdf", io.BytesIO(PDF + b"x" * 2048), 1024)
    assert exc.value.status == 413

    def reject(_path):
        raise StorageError("页数超过上限")
    with pytest.raises(StorageError, match="页数"):
        storage.store_upload(kb, "c.pdf", io.BytesIO(PDF), 1 << 20, check_pdf=reject)
    assert not (kb / "c.pdf").exists()
    assert not list((kb / ".stratum" / "tmp").iterdir())


def test_settings_roundtrip(kb):
    settings = storage.read_settings(kb)
    assert settings["chunk"]["separator"] == "===CHUNK==="
    updated = storage.write_settings(kb, {"chunk": {"separator": "@@@", "max_chars": 1500}, "convert": {}})
    assert updated["chunk"]["separator"] == "@@@" and updated["chunk"]["max_chars"] == 1500
    assert storage.read_settings(kb)["chunk"]["separator"] == "@@@"
    with pytest.raises(StorageError):
        storage.write_settings(kb, {"convert": {"parser": "unknown"}})


def test_retired_parser_in_settings_falls_back(kb):
    import json
    path = storage._settings_path(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"convert": {"parser": "oxr", "ocr_mode": "off"}}), encoding="utf-8")
    convert = storage.read_settings(kb)["convert"]
    assert convert["parser"] == "pymupdf" and convert["ocr_mode"] == "off"

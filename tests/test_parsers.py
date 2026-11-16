"""解析后端注册表与选项兼容性测试。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import parsers  # noqa: E402
from app.chunker import ChunkOptions  # noqa: E402
from app.pipeline import ConvertOptions, convert  # noqa: E402


def test_options_compatibility_and_registry():
    assert ConvertOptions().parser == "pymupdf"
    assert ConvertOptions.from_dict({"ocr_mode": "off", "ocr_dpi": 900}).ocr_dpi == 300
    assert ConvertOptions.from_dict({"ocr_mode": "invalid"}).ocr_mode == "auto"
    with pytest.raises(ValueError):
        ConvertOptions.from_dict({"ocr_dpi": "not-a-number"})
    assert parsers.catalog() == [
        {"id": "pymupdf", "name": "PyMuPDF + RapidOCR", "local_ocr_options": True},
    ]
    for parser in ("unknown", "", "oxr"):
        with pytest.raises(ValueError, match="未知解析后端"):
            ConvertOptions.from_dict({"parser": parser})
        with pytest.raises(ValueError, match="未知解析后端"):
            convert("unused.pdf", ConvertOptions(parser=parser), ChunkOptions())


def test_unavailable_backend_is_hidden_and_rejected(monkeypatch):
    class Remote:
        id, name, local_ocr_options = "remote", "Remote", False

        def available(self):
            return False

    monkeypatch.setitem(parsers._registry, "remote", Remote())
    assert all(p["id"] != "remote" for p in parsers.catalog())
    with pytest.raises(ValueError, match="未启用"):
        parsers.ensure_available("remote")

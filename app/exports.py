"""打包下载：把知识库中的解析结果（可选连同 PDF）打成 zip，附带汇总表。

模式：
- latest   每个 PDF 只取最新版本，文件名保留 `_vN`
- all      全部版本
- fastgpt  每个 PDF 只取最新版本，文件名去掉 `_vN`（`a.md`），便于导入 FastGPT 后显示原文件名
"""
from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import storage
from .storage import StorageError

MODES = ("latest", "all", "fastgpt")
STATUS_TEXT = {"done": "完成", "error": "失败", "skipped": "跳过", "cancelled": "已取消",
               "queued": "排队中", "running": "解析中"}


def _fmt_time(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def build_zip(kb: Path, target: Path, mode: str = "latest", paths: list[str] | None = None,
              with_pdf: bool = False, batch: dict | None = None) -> dict:
    """写出 zip 到 target，返回统计。paths 为 PDF 相对路径；为空时导出整个知识库（或整个批次）。"""
    if mode not in MODES:
        raise StorageError(f"未知导出模式：{mode}")
    inventory = storage.scan(kb)
    files = {f["path"]: f for f in inventory["files"]}
    versions: dict[str, list[dict]] = {}
    for artifact in inventory["artifacts"]:
        versions.setdefault(artifact["pdf_path"], []).append(artifact)
    batch_items = {item["path"]: item for item in (batch or {}).get("items", [])}

    if paths:
        wanted = [storage.clean_rel(p) for p in paths]
    elif batch:
        wanted = list(batch_items)
    else:
        wanted = list(files)
    wanted = list(dict.fromkeys(wanted))

    rows = []
    md_count = pdf_count = 0
    used_names: set[str] = set()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for pdf_rel in wanted:
            pdf_versions = sorted(versions.get(pdf_rel, []), key=lambda a: a["version"])
            item = batch_items.get(pdf_rel)
            if batch and item and item.get("version_path"):
                # 批次导出取该批次生成的版本，而不是之后又被人改出的新版本
                chosen = [a for a in pdf_versions if a["path"] == item["version_path"]] or pdf_versions[-1:]
            else:
                chosen = pdf_versions if mode == "all" else pdf_versions[-1:]
            for artifact in chosen:
                source = storage.resolve(kb, artifact["path"])
                if not source.is_file():
                    continue
                name = artifact["path"]
                if mode == "fastgpt":
                    name = re.sub(r"\.pdf$", "", pdf_rel, flags=re.I) + ".md"
                    if name.lower() in used_names:  # 极少见：a.md 与 a(2).md 之类的冲突
                        name = artifact["path"]
                used_names.add(name.lower())
                zf.write(source, name)
                md_count += 1
                meta = storage.read_meta(kb, artifact["path"])
                stats = meta.get("doc_stats") or {}
                rows.append({
                    "PDF": pdf_rel, "Markdown": name, "版本": artifact["version"],
                    "页数": stats.get("pages", ""), "块数": meta.get("chunks", ""),
                    "OCR 页数": stats.get("ocr_pages", ""), "表格数": stats.get("tables", ""),
                    "分隔符": (meta.get("chunk") or {}).get("separator", ""),
                    "来源": {"batch": "批量解析", "manual": "手动保存"}.get(meta.get("origin"), meta.get("origin", "")),
                    "保存时间": _fmt_time(meta.get("created") or artifact["mtime"]),
                    "批量状态": STATUS_TEXT.get(item["status"], "") if item else "",
                    "说明": (item or {}).get("error") or (item or {}).get("message") or "",
                })
            if not chosen:
                rows.append({"PDF": pdf_rel, "Markdown": "", "版本": "",
                             "批量状态": STATUS_TEXT.get(item["status"], "") if item else "",
                             "说明": (item or {}).get("error") or (item or {}).get("message") or "尚未解析"})
            if with_pdf and pdf_rel in files:
                zf.write(storage.resolve(kb, pdf_rel), pdf_rel)
                pdf_count += 1
        columns = ["PDF", "Markdown", "版本", "页数", "块数", "OCR 页数", "表格数", "分隔符", "来源", "保存时间",
                   "批量状态", "说明"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        # 带 BOM，Excel 直接打开不乱码
        zf.writestr("汇总.csv", "﻿" + buf.getvalue())
    return {"markdown": md_count, "pdf": pdf_count, "rows": len(rows)}


def zip_name(kb_name: str, mode: str, batch_id: str | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M")
    suffix = {"latest": "最新版本", "all": "全部版本", "fastgpt": "FastGPT"}[mode]
    return f"{kb_name}-{'批次' + batch_id + '-' if batch_id else ''}{suffix}-{stamp}.zip"

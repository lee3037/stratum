"""批量解析：在服务端逐个解析知识库中的 PDF，每完成一个就保存为新版本。

- 批次记录保存在 `<知识库>/.stratum/batches/<id>.json`，每次状态变化都写盘；服务重启后未完成的文件重新排队。
- 全局只有少量批量工作线程（STRATUM_BATCH_CONCURRENCY），在各批次之间轮流取文件，
  一个人提交上千个文件也不会让其他人的批次一直排在后面；单个文件的交互解析不经过这里。
- 批量文件的页面分组在解析进程中以低优先级执行（见 scheduler.py），有人单个解析时先让路。
- 已在某个批次中排队或解析的文件，再次提交到新批次时直接跳过。
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from . import chunker, parsers, pipeline, storage
from .storage import StorageError

ACTIVE = ("queued", "running")
FINAL = ("done", "error", "skipped", "cancelled")
MAX_ITEMS = 5000
KEEP_BATCHES = 50


def _batch_dir(kb: Path) -> Path:
    return kb / storage.META_DIR / "batches"


class BatchManager:
    def __init__(self, convert_fn: Callable[[str, dict], dict], concurrency: int):
        """convert_fn(pdf_path, options) -> pipeline.convert 的返回值；由服务注入，复用解析进程池。"""
        self._convert = convert_fn
        self._concurrency = max(1, concurrency)
        self._batches: dict[str, dict] = {}
        self._cond = threading.Condition()
        self._served: dict[str, float] = {}  # 批次上次被取文件的时间，用于轮转
        self._generation = 0  # 每次 start 递增；旧一代的工作线程醒来后自行退出
        # after_save(kb 名, PDF 相对路径) -> dict | None：每个文件保存后调用（如自动导入 FastGPT），结果记入 item["fastgpt"]
        self.after_save: Callable[[str, str], dict | None] | None = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        with self._cond:
            self._generation += 1
            generation = self._generation
            self._batches.clear()
            self._served.clear()
        self._load_all()
        for i in range(self._concurrency):
            threading.Thread(target=self._worker, args=(generation,), name=f"batch-{i}", daemon=True).start()

    def stop(self) -> None:
        with self._cond:
            self._generation += 1
            self._cond.notify_all()

    def _load_all(self) -> None:
        """恢复磁盘上的批次；上次运行到一半的文件重新排队。"""
        for info in storage.list_kbs():
            folder = _batch_dir(storage.kb_dir(info["name"]))
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                try:
                    batch = json.loads(path.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(batch, dict) or "id" not in batch:
                    continue
                batch["kb"] = info["name"]
                resumed = False
                for item in batch.get("items", []):
                    if item.get("status") == "running":
                        item["status"] = "queued"
                        resumed = True
                if resumed:
                    batch["resumed"] = time.time()
                self._refresh_status(batch)
                with self._cond:
                    self._batches[batch["id"]] = batch
                if resumed:
                    self._persist(batch)
        with self._cond:
            self._cond.notify_all()

    # ------------------------------------------------------------ 公开操作

    def create(self, kb_name: str, paths: list[str], settings: dict, skip_converted: bool) -> dict:
        kb = storage.kb_dir(kb_name)
        if not isinstance(paths, list) or not paths:
            raise StorageError("请至少选择一个 PDF")
        if len(paths) > MAX_ITEMS:
            raise StorageError(f"一次最多提交 {MAX_ITEMS} 个文件")
        settings = storage.normalize_settings(settings)
        try:
            parsers.ensure_available(settings["convert"]["parser"])
        except ValueError as exc:
            raise StorageError(str(exc)) from exc
        converted = {f["path"] for f in storage.scan(kb)["files"] if f["converted"]}
        active = self.active_paths(kb.name)  # 已在其他批次中排队或解析的文件不重复解析
        items, seen = [], set()
        for raw in paths:
            rel = storage.clean_rel(raw)
            if rel in seen:
                continue
            seen.add(rel)
            storage.pdf_path(kb, rel)  # 不存在或不是 PDF 时直接报错
            if rel in active:
                items.append({"path": rel, "status": "skipped", "message": "已在其他批次中排队或解析，不重复处理"})
                continue
            skip = skip_converted and rel in converted
            items.append({"path": rel, "status": "skipped" if skip else "queued",
                          "message": "已有解析结果，按设置跳过" if skip else None})
        now = time.time()
        batch = {
            "id": storage.new_id(), "kb": kb.name, "created": now, "updated": now,
            "settings": settings, "skip_converted": skip_converted, "items": items,
        }
        self._refresh_status(batch)
        self._persist(batch)
        with self._cond:
            self._batches[batch["id"]] = batch
            self._cond.notify_all()
        self._prune(kb)
        return self.public(batch)

    def get(self, kb_name: str, batch_id: str) -> dict:
        batch = self._batches.get(batch_id)
        if not batch or batch["kb"] != storage.validate_kb_name(kb_name):
            raise StorageError("批次不存在", 404)
        return batch

    def list(self, kb_name: str, limit: int = 20) -> list[dict]:
        name = storage.validate_kb_name(kb_name)
        with self._cond:
            mine = [b for b in self._batches.values() if b["kb"] == name]
        mine.sort(key=lambda b: b["created"], reverse=True)
        return [self.public(b, items=False) for b in mine[:limit]]

    def cancel(self, kb_name: str, batch_id: str) -> dict:
        batch = self.get(kb_name, batch_id)
        with self._cond:
            for item in batch["items"]:
                if item["status"] == "queued":
                    item["status"] = "cancelled"
            batch["cancelled"] = time.time()
            self._refresh_status(batch)
        self._persist(batch)
        return self.public(batch)

    def retry(self, kb_name: str, batch_id: str) -> dict:
        batch = self.get(kb_name, batch_id)
        with self._cond:
            for item in batch["items"]:
                if item["status"] in ("error", "cancelled"):
                    item.update(status="queued", error=None, message=None)
            batch.pop("cancelled", None)
            self._refresh_status(batch)
            self._cond.notify_all()
        self._persist(batch)
        return self.public(batch)

    def stats(self) -> dict:
        """所有知识库的批量队列概况，供健康检查使用。"""
        with self._cond:
            running = [b for b in self._batches.values() if b["status"] == "running"]
            counts = {"queued": 0, "running": 0}
            for batch in running:
                for item in batch["items"]:
                    if item["status"] in counts:
                        counts[item["status"]] += 1
        return {"batches": len(running), "concurrency": self._concurrency, **counts}

    def active_paths(self, kb_name: str) -> dict[str, str]:
        """知识库中正在排队或解析的文件 → 状态，供文件列表显示。"""
        out = {}
        with self._cond:
            for batch in self._batches.values():
                if batch["kb"] != kb_name:
                    continue
                for item in batch["items"]:
                    if item["status"] in ACTIVE:
                        out[item["path"]] = item["status"]
        return out

    # ------------------------------------------------------------ 展示

    @staticmethod
    def _counts(batch: dict) -> dict:
        counts = {k: 0 for k in ACTIVE + FINAL}
        for item in batch["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return counts

    def _refresh_status(self, batch: dict) -> None:
        counts = self._counts(batch)
        if counts["running"] or counts["queued"]:
            status = "running"
        elif batch.get("cancelled"):
            status = "cancelled"
        else:
            status = "done"
        batch["status"] = status
        batch["counts"] = counts
        batch["updated"] = time.time()

    def public(self, batch: dict, items: bool = True) -> dict:
        data = {k: batch.get(k) for k in ("id", "kb", "created", "updated", "status", "counts", "settings",
                                          "skip_converted")}
        data["total"] = len(batch["items"])
        if items:
            data["items"] = [dict(item) for item in batch["items"]]
        return data

    # ------------------------------------------------------------ 持久化

    def _persist(self, batch: dict) -> None:
        # 同一批次可能有多个工作线程同时更新条目：在锁内生成快照，避免序列化到一半时字典被修改
        with self._cond:
            data = json.dumps({k: v for k, v in batch.items() if k != "kb"}, ensure_ascii=False, indent=1).encode()
        try:
            kb = storage.kb_dir(batch["kb"])
            folder = _batch_dir(kb)
            folder.mkdir(parents=True, exist_ok=True)
            tmp = storage._write_tmp(kb, data)
            os.replace(tmp, folder / f"{batch['id']}.json")
        except (OSError, StorageError):
            traceback.print_exc()

    def _prune(self, kb: Path) -> None:
        """每个知识库只保留最近的批次记录；未完成的批次不删除。"""
        with self._cond:
            finished = sorted((b for b in self._batches.values() if b["kb"] == kb.name and b["status"] != "running"),
                              key=lambda b: b["created"], reverse=True)
            for batch in finished[KEEP_BATCHES:]:
                self._batches.pop(batch["id"], None)
                (_batch_dir(kb) / f"{batch['id']}.json").unlink(missing_ok=True)

    # ------------------------------------------------------------ 执行

    def _next(self) -> tuple[dict, dict] | None:
        """按「最久没被服务」的顺序在批次间轮转，取下一个排队的文件。调用时持有锁。"""
        candidates = [b for b in self._batches.values() if b["status"] == "running"]
        candidates.sort(key=lambda b: (self._served.get(b["id"], 0), b["created"]))
        for batch in candidates:
            for item in batch["items"]:
                if item["status"] == "queued":
                    item["status"] = "running"
                    item["started"] = time.time()
                    self._served[batch["id"]] = time.time()
                    self._refresh_status(batch)
                    return batch, item
        return None

    def _worker(self, generation: int) -> None:
        while True:
            with self._cond:
                picked = None
                while generation == self._generation and (picked := self._next()) is None:
                    self._cond.wait(timeout=5)
                if generation != self._generation:
                    return
            batch, item = picked
            try:
                self._persist(batch)
                self._run_item(batch, item)
            except Exception:  # noqa: BLE001  任何意外都不能让工作线程退出，否则批量处理会停住
                traceback.print_exc()
                with self._cond:
                    if item["status"] == "running":
                        item.update(status="error", error="内部错误，请重试", finished=time.time())
                        self._refresh_status(batch)

    def _run_item(self, batch: dict, item: dict) -> None:
        kb_name = batch["kb"]
        settings = batch["settings"]
        updates: dict = {}
        try:
            kb = storage.kb_dir(kb_name)
            pdf = storage.pdf_path(kb, item["path"])
            options = {**settings["convert"], **settings["chunk"]}
            started = time.time()
            out = self._convert(str(pdf), options)
            result = out["result"]
            meta = {
                "origin": "batch", "batch_id": batch["id"],
                "convert": asdict(pipeline.ConvertOptions.from_dict(settings["convert"])),
                "chunk": asdict(chunker.ChunkOptions.from_dict(settings["chunk"])),
                "doc_stats": result.get("doc_stats", {}),
                "chunks": (result.get("stats") or {}).get("chunks"),
                "warnings": result.get("warnings", []),
                "elapsed": round(time.time() - started, 1),
            }
            saved = storage.save_new_version(kb, item["path"], result["markdown"], meta)
            stats = result.get("doc_stats", {})
            updates = {"status": "done", "version_path": saved["path"], "version": saved["version"],
                       "pages": stats.get("pages"), "ocr_pages": stats.get("ocr_pages"),
                       "chunks": meta["chunks"], "warnings": len(meta["warnings"]), "elapsed": meta["elapsed"]}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            updates = {"status": "error", "error": str(exc) or exc.__class__.__name__}
        if updates.get("status") == "done" and self.after_save:
            # 解析结果已保存；后续步骤（自动导入）失败不影响解析状态
            try:
                outcome = self.after_save(kb_name, item["path"])
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                outcome = {"status": "error", "message": str(exc) or exc.__class__.__name__}
            if outcome:
                updates["fastgpt"] = outcome
        with self._cond:
            item.update(updates, finished=time.time())
            self._refresh_status(batch)
        self._persist(batch)

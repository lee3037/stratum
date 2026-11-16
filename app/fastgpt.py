"""FastGPT 对接：把知识库中已保存的 Markdown 版本导入绑定的 FastGPT 知识库。

- 地址与密钥只在服务端配置（STRATUM_FASTGPT_URL / STRATUM_FASTGPT_API_KEY），浏览器拿不到。
- 每个 Stratum 知识库绑定一个 FastGPT 知识库（datasetId），记录在 `.stratum/settings.json` 的 fastgpt 字段。
- 导入记录在 `.stratum/fastgpt.json`：每个 PDF 导入了哪个版本、对应的集合 ID。
- 导入新版本时先创建新集合，成功后再删除旧集合；中途失败也不会让知识库里少一份文档。
- 分块方式使用自定义分隔符，分隔符取自该版本保存时的分块参数，与 Markdown 中的分块一一对应。
  FastGPT 文档对分块参数写得不完整，具体字段可以用 deploy/fastgpt_probe.py 对照实际实例确认，
  并通过 STRATUM_FASTGPT_SPLIT_MODE / STRATUM_FASTGPT_CHUNK_SIZE / STRATUM_FASTGPT_INDEX_SIZE 调整。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

from . import storage
from .storage import StorageError

RECORDS = "fastgpt.json"
_records_lock = threading.Lock()
# 同一个 PDF 同时只允许一个导入：两人同时点导入时，后到的一方等前一个完成后看到「已导入」，不会重复建集合
_pdf_locks: dict[tuple[str, str], threading.Lock] = {}
_pdf_locks_guard = threading.Lock()


def _pdf_lock(kb: Path, pdf_rel: str) -> threading.Lock:
    with _pdf_locks_guard:
        return _pdf_locks.setdefault((str(kb), pdf_rel), threading.Lock())


class FastGPTError(StorageError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message, status)


# ---------------------------------------------------------------- 配置

def base_url() -> str:
    return os.environ.get("STRATUM_FASTGPT_URL", "").strip().rstrip("/")


def configured() -> bool:
    return bool(base_url() and os.environ.get("STRATUM_FASTGPT_API_KEY", "").strip())


def _timeout() -> float:
    try:
        return max(5.0, float(os.environ.get("STRATUM_FASTGPT_TIMEOUT", "120")))
    except ValueError:
        return 120.0


def chunk_payload(separator: str, max_chars: int) -> dict:
    """create/text 的分块参数：自定义分隔符优先，chunkSize 留足余量，避免把我们的块再切开。"""
    mode = os.environ.get("STRATUM_FASTGPT_SPLIT_MODE", "char").strip() or "char"
    try:
        chunk_size = int(os.environ.get("STRATUM_FASTGPT_CHUNK_SIZE") or 0)
    except ValueError:
        chunk_size = 0
    payload = {
        "trainingType": "chunk",
        "chunkSettingMode": "custom",
        "chunkSplitMode": mode,
        "chunkSplitter": separator,
        # 中文 1 字约 1 token 以上；按最大字数的 2 倍给出上限，由分隔符决定实际分块
        "chunkSize": chunk_size or max(1500, max_chars * 2),
        "indexPrefixTitle": False,
    }
    index_size = os.environ.get("STRATUM_FASTGPT_INDEX_SIZE", "").strip()
    if index_size.isdigit():
        payload["indexSize"] = int(index_size)
    return payload


# ---------------------------------------------------------------- HTTP

class Client:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport

    def _request(self, method: str, path: str, *, params=None, body=None) -> object:
        if not configured() and self._transport is None:
            raise FastGPTError("服务端未配置 FastGPT 地址或 API 密钥（STRATUM_FASTGPT_URL / STRATUM_FASTGPT_API_KEY）", 400)
        url = (base_url() or "http://fastgpt") + path
        headers = {"Authorization": f"Bearer {os.environ.get('STRATUM_FASTGPT_API_KEY', '').strip()}"}
        timeout = _timeout()
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 15)), transport=self._transport) as client:
                response = client.request(method, url, params=params, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise FastGPTError(f"FastGPT 请求超时（{timeout:g} 秒）") from exc
        except httpx.RequestError as exc:
            raise FastGPTError("无法连接 FastGPT，请检查 STRATUM_FASTGPT_URL 与网络") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("statusText") or "")
        if response.status_code in (401, 403):
            raise FastGPTError(f"FastGPT 拒绝访问（HTTP {response.status_code}）：请检查 API 密钥及其对该知识库的权限。{message}")
        if response.status_code == 404:
            raise FastGPTError(f"FastGPT 接口不存在（{path}），请确认版本与地址。{message}", 502)
        if response.status_code >= 400:
            raise FastGPTError(f"FastGPT 返回错误（HTTP {response.status_code}）：{message or response.text[:200]}")
        if not isinstance(payload, dict):
            raise FastGPTError("FastGPT 响应不是 JSON")
        code = payload.get("code")
        if code not in (None, 0, 200):
            raise FastGPTError(f"FastGPT 返回错误（{code}）：{message}")
        return payload.get("data")

    # ------------------------------------------------------------ 知识库与集合

    def list_datasets(self) -> list[dict]:
        """列出 API 密钥可访问的全部知识库，展开文件夹，name 带文件夹路径。"""
        out: list[dict] = []

        def fetch(parent_id, prefix: str, depth: int):
            try:
                data = self._request("POST", "/api/core/dataset/listV2", body={"parentId": parent_id})
            except FastGPTError as exc:
                if "接口不存在" not in str(exc):
                    raise
                data = self._request("POST", "/api/core/dataset/list", body={"parentId": parent_id})
            items = data.get("list") if isinstance(data, dict) else data
            for item in items or []:
                if not isinstance(item, dict) or not item.get("_id"):
                    continue
                name = f"{prefix}{item.get('name', '')}"
                if item.get("type") == "folder":
                    if depth < 4:
                        fetch(item["_id"], f"{name} / ", depth + 1)
                    continue
                out.append({"id": item["_id"], "name": name, "type": item.get("type", "dataset"),
                            "vector_model": (item.get("vectorModel") or {}).get("name")
                            if isinstance(item.get("vectorModel"), dict) else item.get("vectorModel")})

        fetch(None, "", 0)
        return sorted(out, key=lambda d: storage.natural_key(d["name"]))

    def dataset_detail(self, dataset_id: str) -> dict:
        data = self._request("GET", "/api/core/dataset/detail", params={"id": dataset_id})
        if not isinstance(data, dict):
            raise FastGPTError("FastGPT 知识库详情格式无效")
        return data

    def find_collections(self, dataset_id: str, name: str) -> list[dict]:
        data = self._request("POST", "/api/core/dataset/collection/listV2", body={
            "datasetId": dataset_id, "parentId": None, "searchText": name, "offset": 0, "pageSize": 100})
        items = data.get("list") if isinstance(data, dict) else data
        return [i for i in items or [] if isinstance(i, dict) and i.get("name") == name and i.get("type") != "folder"]

    def create_text(self, dataset_id: str, name: str, text: str, separator: str, max_chars: int,
                    metadata: dict | None = None) -> dict:
        body = {"datasetId": dataset_id, "parentId": None, "name": name, "text": text,
                **chunk_payload(separator, max_chars), "metadata": metadata or {}}
        data = self._request("POST", "/api/core/dataset/collection/create/text", body=body)
        if not isinstance(data, dict) or not data.get("collectionId"):
            raise FastGPTError("FastGPT 没有返回集合 ID")
        results = data.get("results") if isinstance(data.get("results"), dict) else {}
        return {"collection_id": data["collectionId"], "chunks": data.get("insertLen", results.get("insertLen"))}

    def delete_collection(self, collection_id: str) -> None:
        try:
            self._request("POST", "/api/core/dataset/collection/delete", params={"id": collection_id},
                          body={"collectionIds": [collection_id]})
        except FastGPTError as exc:
            if "接口不存在" not in str(exc):
                raise
            self._request("DELETE", "/api/core/dataset/collection/delete", params={"id": collection_id})


# ---------------------------------------------------------------- 绑定与记录

def binding(kb: Path) -> dict | None:
    data = storage.read_settings(kb).get("fastgpt")
    return data if isinstance(data, dict) and data.get("dataset_id") else None


def set_binding(kb: Path, dataset_id: str | None, dataset_name: str | None) -> dict | None:
    def apply(current: dict) -> dict:
        if dataset_id:
            current["fastgpt"] = {"dataset_id": dataset_id, "dataset_name": dataset_name or dataset_id,
                                  "bound": time.time()}
        else:
            current.pop("fastgpt", None)
        return current
    storage.update_json(kb, kb / storage.META_DIR / "settings.json", apply)
    return binding(kb)


AUTO_IMPORT_KEY = "fastgpt_auto_import"


def auto_import_enabled(kb: Path) -> bool:
    """保存（单个保存或批量解析完成）后是否自动导入；需同时已绑定 FastGPT 知识库。"""
    return bool(storage.read_settings(kb).get(AUTO_IMPORT_KEY)) and binding(kb) is not None


def set_auto_import(kb: Path, enabled: bool) -> None:
    def apply(current: dict) -> dict:
        current[AUTO_IMPORT_KEY] = bool(enabled)
        return current
    storage.update_json(kb, kb / storage.META_DIR / "settings.json", apply)


def _records_path(kb: Path) -> Path:
    return kb / storage.META_DIR / RECORDS


def read_records(kb: Path, dataset_id: str | None) -> dict:
    """{pdf_path: 记录}；记录按 FastGPT 知识库区分，换绑后旧记录不影响新知识库的状态。"""
    if not dataset_id:
        return {}
    data = storage._read_json(_records_path(kb), {})
    records = data.get(dataset_id) if isinstance(data, dict) else None
    return records if isinstance(records, dict) else {}


def _update_record(kb: Path, dataset_id: str, pdf_rel: str, record: dict | None) -> None:
    with _records_lock:
        data = storage._read_json(_records_path(kb), {})
        if not isinstance(data, dict):
            data = {}
        bucket = data.setdefault(dataset_id, {})
        if record is None:
            bucket.pop(pdf_rel, None)
        else:
            bucket[pdf_rel] = record
        storage._write_json(kb, _records_path(kb), data)


def collection_name(pdf_rel: str) -> str:
    return re.sub(r"\.pdf$", "", pdf_rel, flags=re.I) + ".md"


def status_summary(kb: Path) -> dict:
    """文件列表使用的导入状态：只返回当前绑定知识库的记录。"""
    bound = binding(kb)
    records = read_records(kb, bound["dataset_id"]) if bound else {}
    return {
        "configured": configured(),
        "dataset": {"id": bound["dataset_id"], "name": bound.get("dataset_name")} if bound else None,
        "auto_import": bool(storage.read_settings(kb).get(AUTO_IMPORT_KEY)),
        "imports": {pdf: {k: rec.get(k) for k in ("version", "md_path", "imported", "chunks", "error", "md_etag")}
                    for pdf, rec in records.items()},
        # 有新版本、导入后又修改过、或上次失败的 PDF
        "pending": needs_import(kb) if bound else [],
    }


# ---------------------------------------------------------------- 导入

def _latest_version(kb: Path, pdf_rel: str) -> dict | None:
    versions = storage.versions_of(kb, pdf_rel)
    return versions[-1] if versions else None


def import_one(client: Client, kb: Path, pdf_rel: str, md_rel: str | None = None,
               on_conflict: str = "ask", force: bool = False) -> dict:
    """导入一个 PDF 的某个版本（缺省为最新版本）。返回 {status, ...}：

    imported 已导入；unchanged 该版本已导入且未修改；conflict FastGPT 中已有同名集合（不是 Stratum 导入的），
    需要调用方选择 replace（删除后导入）或 keep（保留并新增）；error 失败（同时写入记录，便于界面显示）。
    """
    pdf_rel = storage.clean_rel(pdf_rel)
    with _pdf_lock(kb, pdf_rel):
        return _import_one(client, kb, pdf_rel, md_rel, on_conflict, force)


def _import_one(client: Client, kb: Path, pdf_rel: str, md_rel: str | None, on_conflict: str, force: bool) -> dict:
    bound = binding(kb)
    if not bound:
        raise StorageError("当前知识库还没有绑定 FastGPT 知识库，请先在「知识库设置」中绑定")
    dataset_id = bound["dataset_id"]
    if md_rel:
        md_rel = storage.clean_rel(md_rel)
        version = next((v for v in storage.versions_of(kb, pdf_rel) if v["path"] == md_rel), None)
        if not version:
            raise StorageError(f"{md_rel} 不是 {pdf_rel} 的已保存版本", 404)
    else:
        version = _latest_version(kb, pdf_rel)
        if not version:
            return {"pdf_path": pdf_rel, "status": "error", "message": "还没有已保存的解析结果"}
    doc = storage.read_markdown(kb, version["path"])
    records = read_records(kb, dataset_id)
    previous = records.get(pdf_rel) or {}
    if (not force and previous.get("collection_id") and previous.get("md_path") == version["path"]
            and previous.get("md_etag") == doc["etag"] and not previous.get("error")):
        return {"pdf_path": pdf_rel, "status": "unchanged", "md_path": version["path"],
                "message": "该版本已导入，内容没有变化"}

    meta = doc["meta"]
    settings = storage.read_settings(kb)["chunk"]
    chunk = {**settings, **(meta.get("chunk") or {})}
    separator = chunk.get("separator") or settings["separator"]
    lines = set(doc["markdown"].splitlines())
    if separator not in lines and settings["separator"] in lines:
        separator = settings["separator"]  # 版本记录的分隔符与内容不符时，以内容为准
    name = collection_name(pdf_rel)
    replace_ids: list[str] = []
    try:
        if not previous.get("collection_id"):
            existing = client.find_collections(dataset_id, name)
            if existing:
                if on_conflict == "ask":
                    return {"pdf_path": pdf_rel, "status": "conflict", "md_path": version["path"], "name": name,
                            "message": f"FastGPT 知识库中已有同名集合「{name}」（不是通过 Stratum 导入的）"}
                if on_conflict == "replace":
                    replace_ids = [c["_id"] for c in existing if c.get("_id")]
                elif on_conflict == "skip":
                    return {"pdf_path": pdf_rel, "status": "skipped", "md_path": version["path"],
                            "message": f"已有同名集合「{name}」，按设置跳过"}
        created = client.create_text(dataset_id, name, doc["markdown"], separator, int(chunk.get("max_chars") or 1000),
                                     metadata={"source": "stratum", "pdf": pdf_rel, "version": version["version"]})
    except StorageError as exc:
        _update_record(kb, dataset_id, pdf_rel, {**previous, "error": str(exc), "failed": time.time()})
        return {"pdf_path": pdf_rel, "status": "error", "md_path": version["path"], "message": str(exc)}

    warnings = []
    old_ids = [i for i in [previous.get("collection_id"), *replace_ids] if i and i != created["collection_id"]]
    for old in old_ids:
        try:
            client.delete_collection(old)
        except StorageError as exc:
            warnings.append(f"新集合已创建，但删除旧集合 {old} 失败：{exc}")
    record = {
        "version": version["version"], "md_path": version["path"], "md_etag": doc["etag"],
        "collection_id": created["collection_id"], "collection_name": name, "chunks": created["chunks"],
        "separator": separator, "imported": time.time(), "error": None,
    }
    _update_record(kb, dataset_id, pdf_rel, record)
    return {"pdf_path": pdf_rel, "status": "imported", "md_path": version["path"], "chunks": created["chunks"],
            "replaced": len(old_ids), "message": "；".join(warnings) or None}


def auto_import(client: Client, kb: Path, pdf_rel: str) -> dict | None:
    """批量解析保存后调用：开启了自动导入时导入最新版本，否则返回 None。
    无人值守，遇到不是本系统导入的同名集合时跳过（结果里注明），不会删除别人的数据。"""
    if not configured() or not auto_import_enabled(kb):
        return None
    try:
        result = import_one(client, kb, pdf_rel, None, "skip")
    except StorageError as exc:
        result = {"status": "error", "message": str(exc)}
    return {"status": result["status"], "message": result.get("message"), "chunks": result.get("chunks")}


def needs_import(kb: Path) -> list[str]:
    """有已保存版本、且最新版本尚未导入（或导入后又修改过、或上次失败）的 PDF。"""
    bound = binding(kb)
    if not bound:
        return []
    records = read_records(kb, bound["dataset_id"])
    inventory = storage.scan(kb)
    latest: dict[str, dict] = {}
    for artifact in inventory["artifacts"]:
        if artifact["pdf_path"] in {f["path"] for f in inventory["files"]}:
            cur = latest.get(artifact["pdf_path"])
            if not cur or artifact["version"] > cur["version"]:
                latest[artifact["pdf_path"]] = artifact
    out = []
    for pdf, artifact in latest.items():
        rec = records.get(pdf) or {}
        etag = storage.etag(storage.resolve(kb, artifact["path"]))
        if rec.get("error") or rec.get("md_path") != artifact["path"] or rec.get("md_etag") != etag:
            out.append(pdf)
    return sorted(out, key=storage.natural_key)


# ---------------------------------------------------------------- 批量导入任务（内存中）

class ImportJobs:
    def __init__(self, client_factory=Client):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._client_factory = client_factory

    def start(self, kb_name: str, paths: list[str], on_conflict: str, force: bool) -> dict:
        kb = storage.kb_dir(kb_name)
        if not binding(kb):
            raise StorageError("当前知识库还没有绑定 FastGPT 知识库，请先在「知识库设置」中绑定")
        if on_conflict not in ("ask", "replace", "keep", "skip"):
            raise StorageError("on_conflict 取值无效")
        paths = [storage.clean_rel(p) for p in paths] if paths else needs_import(kb)
        paths = list(dict.fromkeys(paths))
        job = {"id": storage.new_id(), "kb": kb.name, "created": time.time(), "status": "running",
               "items": [{"pdf_path": p, "status": "queued"} for p in paths]}
        if not paths:
            job["status"] = "done"
        with self._lock:
            self._jobs[job["id"]] = job
            # 只保留最近 50 个任务
            for old in sorted(self._jobs.values(), key=lambda j: j["created"])[:-50]:
                self._jobs.pop(old["id"], None)
        if paths:
            threading.Thread(target=self._run, args=(job, on_conflict, force), daemon=True).start()
        return self.public(job)

    def _run(self, job: dict, on_conflict: str, force: bool) -> None:
        client = self._client_factory()
        kb = storage.kb_dir(job["kb"])
        for item in job["items"]:
            if job.get("cancelled"):
                item["status"] = "cancelled"
                continue
            item["status"] = "running"
            try:
                # 批量导入不能逐个询问：遇到同名集合时按 on_conflict 处理，ask 视为跳过并报告
                result = import_one(client, kb, item["pdf_path"], None,
                                    "skip" if on_conflict == "ask" else on_conflict, force)
            except StorageError as exc:
                result = {"status": "error", "message": str(exc)}
            item.update(result)
        job["status"] = "done"
        job["finished"] = time.time()

    def get(self, kb_name: str, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if not job or job["kb"] != storage.validate_kb_name(kb_name):
            raise StorageError("导入任务不存在", 404)
        return self.public(job)

    @staticmethod
    def public(job: dict) -> dict:
        counts: dict[str, int] = {}
        for item in job["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {**{k: job[k] for k in ("id", "kb", "created", "status")}, "total": len(job["items"]),
                "counts": counts, "items": [dict(i) for i in job["items"]]}


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)

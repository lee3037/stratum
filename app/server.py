"""Web 服务：知识库文件管理 → 异步转换（进度可查）→ 校对 → 保存版本。"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chunker, exports, extract, fastgpt, parsers, pipeline, storage
from .scheduler import BATCH, INTERACTIVE, PriorityPool
from .batches import BatchManager
from .storage import StorageError

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _default_workers() -> int:
    """小机器保持原来的 1–3 个进程；多核服务器按核数的 1/4 放开，最多 16 个，给其他服务留出余量。"""
    cpus = os.cpu_count() or 2
    if cpus <= 8:
        return max(1, min(3, cpus // 2))
    return max(3, min(16, cpus // 4))


WORKERS = int(os.environ.get("STRATUM_WORKERS") or _default_workers())
MAX_UPLOAD_MB = int(os.environ.get("STRATUM_MAX_MB", "200"))
MAX_PAGES = int(os.environ.get("STRATUM_MAX_PAGES", "2000"))
JOB_TTL = int(os.environ.get("STRATUM_JOB_TTL", str(3 * 3600)))
# 显式配置的工作目录只清理本服务创建的任务子目录，不删除目录本身
_WORK_DIR_CONFIGURED = bool(os.environ.get("STRATUM_WORK_DIR"))
WORK_DIR = Path(os.environ.get("STRATUM_WORK_DIR") or tempfile.mkdtemp(prefix="stratum-"))

# 同时进行的批量解析文件数；每个文件内部的页面仍由全部解析进程并行处理
BATCH_CONCURRENCY = int(os.environ.get("STRATUM_BATCH_CONCURRENCY") or max(1, min(4, WORKERS // 6)))
# 同时进行的单个解析任务上限；超出时返回 429，提示稍后再试（批量解析不受此限制，它有自己的队列）
MAX_JOBS = int(os.environ.get("STRATUM_MAX_JOBS") or max(8, WORKERS * 2))
EXPORT_TTL = 3600

_runner = ThreadPoolExecutor(max_workers=MAX_JOBS, thread_name_prefix="job")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _new_pool() -> ProcessPoolExecutor:
    """创建解析进程池。由调度器在首次需要时调用；进程意外退出导致进程池失效时，调度器会再次调用以重建。"""
    os.environ.setdefault("STRATUM_OCR_THREADS", str(max(1, (os.cpu_count() or 2) // WORKERS)))
    return ProcessPoolExecutor(max_workers=WORKERS, mp_context=mp.get_context("spawn"),
                               initializer=extract.worker_init)


# 所有页面分组都经过调度器：单个解析优先于批量解析，同优先级的文件轮流执行
scheduler = PriorityPool(lambda: _new_pool(), WORKERS)


@asynccontextmanager
async def lifespan(_app):
    scheduler.start()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        storage.ensure_root()
    except OSError as exc:  # 存储不可用时服务仍然启动，健康检查会报告原因
        print(f"[stratum] 文件库目录不可用：{storage.data_dir()}：{exc}")
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    try:
        batches.start()
    except (OSError, StorageError) as exc:
        print(f"[stratum] 无法恢复批量任务：{exc}")
    yield
    batches.stop()
    scheduler.shutdown()
    if _WORK_DIR_CONFIGURED:
        for job in list(_jobs.values()):
            if job.get("dir"):
                shutil.rmtree(job["dir"], ignore_errors=True)
    else:
        shutil.rmtree(WORK_DIR, ignore_errors=True)


app = FastAPI(title="Stratum 层析", lifespan=lifespan)


@app.exception_handler(StorageError)
async def _storage_error(_request: Request, exc: StorageError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


def _cleanup_loop():
    while True:
        time.sleep(600)
        now = time.time()
        with _lock:
            expired = [k for k, j in _jobs.items() if now - j["updated"] > JOB_TTL and j["status"] in ("done", "error")]
            for k in expired:
                job = _jobs.pop(k)
                if job.get("dir"):
                    shutil.rmtree(job["dir"], ignore_errors=True)
        try:
            for kb in storage.list_kbs():
                storage.cleanup_tmp(storage.kb_dir(kb["name"]))
        except (OSError, StorageError):
            pass
        for export_id, info in list(_exports.items()):
            if now - info["created"] > EXPORT_TTL:
                _exports.pop(export_id, None)
                Path(info["file"]).unlink(missing_ok=True)


def _convert(pdf: str, conv, copts, priority: int, label: str, progress=None) -> dict:
    backend = parsers.ensure_available(conv.parser)
    if not backend.uses_local_workers:
        return pipeline.convert(pdf, conv, copts, None, WORKERS, progress)
    with scheduler.session(priority, label) as executor:
        return pipeline.convert(pdf, conv, copts, executor, WORKERS, progress)


def _batch_convert(pdf: str, options: dict) -> dict:
    """批量解析单个文件：与交互解析共用解析进程，但优先级更低。"""
    _page_count(Path(pdf))
    conv = pipeline.ConvertOptions.from_dict(options)
    copts = chunker.ChunkOptions.from_dict(options)
    return _convert(pdf, conv, copts, BATCH, "batch")


batches = BatchManager(_batch_convert, BATCH_CONCURRENCY)
fastgpt_client = fastgpt.Client()
imports = fastgpt.ImportJobs(lambda: fastgpt_client)
batches.after_save = lambda kb, pdf: fastgpt.auto_import(fastgpt_client, storage.kb_dir(kb), pdf)
_exports: dict[str, dict] = {}


def _update(job_id, **kw):
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job.update(kw, updated=time.time())


def _run_job(job_id: str):
    job = _jobs[job_id]
    try:
        _update(job_id, status="running", stage="版面分析")

        def progress(done, total):
            _update(job_id, progress={"done": done, "total": total})

        conv = pipeline.ConvertOptions.from_dict(job["convert_options"])
        copts = chunker.ChunkOptions.from_dict(job["chunk_options"])
        out = _convert(job["pdf"], conv, copts, INTERACTIVE, job_id, progress)
        _update(job_id, status="done", stage="完成", elements=out["elements"], result=out["result"],
                source_warnings=out["source_warnings"],
                elapsed=round(time.time() - job["created"], 1))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        msg = str(e) or e.__class__.__name__
        _update(job_id, status="error", stage="失败", error=msg)


def _public(job: dict, with_result=True) -> dict:
    data = {k: job.get(k) for k in ("id", "filename", "status", "stage", "progress", "error", "elapsed", "page_count",
                                    "kb", "path")}
    if with_result and job.get("result"):
        data["result"] = job["result"]
    return data


def _validate_options(opts) -> dict:
    if not isinstance(opts, dict):
        raise HTTPException(400, "options 必须是 JSON 对象")
    try:
        conv = pipeline.ConvertOptions.from_dict(opts)
        chunker.ChunkOptions.from_dict(opts)
        parsers.ensure_available(conv.parser)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return opts


def _page_count(pdf: Path) -> int:
    try:
        info = extract.document_info(str(pdf))
    except Exception as e:  # noqa: BLE001
        raise StorageError(f"无法打开 PDF：{e}") from e
    if info["page_count"] > MAX_PAGES:
        raise StorageError(f"页数 {info['page_count']} 超过上限 {MAX_PAGES}")
    return info["page_count"]


def _active_jobs() -> int:
    return sum(1 for j in _jobs.values() if j["status"] in ("queued", "running"))


def _check_capacity() -> None:
    if _active_jobs() >= MAX_JOBS:
        raise HTTPException(429, f"当前正在解析的文件较多（{MAX_JOBS} 个），请稍后再试")


def _submit(pdf: Path, filename: str, opts: dict, page_count: int, job_dir: Path | None = None,
            kb: str | None = None, path: str | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    job = {
        "id": job_id, "filename": filename, "dir": str(job_dir) if job_dir else None, "pdf": str(pdf),
        "kb": kb, "path": path,
        "status": "queued", "stage": "排队中", "progress": {"done": 0, "total": page_count},
        "page_count": page_count, "convert_options": opts, "chunk_options": opts,
        "created": now, "updated": now,
    }
    with _lock:
        _jobs[job_id] = job
    _runner.submit(_run_job, job_id)
    return job_id


# ---------------------------------------------------------------- 任务（直接上传，保留兼容）

@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...), options: str = Form("{}")):
    try:
        opts = json.loads(options or "{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "options 不是合法的 JSON")
    _validate_options(opts)
    _check_capacity()
    storage.ensure_free_space(WORK_DIR)
    name = Path(file.filename or "document.pdf").name
    job_dir = WORK_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True)
    pdf_path = job_dir / "source.pdf"
    size = 0
    with open(pdf_path, "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_MB << 20:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB} MB 上限")
            f.write(chunk)
    with open(pdf_path, "rb") as f:
        if not f.read(1024).lstrip().startswith(b"%PDF"):
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(400, "上传的文件不是 PDF")
    try:
        page_count = _page_count(pdf_path)
    except StorageError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    job_id = _submit(pdf_path, name, opts, page_count, job_dir)
    return {"id": job_id, "page_count": page_count}


def _job_or_404(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期，请重新解析")
    return job


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _public(_job_or_404(job_id))


class RechunkBody(BaseModel):
    options: dict = {}


@app.post("/api/jobs/{job_id}/rechunk")
def rechunk(job_id: str, body: RechunkBody):
    job = _job_or_404(job_id)
    if job["status"] != "done":
        raise HTTPException(409, "转换尚未完成")
    copts = chunker.ChunkOptions.from_dict(body.options)
    result = chunker.to_markdown(job["elements"], copts)
    old = job["result"]
    result["doc_stats"] = old.get("doc_stats", {})
    result["warnings"] = list(dict.fromkeys(job.get("source_warnings", []) + result["warnings"]))
    _update(job_id, result=result, chunk_options=body.options)
    return result


@app.get("/api/jobs/{job_id}/pdf")
def get_pdf(job_id: str):
    job = _job_or_404(job_id)
    if not Path(job["pdf"]).is_file():
        raise HTTPException(404, "源文件已不存在")
    return FileResponse(job["pdf"], media_type="application/pdf",
                        headers={"Content-Disposition": "inline"})


# ---------------------------------------------------------------- 知识库

class KbBody(BaseModel):
    name: str


@app.get("/api/kbs")
def list_kbs():
    return {"kbs": storage.list_kbs(), "storage": storage.root_status()}


@app.post("/api/kbs")
def create_kb(body: KbBody):
    path = storage.create_kb(body.name)
    return {"name": path.name}


@app.get("/api/kbs/{kb}/settings")
def get_settings(kb: str):
    return storage.read_settings(storage.kb_dir(kb))


@app.put("/api/kbs/{kb}/settings")
def put_settings(kb: str, body: dict):
    return storage.write_settings(storage.kb_dir(kb), body)


@app.get("/api/kbs/{kb}/files")
def list_files(kb: str):
    name = storage.validate_kb_name(kb)
    folder = storage.kb_dir(kb)
    return {"name": name, **storage.scan(folder), "active": batches.active_paths(name),
            "fastgpt": fastgpt.status_summary(folder)}


@app.post("/api/kbs/{kb}/files")
def upload_files(kb: str, files: list[UploadFile] = File(...), paths: list[str] = Form(default=[])):
    """上传一个或多个 PDF。paths 与 files 一一对应，用于保留文件夹结构；缺省时使用文件名。"""
    folder = storage.kb_dir(kb)
    results = []
    for i, upload in enumerate(files):
        rel = paths[i] if i < len(paths) and paths[i] else (upload.filename or "")
        try:
            results.append(storage.store_upload(folder, rel, upload.file, MAX_UPLOAD_MB << 20, check_pdf=_page_count))
        except StorageError as exc:
            results.append({"path": rel, "status": "error", "message": str(exc)})
    return {"results": results}


@app.get("/api/kbs/{kb}/pdf")
def kb_pdf(kb: str, path: str):
    pdf = storage.pdf_path(storage.kb_dir(kb), path)
    return FileResponse(pdf, media_type="application/pdf", headers={"Content-Disposition": "inline"})


@app.get("/api/kbs/{kb}/markdown")
def kb_markdown(kb: str, path: str):
    return storage.read_markdown(storage.kb_dir(kb), path)


class OverwriteBody(BaseModel):
    path: str
    markdown: str
    etag: str | None = None


@app.put("/api/kbs/{kb}/markdown")
def kb_overwrite(kb: str, body: OverwriteBody):
    return storage.overwrite_markdown(storage.kb_dir(kb), body.path, body.markdown, body.etag)


class VersionBody(BaseModel):
    pdf_path: str
    markdown: str
    job_id: str | None = None
    convert: dict | None = None
    chunk: dict | None = None
    version: int | None = None


def _version_meta(job: dict | None, convert: dict | None, chunk: dict | None, origin: str) -> dict:
    meta: dict = {"origin": origin}
    if job:
        meta["job_id"] = job["id"]
        meta["convert"] = asdict_options(pipeline.ConvertOptions, job["convert_options"])
        result = job.get("result") or {}
        meta["doc_stats"] = result.get("doc_stats", {})
        meta["chunks"] = (result.get("stats") or {}).get("chunks")
        meta["warnings"] = result.get("warnings", [])
        meta["elapsed"] = job.get("elapsed")
    if convert:
        meta["convert"] = asdict_options(pipeline.ConvertOptions, convert)
    if chunk:
        meta["chunk"] = asdict_options(chunker.ChunkOptions, chunk)
    elif job:
        meta["chunk"] = asdict_options(chunker.ChunkOptions, job["chunk_options"])
    return meta


def asdict_options(cls, data: dict) -> dict:
    try:
        return asdict(cls.from_dict(data))
    except (ValueError, TypeError):
        return {}


@app.post("/api/kbs/{kb}/versions")
def save_version(kb: str, body: VersionBody):
    """把结果保存为该 PDF 的新版本；版本号由服务端分配。"""
    job = _jobs.get(body.job_id) if body.job_id else None
    meta = _version_meta(job, body.convert, body.chunk, "manual")
    return storage.save_new_version(storage.kb_dir(kb), body.pdf_path, body.markdown, meta, body.version)


class KbJobBody(BaseModel):
    path: str
    options: dict = {}


@app.post("/api/kbs/{kb}/jobs")
def create_kb_job(kb: str, body: KbJobBody):
    """解析知识库中已上传的 PDF，不再重复上传。

    同一文件正在批量解析时拒绝（批量完成后会自动保存）；同一文件、同样参数的单个解析正在进行时，
    返回那个任务（例如刷新页面或两人同时点击），不重复解析。"""
    opts = _validate_options(body.options)
    folder = storage.kb_dir(kb)
    pdf = storage.pdf_path(folder, body.path)
    rel = storage.clean_rel(body.path)
    state = batches.active_paths(folder.name).get(rel)
    if state:
        raise StorageError(f"该文件正在批量解析（{'排队中' if state == 'queued' else '解析中'}），完成后会自动保存，无需再次解析", 409)
    with _lock:
        running = next((j for j in _jobs.values() if j.get("kb") == folder.name and j.get("path") == rel
                        and j["status"] in ("queued", "running") and j["convert_options"] == opts), None)
    if running:
        return {"id": running["id"], "page_count": running["page_count"], "joined": True}
    _check_capacity()
    page_count = _page_count(pdf)
    job_id = _submit(pdf, pdf.name, opts, page_count, kb=folder.name, path=rel)
    return {"id": job_id, "page_count": page_count}


# ---------------------------------------------------------------- 批量解析

class BatchBody(BaseModel):
    paths: list[str]
    skip_converted: bool = True
    settings: dict | None = None


@app.post("/api/kbs/{kb}/batches")
def create_batch(kb: str, body: BatchBody):
    """批量解析所选 PDF；不传 settings 时使用知识库默认参数。结果自动保存为新版本。"""
    folder = storage.kb_dir(kb)
    settings = body.settings if body.settings is not None else storage.read_settings(folder)
    return batches.create(folder.name, body.paths, settings, body.skip_converted)


@app.get("/api/kbs/{kb}/batches")
def list_batches(kb: str):
    storage.kb_dir(kb)
    return {"batches": batches.list(kb)}


@app.get("/api/kbs/{kb}/batches/{batch_id}")
def get_batch(kb: str, batch_id: str):
    return batches.public(batches.get(kb, batch_id))


@app.post("/api/kbs/{kb}/batches/{batch_id}/cancel")
def cancel_batch(kb: str, batch_id: str):
    return batches.cancel(kb, batch_id)


@app.post("/api/kbs/{kb}/batches/{batch_id}/retry")
def retry_batch(kb: str, batch_id: str):
    return batches.retry(kb, batch_id)


# ---------------------------------------------------------------- 打包下载

class ExportBody(BaseModel):
    mode: str = "latest"
    paths: list[str] = []
    with_pdf: bool = False
    batch_id: str | None = None


@app.post("/api/kbs/{kb}/exports")
def create_export(kb: str, body: ExportBody):
    """生成 zip，返回下载编号；浏览器再用 GET 下载，便于下载大文件。"""
    folder = storage.kb_dir(kb)
    batch = batches.get(kb, body.batch_id) if body.batch_id else None
    export_dir = WORK_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    storage.ensure_free_space(export_dir)
    export_id = uuid.uuid4().hex
    target = export_dir / f"{export_id}.zip"
    try:
        stats = exports.build_zip(folder, target, body.mode, body.paths, body.with_pdf, batch)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if not stats["markdown"] and not stats["pdf"]:
        target.unlink(missing_ok=True)
        raise StorageError("所选文件还没有可下载的解析结果")
    name = exports.zip_name(folder.name, body.mode, body.batch_id)
    _exports[export_id] = {"kb": folder.name, "file": str(target), "name": name, "created": time.time()}
    return {"id": export_id, "name": name, **stats}


@app.get("/api/kbs/{kb}/exports/{export_id}")
def download_export(kb: str, export_id: str):
    info = _exports.pop(export_id, None)  # 每个下载编号只能用一次
    if not info or info["kb"] != storage.validate_kb_name(kb) or not Path(info["file"]).is_file():
        raise HTTPException(404, "下载已过期，请重新打包")
    return FileResponse(info["file"], media_type="application/zip", filename=info["name"],
                        background=BackgroundTask(lambda: Path(info["file"]).unlink(missing_ok=True)))


# ---------------------------------------------------------------- FastGPT

@app.get("/api/fastgpt")
def fastgpt_status():
    return {"configured": fastgpt.configured(), "url": fastgpt.base_url() or None}


@app.get("/api/fastgpt/datasets")
def fastgpt_datasets():
    return {"datasets": fastgpt_client.list_datasets()}


_kb_from_dataset_lock = threading.Lock()


@app.post("/api/fastgpt/datasets/{dataset_id}/kb")
def kb_from_dataset(dataset_id: str):
    """为 FastGPT 知识库准备一个 Stratum 知识库并绑定：已有绑定的直接返回；
    有同名且未绑定的知识库就绑定它；否则新建同名知识库（重名时加 (2)）。"""
    detail = fastgpt_client.dataset_detail(dataset_id)
    dataset_name = detail.get("name") or dataset_id
    with _kb_from_dataset_lock:
        kbs = storage.list_kbs()
        for kb in kbs:
            if (kb.get("fastgpt") or {}).get("dataset_id") == dataset_id:
                return {"name": kb["name"], "created": False}
        name = storage.kb_name_from(dataset_name)
        same = next((kb for kb in kbs if kb["name"].lower() == name.lower() and not kb.get("fastgpt")), None)
        if same:
            folder, created = storage.kb_dir(same["name"]), False
        else:
            folder, created = storage.create_kb(storage.free_kb_name(name)), True
        fastgpt.set_binding(folder, dataset_id, dataset_name)
    return {"name": folder.name, "created": created}


@app.get("/api/kbs/{kb}/fastgpt")
def kb_fastgpt(kb: str):
    return fastgpt.status_summary(storage.kb_dir(kb))


class BindBody(BaseModel):
    dataset_id: str | None = None


@app.put("/api/kbs/{kb}/fastgpt")
def bind_fastgpt(kb: str, body: BindBody):
    """绑定 FastGPT 知识库；dataset_id 为空时解除绑定。绑定前确认密钥能访问该知识库。"""
    folder = storage.kb_dir(kb)
    name = None
    if body.dataset_id:
        detail = fastgpt_client.dataset_detail(body.dataset_id)
        name = detail.get("name")
    fastgpt.set_binding(folder, body.dataset_id, name)
    return fastgpt.status_summary(folder)


class AutoImportBody(BaseModel):
    enabled: bool


@app.put("/api/kbs/{kb}/fastgpt/auto-import")
def put_auto_import(kb: str, body: AutoImportBody):
    """解析结果保存后是否自动导入绑定的 FastGPT 知识库。"""
    folder = storage.kb_dir(kb)
    fastgpt.set_auto_import(folder, body.enabled)
    return fastgpt.status_summary(folder)


class ImportBody(BaseModel):
    pdf_path: str
    md_path: str | None = None
    on_conflict: str = "ask"
    force: bool = False


@app.post("/api/kbs/{kb}/fastgpt/import")
def import_one(kb: str, body: ImportBody):
    """导入单个版本（缺省为最新版本）。遇到非 Stratum 导入的同名集合时返回 conflict，由用户选择。"""
    if body.on_conflict not in ("ask", "replace", "keep", "skip"):
        raise HTTPException(400, "on_conflict 取值无效")
    return fastgpt.import_one(fastgpt_client, storage.kb_dir(kb), body.pdf_path, body.md_path,
                              body.on_conflict, body.force)


class ImportJobBody(BaseModel):
    paths: list[str] = []
    on_conflict: str = "skip"
    force: bool = False


@app.post("/api/kbs/{kb}/fastgpt/imports")
def start_imports(kb: str, body: ImportJobBody):
    """批量导入；paths 为空时导入所有「有新版本或尚未导入」的 PDF。"""
    return imports.start(kb, body.paths, body.on_conflict, body.force)


@app.get("/api/kbs/{kb}/fastgpt/imports/{job_id}")
def get_imports(kb: str, job_id: str):
    return imports.get(kb, job_id)


# ---------------------------------------------------------------- 其他

@app.get("/api/defaults")
def defaults():
    return {**asdict(pipeline.ConvertOptions()), **asdict(chunker.ChunkOptions()), "workers": WORKERS,
            "max_upload_mb": MAX_UPLOAD_MB}


@app.get("/api/parsers")
def get_parsers():
    return {"parsers": parsers.catalog()}


@app.get("/api/health")
def health():
    """运维检查：进程池负载、排队情况、磁盘空间。"""
    return JSONResponse({"ok": True, "jobs": len(_jobs), "active_jobs": _active_jobs(), "max_jobs": MAX_JOBS,
                         "workers": WORKERS, "batch_concurrency": BATCH_CONCURRENCY, "pool": scheduler.stats(),
                         "batches": batches.stats(), "storage": storage.root_status()})


class _StaticFiles(StaticFiles):
    """首页每次都向服务器确认，更新部署后浏览器立即拿到新版；
    assets/ 下的文件名带内容哈希，可以长期缓存。"""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if str(full_path).endswith(".html"):
            response.headers["Cache-Control"] = "no-cache"
        elif "/assets/" in str(full_path).replace("\\", "/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.mount("/", _StaticFiles(directory=STATIC_DIR, html=True), name="static")

"""服务端文件库：根目录下每个子目录是一个知识库，PDF 与各版本 Markdown 放在一起。

    <STRATUM_DATA_DIR>/<知识库>/合同/a.pdf
    <STRATUM_DATA_DIR>/<知识库>/合同/a_v1.md
    <STRATUM_DATA_DIR>/<知识库>/.stratum/        设置、版本元数据、临时文件（不出现在文件列表中）

所有对外路径都是相对知识库根目录、以 `/` 分隔的字符串；进入文件系统前统一经过 `resolve()` 校验，
拒绝绝对路径、`..`、隐藏段和指向知识库外的软链接。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

from . import chunker, parsers, pipeline

META_DIR = ".stratum"
MAX_DEPTH = 8
MAX_NAME_BYTES = 240
_SKIP_DIRS = {"node_modules", "__pycache__", "lost+found"}
_KB_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,64}$")
_BAD_SEGMENT_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")
_VERSIONED = re.compile(r"^(.*)_v([1-9]\d*)$", re.I)


class StorageError(ValueError):
    """可直接展示给用户的文件库错误；status 对应 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def data_dir() -> Path:
    default = Path(__file__).resolve().parent.parent / "data" / "library"
    return Path(os.environ.get("STRATUM_DATA_DIR") or default)


def ensure_root() -> Path:
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def min_free_bytes() -> int:
    return int(os.environ.get("STRATUM_MIN_FREE_MB") or 2048) << 20


def free_bytes(path: Path | None = None) -> int | None:
    try:
        return shutil.disk_usage(path or data_dir()).free
    except OSError:
        return None


def ensure_free_space(path: Path | None = None) -> None:
    """写入前检查剩余空间；低于 STRATUM_MIN_FREE_MB 时拒绝写入，避免磁盘写满后文件库和 FastGPT 一起出问题。"""
    free = free_bytes(path)
    if free is not None and free < min_free_bytes():
        raise StorageError(f"服务器磁盘剩余空间不足（{free >> 20} MB），暂停写入，请联系管理员清理", 507)


def root_status() -> dict:
    root = data_dir()
    ok = root.is_dir() and os.access(root, os.R_OK | os.W_OK | os.X_OK)
    free = free_bytes(root) if ok else None
    return {"path": str(root), "writable": ok, "free_mb": free >> 20 if free is not None else None,
            "min_free_mb": min_free_bytes() >> 20, "low_space": free is not None and free < min_free_bytes()}


# ---------------------------------------------------------------- 路径

def natural_key(path: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path)]


def validate_kb_name(name: str) -> str:
    name = (name or "").strip()
    if not _KB_NAME.match(name) or name.startswith(".") or name in (".", ".."):
        raise StorageError("知识库名称不能为空、不能以 . 开头，不能包含 / \\ 等特殊字符，最多 64 个字符")
    return name


def kb_dir(name: str, must_exist: bool = True) -> Path:
    name = validate_kb_name(name)
    path = data_dir() / name
    if must_exist and not path.is_dir():
        raise StorageError(f"知识库「{name}」不存在", 404)
    return path


def clean_rel(rel: str) -> str:
    """规范化相对路径；任何可疑片段都直接拒绝，而不是悄悄修正。"""
    if not isinstance(rel, str) or not rel.strip():
        raise StorageError("路径不能为空")
    if rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel) or "\\" in rel:
        raise StorageError(f"非法路径：{rel}")
    parts = rel.split("/")
    for part in parts:
        if not part or part in (".", "..") or part.startswith(".") or _BAD_SEGMENT_CHARS.search(part):
            raise StorageError(f"非法路径：{rel}")
        if len(part.encode()) > MAX_NAME_BYTES:
            raise StorageError(f"文件名过长：{part}")
    if len(parts) > MAX_DEPTH + 1:
        raise StorageError(f"目录层级过深：{rel}")
    return "/".join(parts)


def resolve(kb: Path, rel: str) -> Path:
    rel = clean_rel(rel)
    target = (kb / rel).resolve()
    base = kb.resolve()
    if target != base and base not in target.parents:
        raise StorageError(f"非法路径：{rel}")
    return target


def rel_of(kb: Path, path: Path) -> str:
    return path.relative_to(kb).as_posix()


def safe_segment(name: str) -> str:
    """把用户上传时带来的文件名/目录名转换成可用的单个路径段。"""
    name = _BAD_SEGMENT_CHARS.sub("_", name or "").strip().lstrip(".").strip()
    if not name:
        raise StorageError("文件名无效")
    while len(name.encode()) > MAX_NAME_BYTES:
        stem, ext = os.path.splitext(name)
        name = stem[:-1] + ext
    return name


# ---------------------------------------------------------------- 原子写入

def _tmp_dir(kb: Path) -> Path:
    path = kb / META_DIR / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_tmp(kb: Path, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(dir=_tmp_dir(kb), suffix=".part")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return Path(name)


def publish_new(tmp: Path, target: Path) -> None:
    """把写完的临时文件发布为 target；target 已存在时抛出 FileExistsError，绝不覆盖。

    成功后删除 tmp；失败时保留 tmp，由调用方决定换名重试或清理。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(tmp, target)  # 原子且不覆盖
    except FileExistsError:
        raise
    except OSError:
        # 文件系统不支持硬链接时退回独占创建 + 复制
        with open(target, "xb") as out, open(tmp, "rb") as src:
            while chunk := src.read(1 << 20):
                out.write(chunk)
    tmp.unlink(missing_ok=True)


def replace_file(kb: Path, target: Path, data: bytes) -> None:
    tmp = _write_tmp(kb, data)
    os.replace(tmp, target)


def etag(path: Path) -> str:
    st = path.stat()
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


# ---------------------------------------------------------------- 扫描与版本识别

def _walk(kb: Path, sub: Path | None = None, recursive: bool = True) -> list[dict]:
    entries = []
    start = sub or kb
    base_depth = len(start.relative_to(kb).parts) if sub else 0
    for dirpath, dirnames, filenames in os.walk(start):
        here = Path(dirpath)
        depth = len(here.relative_to(kb).parts)
        dirnames[:] = sorted(d for d in dirnames
                             if recursive and not d.startswith(".") and d not in _SKIP_DIRS
                             and depth - base_depth < MAX_DEPTH and not (here / d).is_symlink())
        for name in filenames:
            if name.startswith(".") or not re.search(r"\.(pdf|md)$", name, re.I):
                continue
            path = here / name
            if path.is_symlink():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append({"path": rel_of(kb, path), "size": st.st_size, "mtime": st.st_mtime})
    return entries


def index_files(entries: list[dict]) -> dict:
    """识别 PDF 与其 Markdown 产物：`a_v2.md` 属于 `a.pdf`；与 PDF 同名的 `a.md` 视为第 1 版。"""
    pdfs = [e for e in entries if e["path"].lower().endswith(".pdf")]
    pdf_paths = {e["path"].lower(): e["path"] for e in pdfs}
    artifacts = []
    for entry in entries:
        path = entry["path"]
        if not path.lower().endswith(".md"):
            continue
        stem = path[:-3]
        legacy = pdf_paths.get(f"{stem}.pdf".lower())
        match = None if legacy else _VERSIONED.match(stem)
        if not legacy and not match:
            continue  # README.md 之类的普通文档不是解析产物
        source = f"{match.group(1)}.pdf" if match else stem + ".pdf"
        artifacts.append({
            "path": path,
            "pdf_path": legacy or pdf_paths.get(source.lower(), source),
            "version": int(match.group(2)) if match else 1,
            "size": entry["size"],
            "mtime": entry["mtime"],
        })
    artifacts.sort(key=lambda a: natural_key(a["path"]))
    converted = {a["pdf_path"] for a in artifacts}
    files = sorted(({**e, "converted": e["path"] in converted} for e in pdfs), key=lambda f: natural_key(f["path"]))
    return {"files": files, "artifacts": artifacts}


def scan(kb: Path) -> dict:
    return index_files(_walk(kb))


# ---------------------------------------------------------------- 知识库

def default_settings() -> dict:
    return {"convert": asdict(pipeline.ConvertOptions()), "chunk": asdict(chunker.ChunkOptions())}


def _settings_path(kb: Path) -> Path:
    return kb / META_DIR / "settings.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(kb: Path, path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_tmp(kb, json.dumps(data, ensure_ascii=False, indent=2).encode())
    os.replace(tmp, path)


# settings.json 由多处读改写（默认参数、FastGPT 绑定），Markdown 修改需要「比对 etag + 写入」不被打断；
# 同一进程内用这把锁串行化（服务必须以单进程运行，见部署文档）
_json_lock = threading.RLock()


def update_json(kb: Path, path: Path, fn) -> dict:
    """在锁内读取 → fn(当前内容) → 写回，返回写入的内容。"""
    with _json_lock:
        current = _read_json(path, {})
        updated = fn(current if isinstance(current, dict) else {})
        _write_json(kb, path, updated)
        return updated


def normalize_settings(data: dict) -> dict:
    if not isinstance(data, dict):
        raise StorageError("设置必须是 JSON 对象")
    try:
        convert = asdict(pipeline.ConvertOptions.from_dict(data.get("convert") or {}))
        chunk = asdict(chunker.ChunkOptions.from_dict(data.get("chunk") or {}))
    except (ValueError, TypeError) as exc:
        raise StorageError(str(exc)) from exc
    return {"convert": convert, "chunk": chunk}


def read_settings(kb: Path) -> dict:
    stored = _read_json(_settings_path(kb), {})
    merged = default_settings()
    for key in ("convert", "chunk"):
        if isinstance(stored.get(key), dict):
            merged[key].update(stored[key])
    # 已下线的解析方案（如旧版的 OXR）回退为默认方案，避免批量解析直接失败
    try:
        parsers.get_parser(str(merged["convert"].get("parser")))
    except ValueError:
        merged["convert"]["parser"] = default_settings()["convert"]["parser"]
    # 其他扩展字段（如 FastGPT 绑定）原样保留
    return {**{k: v for k, v in stored.items() if k not in merged}, **merged}


def write_settings(kb: Path, data: dict) -> dict:
    normalized = normalize_settings(data)
    update_json(kb, _settings_path(kb), lambda current: {**current, **normalized})
    return read_settings(kb)


def list_kbs() -> list[dict]:
    root = data_dir()
    if not root.is_dir():
        return []
    result = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith(".") or child.is_symlink():
            continue
        try:
            validate_kb_name(child.name)
        except StorageError:
            continue
        inventory = scan(child)
        times = [e["mtime"] for e in inventory["files"] + inventory["artifacts"]]
        result.append({
            "name": child.name,
            "pdfs": len(inventory["files"]),
            "converted": sum(f["converted"] for f in inventory["files"]),
            "updated": max(times, default=child.stat().st_mtime),
        })
    return sorted(result, key=lambda kb: natural_key(kb["name"]))


def create_kb(name: str) -> Path:
    name = validate_kb_name(name)
    root = ensure_root()
    for child in root.iterdir():
        if child.name.lower() == name.lower():
            raise StorageError(f"知识库「{child.name}」已存在", 409)
    path = root / name
    path.mkdir()
    _write_json(path, _settings_path(path), {**default_settings(), "created": time.time()})
    return path


# ---------------------------------------------------------------- 上传

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _sibling_ci(directory: Path, name: str) -> Path | None:
    if not directory.is_dir():
        return None
    lower = name.lower()
    for child in directory.iterdir():
        if child.name.lower() == lower:
            return child
    return None


def upload_target(kb: Path, rel: str) -> str:
    """把浏览器给出的相对路径（文件名或 webkitRelativePath）转换成知识库内的安全路径。"""
    parts = [p for p in re.split(r"[/\\]+", rel or "") if p and p not in (".", "..")]
    if not parts:
        raise StorageError("文件名无效")
    parts = [safe_segment(p) for p in parts]
    if not parts[-1].lower().endswith(".pdf"):
        raise StorageError(f"{parts[-1]} 不是 PDF 文件")
    return clean_rel("/".join(parts))


def store_upload(kb: Path, rel: str, stream: BinaryIO, max_bytes: int, check_pdf=None) -> dict:
    """保存一个上传的 PDF。同名同内容跳过；同名不同内容自动改名为 `名称(2).pdf`，不覆盖已有文件。

    `check_pdf(path)` 可选，在发布前校验文件（例如能否打开、页数），失败时抛出 StorageError。
    """
    ensure_free_space(kb)
    rel = upload_target(kb, rel)
    fd, tmp_name = tempfile.mkstemp(dir=_tmp_dir(kb), suffix=".part")
    tmp = Path(tmp_name)
    h = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            head = b""
            while chunk := stream.read(1 << 20):
                size += len(chunk)
                if size > max_bytes:
                    raise StorageError(f"文件超过 {max_bytes >> 20} MB 上限", 413)
                if len(head) < 1024:
                    head += chunk[:1024]
                h.update(chunk)
                f.write(chunk)
        if not head.lstrip().startswith(b"%PDF"):
            raise StorageError("文件不是 PDF")
        if check_pdf:
            check_pdf(tmp)
        digest = h.hexdigest()
        target = resolve(kb, rel)
        stem, ext = os.path.splitext(target.name)
        for n in range(1, 1000):
            name = target.name if n == 1 else f"{stem}({n}){ext}"
            existing = _sibling_ci(target.parent, name)
            if existing is not None:
                if existing.is_file() and existing.suffix.lower() == ".pdf" and _sha256(existing) == digest:
                    return {"path": rel_of(kb, existing), "status": "skipped", "message": "已存在相同内容的文件"}
                continue
            candidate = target.with_name(name)
            try:
                publish_new(tmp, candidate)
            except FileExistsError:
                continue
            path = rel_of(kb, candidate)
            if n == 1:
                return {"path": path, "status": "added", "size": size}
            return {"path": path, "status": "renamed", "size": size, "message": f"同名文件已存在，保存为 {candidate.name}"}
        raise StorageError("同名文件过多，请先整理目录")
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- Markdown 版本

def _meta_path(kb: Path, md_rel: str) -> Path:
    return kb / META_DIR / "meta" / (md_rel + ".json")


def read_meta(kb: Path, md_rel: str) -> dict:
    return _read_json(_meta_path(kb, md_rel), {})


def write_meta(kb: Path, md_rel: str, meta: dict) -> None:
    _write_json(kb, _meta_path(kb, md_rel), meta)


def versions_of(kb: Path, pdf_rel: str) -> list[dict]:
    """同一目录内属于该 PDF 的全部 Markdown 版本（按版本号升序）。"""
    pdf = resolve(kb, pdf_rel)
    folder = pdf.parent
    entries = []
    if folder.is_dir():
        for child in folder.iterdir():
            if child.is_file() and not child.name.startswith(".") and re.search(r"\.(pdf|md)$", child.name, re.I):
                st = child.stat()
                entries.append({"path": rel_of(kb, child), "size": st.st_size, "mtime": st.st_mtime})
    wanted = clean_rel(pdf_rel).lower()
    return sorted((a for a in index_files(entries)["artifacts"] if a["pdf_path"].lower() == wanted),
                  key=lambda a: a["version"])


def md_name(pdf_rel: str, version: int) -> str:
    return re.sub(r"\.pdf$", "", pdf_rel, flags=re.I) + f"_v{version}.md"


def save_new_version(kb: Path, pdf_rel: str, markdown: str, meta: dict | None = None,
                     min_version: int | None = None) -> dict:
    """为 PDF 生成下一个版本号并写入；并发保存时各自拿到不同的版本号，不会互相覆盖。

    min_version：前端已向用户展示的预定版本号。未被占用时优先使用，否则取更大的空闲版本号。
    """
    pdf_rel = clean_rel(pdf_rel)
    if not pdf_rel.lower().endswith(".pdf"):
        raise StorageError("pdf_path 必须指向 PDF 文件")
    if not resolve(kb, pdf_rel).is_file():
        raise StorageError(f"找不到 {pdf_rel}", 404)
    ensure_free_space(kb)
    version = max((a["version"] for a in versions_of(kb, pdf_rel)), default=0) + 1
    if isinstance(min_version, int) and 0 < min_version < 100000:
        version = max(version, min_version)
    tmp = _write_tmp(kb, markdown.encode("utf-8"))
    try:
        for _ in range(100):
            rel = md_name(pdf_rel, version)
            target = resolve(kb, rel)
            try:
                publish_new(tmp, target)
            except FileExistsError:
                version += 1
                continue
            write_meta(kb, rel, {**(meta or {}), "pdf_path": pdf_rel, "version": version, "created": time.time()})
            return {"path": rel, "pdf_path": pdf_rel, "version": version, "etag": etag(target)}
    finally:
        tmp.unlink(missing_ok=True)
    raise StorageError("无法分配新的版本号，请稍后重试", 409)


def read_markdown(kb: Path, rel: str) -> dict:
    rel = clean_rel(rel)
    if not rel.lower().endswith(".md"):
        raise StorageError("只能读取 Markdown 文件")
    path = resolve(kb, rel)
    if not path.is_file():
        raise StorageError(f"找不到 {rel}", 404)
    return {"path": rel, "markdown": path.read_text("utf-8", errors="replace"), "etag": etag(path),
            "meta": read_meta(kb, rel)}


def overwrite_markdown(kb: Path, rel: str, markdown: str, expected_etag: str | None) -> dict:
    """保存对已有版本的修改。expected_etag 与当前文件不一致时说明别人已改过，返回冲突。"""
    rel = clean_rel(rel)
    if not rel.lower().endswith(".md"):
        raise StorageError("只能修改 Markdown 文件")
    path = resolve(kb, rel)
    if not path.is_file():
        raise StorageError(f"找不到 {rel}", 404)
    ensure_free_space(kb)
    # 比对 etag 与写入放在同一把锁内：两人同时保存同一版本时，只有先到的一方成功
    with _json_lock:
        if expected_etag and etag(path) != expected_etag:
            raise StorageError(f"{rel} 已被其他人修改，请重新打开后再编辑", 409)
        replace_file(kb, path, markdown.encode("utf-8"))
    meta = read_meta(kb, rel)
    write_meta(kb, rel, {**meta, "edited": time.time()})
    return {"path": rel, "etag": etag(path)}


def pdf_path(kb: Path, rel: str) -> Path:
    rel = clean_rel(rel)
    if not rel.lower().endswith(".pdf"):
        raise StorageError("只能读取 PDF 文件")
    path = resolve(kb, rel)
    if not path.is_file():
        raise StorageError(f"找不到 {rel}", 404)
    return path


def cleanup_tmp(kb: Path, older_than: float = 3600) -> None:
    folder = kb / META_DIR / "tmp"
    if not folder.is_dir():
        return
    now = time.time()
    for child in folder.iterdir():
        try:
            if now - child.stat().st_mtime > older_than:
                child.unlink()
        except OSError:
            pass


def new_id() -> str:
    return uuid.uuid4().hex[:12]

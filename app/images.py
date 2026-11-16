"""PDF 中的图片：截取版面分析识别出的图片区域，按内容哈希保存在文件库的 .images 目录，
Markdown 中以绝对地址引用，FastGPT 展示引用内容时由浏览器直接从 Stratum 加载。

    <STRATUM_DATA_DIR>/.images/ab/ab12…ef.png   →   <公开地址>/api/images/ab12…ef.png

公开地址取 STRATUM_PUBLIC_URL；未配置时使用用户访问 Stratum 时的地址。
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

NAME_RE = re.compile(r"^[0-9a-f]{40}\.(png|jpg)$")
MIN_SIDE = 36  # pt，过小的区域多为图标、装饰线
MIN_AREA = 3600  # pt²
MAX_PIXELS = 1600  # 长边像素上限
PNG_LIMIT = 400 << 10  # PNG 超过该大小（多为照片）改存 JPEG

_lock = threading.Lock()
_seen_base = ""


def images_dir() -> Path:
    from .storage import data_dir

    return data_dir() / ".images"


def path_for(name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    return images_dir() / name[:2] / name


def public_base() -> str:
    env = (os.environ.get("STRATUM_PUBLIC_URL") or "").strip().rstrip("/")
    return env or _seen_base


def remember_base(url: str) -> None:
    """记住用户访问 Stratum 的地址（未配置 STRATUM_PUBLIC_URL 时用来生成图片链接）。"""
    global _seen_base
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or host in ("localhost", "testserver", "::1") or host.startswith("127."):
        return
    base = f"{parts.scheme}://{parts.netloc}"
    if base != _seen_base:
        with _lock:
            _seen_base = base


def url_for(name: str) -> str:
    return f"{public_base()}/api/images/{name}"


# ---------------------------------------------------------------- 工作进程中调用


def _store(data: bytes, ext: str, root: Path) -> str:
    name = f"{hashlib.sha1(data).hexdigest()}.{ext}"
    target = root / name[:2] / name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return name


def save_region(page, bbox, root: str) -> dict | None:
    """把页面上的图片区域渲染成图片并保存，返回 {"name", "width", "height"}；区域过小时返回 None。"""
    import pymupdf

    rect = pymupdf.Rect(bbox) & page.rect
    if rect.is_empty or min(rect.width, rect.height) < MIN_SIDE or rect.get_area() < MIN_AREA:
        return None
    dpi = max(72, min(200, int(MAX_PIXELS * 72 / max(rect.width, rect.height))))
    pix = page.get_pixmap(clip=rect, dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    if pix.width < 20 or pix.height < 20 or _skip(pix):
        return None
    data, ext = pix.tobytes("png"), "png"
    if len(data) > PNG_LIMIT:
        data, ext = pix.tobytes("jpeg", jpg_quality=85), "jpg"
    return {"name": _store(data, ext, Path(root)), "width": pix.width, "height": pix.height}


def _skip(pix) -> bool:
    """不值得保存的区域：
    - 几乎全白（空白占位框）；
    - 红色印章：有颜色的像素大多是红色（盖在落款文字上时也成立）；
    - 底色、渐变背景：几乎没有轮廓，且大部分不是白色（白底上的细线图不受影响）。"""
    import numpy as np

    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    step = max(1, int((pix.width * pix.height / 250_000) ** 0.5))
    img = img[::step, ::step].astype(np.int16)
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    ink = img.min(axis=2) < 200
    n_ink = int(ink.sum())
    if (img.min(axis=2) < 235).mean() < 0.005:
        return True
    red = ink & (r > 150) & (g < 140) & (b < 140) & (r - np.maximum(g, b) > 60)
    if n_ink > 50 and red.sum() >= 0.5 * n_ink:
        return True
    gray = (r * 3 + g * 6 + b) // 10
    edges = (np.abs(np.diff(gray, axis=1))[:-1, :] > 24) | (np.abs(np.diff(gray, axis=0))[:, :-1] > 24)
    white = (img.min(axis=2) >= 235).mean()
    return edges.mean() < 0.015 and white < 0.5

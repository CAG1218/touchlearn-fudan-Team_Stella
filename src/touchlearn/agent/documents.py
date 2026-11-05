"""Read text-bearing and scanned uploads so the agent can use their content.

Images are handled by :mod:`touchlearn.agent.vision`, meshes by the volumetric
module.  This module covers PDF/TXT/JSON: it extracts plain text, keeps the
result bounded, and -- when a PDF has no text layer -- renders the first pages
to PNG so the optional vision model can read a scanned worksheet, a photographed
textbook page, or a scanned drawing.

Rasterisation tries, in order: PyMuPDF, the poppler ``pdftoppm`` binary, and
images embedded in the PDF through pypdf.  Every backend is optional; when none
is available the draft says so instead of pretending the scan was read.  A
blank page is not turned into a fake "scan": it is reported as blank.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_TEXT_CHARS = 60_000
MAX_PDF_PAGES = 40
MAX_RASTER_PAGES = 6
RASTER_DPI = 150
MAX_RASTER_EDGE = 2200


@dataclass
class DocumentText:
    ok: bool
    text: str = ""
    pages: int = 0
    message: str = ""
    truncated: bool = False
    page_images: list[str] = field(default_factory=list)
    rasterized: bool = False
    rasterizer: str = ""

    def metadata(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "chars": len(self.text),
            "pages": self.pages,
            "truncated": self.truncated,
            "message": self.message,
            "rasterized": self.rasterized,
            "rasterizer": self.rasterizer,
            "page_images": len(self.page_images),
        }


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _bounded(text: str) -> tuple[str, bool]:
    cleaned = text.replace("\x00", "").strip()
    if len(cleaned) <= MAX_TEXT_CHARS:
        return cleaned, False
    return cleaned[:MAX_TEXT_CHARS], True


def _has_ink(path: Path) -> bool:
    """Return False for an all-white page, True when it carries marks.

    A blank page usually means the PDF is a form or an empty export, not a
    scan.  Treating it as an image would hand a white rectangle to the vision
    model and produce an invented diagram.  If Pillow is unavailable we keep the
    page: the caller still sees an honest "no text layer" note.
    """
    try:
        from PIL import Image
    except ImportError:
        return True
    try:
        with Image.open(path) as image:
            grayscale = image.convert("L")
            histogram = grayscale.histogram()
            total = sum(histogram)
            if total == 0:
                return False
            dark = sum(histogram[:246])
            return dark / total > 0.0015
    except Exception:
        return True


def _unique_prefix(path: Path, directory: Path) -> str:
    prefix = f"{path.stem}-{uuid.uuid4().hex[:6]}"
    return str(directory / prefix)


def _rasterize_with_pymupdf(path: Path, directory: Path) -> tuple[list[str], str]:
    import pymupdf as fitz  # type: ignore

    document = fitz.open(str(path))
    try:
        count = min(document.page_count, MAX_RASTER_PAGES)
        images: list[str] = []
        for index in range(count):
            page = document.load_page(index)
            rect = page.rect
            scale = RASTER_DPI / 72.0
            longest = max(rect.width, rect.height) * scale
            if longest > MAX_RASTER_EDGE:
                scale *= MAX_RASTER_EDGE / longest
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
            target = Path(f"{_unique_prefix(path, directory)}-p{index + 1}.png")
            pixmap.save(str(target))
            if _has_ink(target):
                images.append(str(target))
            else:
                target.unlink(missing_ok=True)
        return images, "pymupdf"
    finally:
        document.close()


def _rasterize_with_pdftoppm(path: Path, directory: Path) -> tuple[list[str], str]:
    binary = shutil.which("pdftoppm")
    if not binary:
        raise FileNotFoundError("pdftoppm not found")
    with tempfile.TemporaryDirectory(dir=str(directory)) as temp_dir:
        prefix = Path(temp_dir) / "page"
        result = subprocess.run(
            [
                binary,
                "-png",
                "-r",
                str(RASTER_DPI),
                "-f",
                "1",
                "-l",
                str(MAX_RASTER_PAGES),
                str(path),
                str(prefix),
            ],
            capture_output=True,
            timeout=90,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace")[:200]
            raise RuntimeError(message or "pdftoppm failed")
        produced = sorted(Path(temp_dir).glob("page-*.png"))
        images: list[str] = []
        for index, source in enumerate(produced):
            if not _has_ink(source):
                continue
            target = Path(f"{_unique_prefix(path, directory)}-p{index + 1}.png")
            source.replace(target)
            images.append(str(target))
        return images, "pdftoppm"


def _rasterize_with_pypdf(path: Path, directory: Path) -> tuple[list[str], str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    images: list[str] = []
    for index, page in enumerate(reader.pages[:MAX_RASTER_PAGES]):
        for image_index, image in enumerate(page.images):
            try:
                data = image.data
            except Exception:
                continue
            suffix = Path(str(image.name or "")).suffix.lower() or ".png"
            if suffix not in {".png", ".jpg", ".jpeg"}:
                suffix = ".png"
            target = Path(f"{_unique_prefix(path, directory)}-p{index + 1}-{image_index + 1}{suffix}")
            target.write_bytes(data)
            if _has_ink(target):
                images.append(str(target))
            else:
                target.unlink(missing_ok=True)
    return images, "pypdf"


def _rasterize_pdf(path: Path, directory: Path | None = None) -> tuple[list[str], str, str]:
    """Render a scanned PDF to PNG pages.

    Returns ``(paths, backend, error)``; ``backend`` is empty when every
    renderer failed.  The caller decides whether a blank page or a missing
    renderer is fatal for its task.
    """
    target_dir = directory or (path.parent / "pages")
    target_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for backend in (_rasterize_with_pymupdf, _rasterize_with_pdftoppm, _rasterize_with_pypdf):
        try:
            images, name = backend(path, target_dir)
            return images, name, ""
        except Exception as error:  # try the next optional backend
            errors.append(f"{backend.__name__.replace('_rasterize_with_', '')}: {error}")
    return [], "", "; ".join(errors)


def _extract_pdf(path: Path, raster_dir: Path | None = None) -> DocumentText:
    try:
        from pypdf import PdfReader
    except ImportError:
        return DocumentText(
            False,
            message="未安装 pypdf，无法读取 PDF 文字层；可安装 `pip install pypdf` 后重试，或把关键页导出为 PNG。",
        )
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return DocumentText(False, message="PDF 已加密，无法读取文字层。")
        pages = len(reader.pages)
        chunks: list[str] = []
        consumed = 0
        text_pages = 0
        for index, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
            try:
                page_text = page.extract_text() or ""
            except Exception as error:  # one broken page must not kill the upload
                page_text = ""
                if index == 0:
                    return DocumentText(False, pages=pages, message=f"PDF 首页文字层解析失败：{error}")
            if page_text.strip():
                chunks.append(page_text.strip())
                consumed += len(page_text)
                text_pages += 1
            if consumed >= MAX_TEXT_CHARS:
                break
        text, truncated = _bounded("\n\n".join(chunks))
        if text:
            return DocumentText(
                True,
                text=text,
                pages=pages,
                message=f"已读取 PDF 文字层（{text_pages}/{pages} 页有文字）。",
                truncated=truncated,
            )

        # No text layer: this is the scanned-document path.  Render pages and
        # hand them to the vision model; never infer the content from the file
        # name or a blank page.
        images, backend, error = _rasterize_pdf(path, raster_dir)
        if images:
            note = f"PDF 没有可提取的文字层（扫描件），已把 {len(images)} 页渲染为图片交给视觉模型理解。"
            if len(images) < min(pages, MAX_RASTER_PAGES):
                note += f" 只处理前 {MAX_RASTER_PAGES} 页，空白页已跳过。"
            return DocumentText(
                True,
                pages=pages,
                message=note,
                page_images=images,
                rasterized=True,
                rasterizer=backend,
            )
        if error:
            note = "PDF 没有可提取的文字层（可能是扫描件），但本机没有可用的 PDF 渲染器。请安装 PyMuPDF（`pip install pymupdf`）或 poppler，或把关键页导出为 PNG。"
        else:
            note = "PDF 没有可提取的文字层（可能是扫描件或空白页），渲染后也没有发现可读内容。请检查文件，或把关键页导出为 PNG。"
        return DocumentText(False, pages=pages, message=note, rasterizer=backend)
    except Exception as error:
        return DocumentText(False, message=f"PDF 读取失败：{error}")


def extract_document(path: Path, *, raster_dir: Path | None = None) -> DocumentText:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path, raster_dir=raster_dir)
    if suffix not in {".txt", ".json"}:
        return DocumentText(False, message="该文件类型不提供文字内容。")
    try:
        if path.stat().st_size > 30 * 1024 * 1024:
            return DocumentText(False, message="文本文件超过 30 MB 限制。")
        text, truncated = _bounded(_decode(path.read_bytes()))
    except OSError as error:
        return DocumentText(False, message=f"读取失败：{error}")
    if not text:
        return DocumentText(False, message="文件内容为空。")
    return DocumentText(True, text=text, message="已读取文本内容。", truncated=truncated)


def scene_from_text(text: str) -> dict[str, Any] | None:
    """Return a scene object when the text is a scene JSON document.

    Accepts either a bare scene (``{"title": ..., "blocks": [...]}``) or a
    wrapper with a ``scene`` key.  Anything else is ignored; this function
    never guesses a structure from prose.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("scene") if isinstance(payload.get("scene"), dict) else payload
    if not isinstance(candidate, dict):
        return None
    blocks = candidate.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None
    return candidate

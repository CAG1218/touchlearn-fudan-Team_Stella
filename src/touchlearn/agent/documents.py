"""Read text-bearing uploads so the agent can use their content.

Images are handled by :mod:`touchlearn.agent.vision`, meshes by the volumetric
module.  This module covers PDF/TXT/JSON: it extracts plain text, keeps the
result bounded, and reports clearly when a PDF is a scan with no text layer.
Extraction is best-effort and never fabricated: a PDF without a text layer is
reported as such instead of being guessed from the file name.

``pypdf`` is optional.  Without it the rest of the agent keeps working and the
draft tells the user that PDF text extraction is unavailable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_TEXT_CHARS = 60_000
MAX_PDF_PAGES = 40


@dataclass
class DocumentText:
    ok: bool
    text: str = ""
    pages: int = 0
    message: str = ""
    truncated: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "chars": len(self.text),
            "pages": self.pages,
            "truncated": self.truncated,
            "message": self.message,
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


def _extract_pdf(path: Path) -> DocumentText:
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
            if consumed >= MAX_TEXT_CHARS:
                break
        text, truncated = _bounded("\n\n".join(chunks))
        if not text:
            return DocumentText(
                False,
                pages=pages,
                message="PDF 没有可提取的文字层（可能是扫描件）。请导出关键页为 PNG，或配置视觉模型后按图片理解。",
            )
        return DocumentText(True, text=text, pages=pages, message=f"已读取 PDF 文字层（{pages} 页）。", truncated=truncated)
    except Exception as error:
        return DocumentText(False, message=f"PDF 读取失败：{error}")


def extract_document(path: Path) -> DocumentText:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
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

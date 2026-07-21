"""Safe local PDF URLs for Gradio citations (#page=N)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

from rag_engine.config import library_root

# Stored Chroma page values from PyPDFLoader are 0-based.
STORED_PAGE_BASE = 0


def viewer_page(stored_page: int | str | None) -> int | None:
    """Convert stored page to 1-based PDF viewer fragment page."""
    if stored_page is None or stored_page == "?" or stored_page == "":
        return None
    try:
        n = int(stored_page)
    except (TypeError, ValueError):
        return None
    if STORED_PAGE_BASE == 0:
        return max(1, n + 1)
    return max(1, n)


def _casefold_path(p: Path) -> str:
    # APFS is case-insensitive by default; compare folded forms.
    return os.path.normcase(str(p))


def resolve_library_file(rel_or_abs: str, *, root: Path | None = None) -> Path:
    """Resolve a source path to a real file under library root.

    Raises ValueError on traversal / escape / missing file.
    Symlinks are resolved with Path.resolve() before the prefix check.
    """
    root = (root or library_root()).resolve()
    raw = (rel_or_abs or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("empty path")

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        real = candidate.resolve(strict=True)
    except FileNotFoundError as e:
        raise ValueError(f"source file not found: {raw}") from e

    root_cf = _casefold_path(root)
    real_cf = _casefold_path(real)
    # Ensure real is under root (prefix + separator, or equal)
    if real_cf != root_cf and not real_cf.startswith(root_cf + os.sep):
        raise ValueError("path escapes CE_LIBRARY_ROOT")
    if not real.is_file():
        raise ValueError(f"not a file: {raw}")
    return real


def safe_pdf_file_url(
    rel_or_abs: str,
    stored_page: int | str | None = None,
    *,
    root: Path | None = None,
    route_prefix: str = "/file=",
) -> str:
    """Build a Gradio-safe local file URL with optional #page=N (1-based).

    ``route_prefix`` defaults to Gradio's ``/file=`` absolute-path form.
    Path is URL-encoded; only files under the library root are allowed.
    """
    real = resolve_library_file(rel_or_abs, root=root)
    # Gradio absolute file route; encode path safely
    url = f"/file={quote(str(real), safe='/')}"
    page = viewer_page(stored_page)
    if page is not None and real.suffix.lower() == ".pdf":
        url = f"{url}#page={page}"
    return url


def source_open_markdown(
    path: str,
    page: int | str | None,
    *,
    root: Path | None = None,
) -> str:
    """Markdown link for Gradio, or a useful missing-file message."""
    try:
        url = safe_pdf_file_url(path, page, root=root)
        label = Path(path.replace("\\", "/")).name
        vp = viewer_page(page)
        page_note = f" (p.{vp})" if vp is not None else ""
        return f"[{label}{page_note}]({url})"
    except ValueError as e:
        return f"_Unavailable: {path} — {e}_"


def is_macos() -> bool:
    return sys.platform == "darwin"

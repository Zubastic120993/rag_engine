"""Page citation contract + safe local PDF URLs for Gradio (#page=N).

Page contract (ORCH_102B)
-------------------------
* ``page_index`` — internal storage / Chroma / PyPDFLoader index; **0-based**
  integer for PDFs.
* ``page`` — human-facing citation / PDF viewer fragment; **1-based** integer.
  For PDFs: ``page = page_index + 1``.

Non-PDF sources (e.g. CE Wiki markdown) store a 1-based sentinel at ingest
(``page = 1``). For those, ``page`` equals the stored value (no +1).

Conversion ownership: this module is the single authority. Human-facing
emitters (ask sources JSON, CLI, Hermes passthrough of ask JSON, Gradio,
benchmark scoring) must use these helpers — never ad-hoc ``+1``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from rag_engine.config import library_root

# Stored Chroma page values from PyPDFLoader are 0-based for PDFs.
STORED_PAGE_BASE = 0


def is_pdf_source(source: str | None) -> bool:
    """True when the source path looks like a PDF (case-insensitive)."""
    if not source:
        return False
    return str(source).replace("\\", "/").lower().endswith(".pdf")


def parse_page_index(stored_page: Any) -> int | None:
    """Parse an internal/stored page value to a non-negative int, else None.

    Rejects None, empty, ``?``, non-integers, and negative values.
    """
    if stored_page is None or stored_page == "?" or stored_page == "":
        return None
    try:
        n = int(stored_page)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return n


def viewer_page(
    stored_page: Any,
    *,
    source: str | None = None,
) -> int | None:
    """Convert stored page to 1-based human / PDF-viewer page.

    * PDF (default when ``source`` is omitted or ends with ``.pdf``):
      ``page_index + 1`` when ``page_index >= 0``.
    * Non-PDF with ``source`` provided: stored value is already human-facing;
      return it unchanged when ``>= 1``.
    * Invalid / negative / non-integer → ``None``.
    """
    idx = parse_page_index(stored_page)
    if idx is None:
        return None
    if source is not None and not is_pdf_source(source):
        return idx if idx >= 1 else None
    # PDF (and unspecified source — Gradio URL path is PDF-only anyway)
    if STORED_PAGE_BASE == 0:
        return idx + 1
    return idx


def citation_page_fields(
    stored_page: Any,
    *,
    source: str | None = None,
) -> dict[str, int]:
    """Build public citation fields from a stored page value.

    Returns a dict that may contain:
    * ``page`` — human-facing 1-based page
    * ``page_index`` — internal 0-based index (PDFs) or stored sentinel (non-PDF)

    Empty dict when the stored value cannot be parsed safely.
    """
    idx = parse_page_index(stored_page)
    if idx is None:
        return {}
    human = viewer_page(idx, source=source)
    if human is None:
        return {}
    return {"page": human, "page_index": idx}


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

    ``stored_page`` must be the **0-based page_index** (or legacy stored value).
    ``route_prefix`` defaults to Gradio's ``/file=`` absolute-path form.
    Path is URL-encoded; only files under the library root are allowed.
    """
    real = resolve_library_file(rel_or_abs, root=root)
    # Gradio absolute file route; encode path safely
    url = f"/file={quote(str(real), safe='/')}"
    page = viewer_page(stored_page, source=str(real))
    if page is not None and real.suffix.lower() == ".pdf":
        url = f"{url}#page={page}"
    return url


def source_open_markdown(
    path: str,
    page: int | str | None,
    *,
    root: Path | None = None,
) -> str:
    """Markdown link for Gradio, or a useful missing-file message.

    ``page`` is the **stored / page_index** value (0-based for PDFs).
    """
    try:
        url = safe_pdf_file_url(path, page, root=root)
        label = Path(path.replace("\\", "/")).name
        vp = viewer_page(page, source=path)
        page_note = f" (p.{vp})" if vp is not None else ""
        return f"[{label}{page_note}]({url})"
    except ValueError as e:
        return f"_Unavailable: {path} — {e}_"


def is_macos() -> bool:
    return sys.platform == "darwin"

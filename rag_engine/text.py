"""Text normalization shared by ingest and query."""

from __future__ import annotations

import unicodedata
from pathlib import Path

PDF_MAGIC = b"%PDF"


def is_valid_pdf(path: Path) -> bool:
    """True if the file begins with the %PDF magic bytes.

    Guards against files with a .pdf extension that are actually saved HTML,
    error pages, or truncated downloads — those must never reach the PDF
    loader. Read errors count as invalid (the file is unusable either way).
    """
    try:
        with Path(path).open("rb") as f:
            return f.read(len(PDF_MAGIC)) == PDF_MAGIC
    except OSError:
        return False


def normalize_text(text: str) -> str:
    """NFKC-normalize and drop control chars except newline/tab.

    Ligatures and full-width forms become ordinary ASCII letters so retrieval
    matches typed queries. Micro sign (U+00B5) becomes Greek mu (U+03BC) via
    NFKC — call this on both stored chunks and the query string.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        ch
        for ch in text
        if ch in "\n\t" or unicodedata.category(ch)[0] != "C"
    ).strip()

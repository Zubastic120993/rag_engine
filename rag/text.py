"""Text normalization shared by ingest and query."""

from __future__ import annotations

import unicodedata


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

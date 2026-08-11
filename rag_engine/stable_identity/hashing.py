"""Hashing primitives for stable-id-v1."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    """Return lowercase SHA-256 hex digest of raw bytes."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("sha256_bytes requires bytes-like input")
    return hashlib.sha256(bytes(data)).hexdigest()


def sha256_utf8(text: str) -> str:
    """Return lowercase SHA-256 hex digest of UTF-8 encoded text."""
    if not isinstance(text, str):
        raise TypeError("sha256_utf8 requires str")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_hash_from_bytes(data: bytes) -> str:
    """SHA-256 of exact raw source file bytes (lowercase 64 hex, no prefix)."""
    return sha256_bytes(data)


def source_hash_from_file(path: str | Path) -> str:
    """SHA-256 of exact raw file bytes (streaming). Path does not affect digest."""
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# Phase 2 plan aliases
source_hash_bytes = source_hash_from_bytes
source_hash_file = source_hash_from_file


def content_hash(text: str) -> str:
    """SHA-256 of normalized extracted text (not file bytes).

    Normalization (documented decision matching ingest ``normalize_text``):
    - Unicode NFKC
    - drop Unicode control characters except ``\\n`` and ``\\t``
    - strip leading/trailing whitespace
    - UTF-8 encode the result
    - no CRLF→LF conversion beyond what NFKC already does (matches ingest)
    """
    # Local import avoids circular dependency with canonical.py.
    from rag_engine.stable_identity.canonical import normalize_content_text

    if not isinstance(text, str):
        raise TypeError("content_hash requires str")
    return sha256_utf8(normalize_content_text(text))


content_hash_text = content_hash

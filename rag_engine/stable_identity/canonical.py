"""Canonicalization helpers for stable-id-v1."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Mapping

from rag_engine.stable_identity.constants import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_EXTRACTOR,
    DEFAULT_EXTRACTOR_VERSION,
    DEFAULT_MAX_CHUNK_CHARS,
    DEFAULT_MIN_CHUNK_CHARS,
    DEFAULT_NORMALIZATION,
    DEFAULT_CHUNKING_SEPARATORS,
    IDENTITY_SCHEME_VERSION,
    SUBJECT_KEY_KINDS,
)
from rag_engine.stable_identity.hashing import sha256_utf8

# ASCII slug: lowercase letters, digits, underscore, hyphen, dot.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class CanonicalizationError(ValueError):
    """Raised when a value cannot be deterministically canonicalized."""


def normalize_content_text(text: str) -> str:
    """NFKC + drop controls except newline/tab + strip (ingest-aligned)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C"
    ).strip()


def normalize_subject_key_slug(key: str) -> str:
    """Normalize a subject key body to a lowercase ASCII slug.

    Spec §5.1 requires a lowercase ASCII slug. Case is folded; surrounding
    whitespace stripped; Unicode NFKC applied first. Empty / non-ASCII /
    delimiter characters are rejected.
    """
    if not isinstance(key, str):
        raise TypeError("subject key must be str")
    raw = unicodedata.normalize("NFKC", key).strip().lower()
    if not raw:
        raise CanonicalizationError("subject key must be non-empty")
    if ":" in raw:
        raise CanonicalizationError("subject key must not contain ':'")
    if any(unicodedata.category(ch)[0] == "C" for ch in raw):
        raise CanonicalizationError("subject key must not contain control characters")
    if not _SLUG_RE.fullmatch(raw):
        raise CanonicalizationError(
            "subject key must be a lowercase ASCII slug "
            "(start with alnum; then [a-z0-9._-]*)"
        )
    return raw


def validate_subject_key_kind(kind: str) -> str:
    if not isinstance(kind, str):
        raise TypeError("subject key kind must be str")
    kind = kind.strip()
    if kind not in SUBJECT_KEY_KINDS:
        raise CanonicalizationError(
            f"subject key kind must be one of {sorted(SUBJECT_KEY_KINDS)}"
        )
    return kind


def _canonical_json_value(value: Any) -> Any:
    """Convert a value to a JSON-serializable form under the supported model.

    Allowed: None, bool, int (not bool), str, list/tuple, mapping with str keys.
    Floats are rejected (current chunking contract does not require them).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalizationError(
            "floats are not allowed in chunking contracts (rejecting ambiguity)"
        )
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(v) for v in value]
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalizationError(
                    f"chunking contract keys must be str, got {type(k).__name__}"
                )
            out[k] = _canonical_json_value(v)
        return out
    raise CanonicalizationError(
        f"unsupported chunking contract value type: {type(value).__name__}"
    )


def canonicalize_chunking_contract(contract: Mapping[str, Any]) -> str:
    """Deterministic canonical JSON for a chunking contract.

    - mapping key order independent (``sort_keys=True``)
    - compact separators ``(",", ":")``
    - UTF-8 / ``ensure_ascii=False``
    - ``allow_nan=False``
    - unsupported types and floats fail closed
    """
    if not isinstance(contract, Mapping):
        raise TypeError("chunking contract must be a mapping")
    prepared = _canonical_json_value(contract)
    if not isinstance(prepared, dict):
        raise CanonicalizationError("chunking contract must canonicalize to object")
    return json.dumps(
        prepared,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def chunking_fingerprint(contract: Mapping[str, Any]) -> str:
    """SHA-256 of canonical JSON chunking contract (Spec §7.1).

    Preimage = UTF-8(canonical_json(contract)). The contract itself should
    include ``identity_scheme_version`` (see ``default_chunking_contract``).
    """
    return sha256_utf8(canonicalize_chunking_contract(contract))


def default_chunking_contract(
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    separators: tuple[str, ...] | list[str] = DEFAULT_CHUNKING_SEPARATORS,
    normalization: str = DEFAULT_NORMALIZATION,
    min_chunk_chars: int = DEFAULT_MIN_CHUNK_CHARS,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    extractor: str = DEFAULT_EXTRACTOR,
    extractor_version: str = DEFAULT_EXTRACTOR_VERSION,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
) -> dict[str, Any]:
    """Return a Spec §7.1-shaped contract (does not read production config)."""
    return {
        "identity_scheme_version": identity_scheme_version,
        "chunk_size": int(chunk_size),
        "chunk_overlap": int(chunk_overlap),
        "separators": list(separators),
        "normalization": normalization,
        "min_chunk_chars": int(min_chunk_chars),
        "max_chunk_chars": int(max_chunk_chars),
        "extractor": extractor,
        "extractor_version": extractor_version,
    }

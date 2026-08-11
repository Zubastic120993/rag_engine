"""Validation helpers for stable-id-v1 identifiers."""

from __future__ import annotations

import re
import uuid

from rag_engine.stable_identity.constants import (
    CHUNK_ID_BODY_HEX_LEN,
    CHUNK_ID_PREFIX,
    DOCUMENT_ID_PREFIX,
    SHA256_HEX_LEN,
    SUBJECT_KEY_KINDS,
    SUBJECT_KIND_KEY,
    SUBJECT_KIND_PENDING,
    SUBJECT_KIND_UUID,
    SUBJECT_PREFIX,
)

_HEX64_RE = re.compile(rf"^[0-9a-f]{{{SHA256_HEX_LEN}}}$")
_HEX32_RE = re.compile(rf"^[0-9a-f]{{{CHUNK_ID_BODY_HEX_LEN}}}$")


class IdentityValidationError(ValueError):
    """Raised when an identity value is malformed."""


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise IdentityValidationError(f"{name} must be str")
    return value


def validate_source_hash(value: str) -> str:
    """Accept exactly 64 lowercase hex characters (no prefix)."""
    s = _require_str(value, "source_hash")
    if not _HEX64_RE.fullmatch(s):
        raise IdentityValidationError(
            "source_hash must be exactly 64 lowercase hexadecimal characters"
        )
    return s


def validate_content_hash(value: str) -> str:
    """Content hash uses the same hex form as source_hash."""
    s = _require_str(value, "content_hash")
    if not _HEX64_RE.fullmatch(s):
        raise IdentityValidationError(
            "content_hash must be exactly 64 lowercase hexadecimal characters"
        )
    return s


def validate_chunking_fingerprint(value: str) -> str:
    s = _require_str(value, "chunking_fingerprint")
    if not _HEX64_RE.fullmatch(s):
        raise IdentityValidationError(
            "chunking_fingerprint must be exactly 64 lowercase hexadecimal characters"
        )
    return s


def validate_document_id(value: str) -> str:
    """Accept exactly ``docrev:<64-lowercase-hex>``."""
    s = _require_str(value, "document_id")
    if not s.startswith(DOCUMENT_ID_PREFIX):
        raise IdentityValidationError(
            f"document_id must start with {DOCUMENT_ID_PREFIX!r}"
        )
    body = s[len(DOCUMENT_ID_PREFIX) :]
    if not _HEX64_RE.fullmatch(body):
        raise IdentityValidationError(
            "document_id must be docrev: followed by 64 lowercase hex characters"
        )
    return s


def validate_chunk_id(value: str) -> str:
    """Accept exactly ``chunk:<32-lowercase-hex>``."""
    s = _require_str(value, "chunk_id")
    if not s.startswith(CHUNK_ID_PREFIX):
        raise IdentityValidationError(f"chunk_id must start with {CHUNK_ID_PREFIX!r}")
    body = s[len(CHUNK_ID_PREFIX) :]
    if not _HEX32_RE.fullmatch(body):
        raise IdentityValidationError(
            "chunk_id must be chunk: followed by exactly 32 lowercase hex characters"
        )
    return s


def validate_subject_id(value: str) -> str:
    """Accept Spec §5.1 subject_id forms; reject malformed prefixes/bodies.

    Does not silently canonicalize: key bodies must already be lowercase ASCII
    slugs; UUID bodies must already be lowercase RFC 4122 text.
    """
    # Local import keeps validation usable without pulling slug helpers early.
    from rag_engine.stable_identity.canonical import normalize_subject_key_slug
    from rag_engine.stable_identity.canonical import CanonicalizationError

    s = _require_str(value, "subject_id")
    if not s.startswith(SUBJECT_PREFIX):
        raise IdentityValidationError(f"subject_id must start with {SUBJECT_PREFIX!r}")
    body = s[len(SUBJECT_PREFIX) :]
    if body.startswith(f"{SUBJECT_KIND_KEY}:"):
        rest = body[len(SUBJECT_KIND_KEY) + 1 :]
        parts = rest.split(":", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise IdentityValidationError(
                "subject_id key form must be subj:key:<kind>:<normalized_key>"
            )
        kind, key = parts
        if kind not in SUBJECT_KEY_KINDS:
            raise IdentityValidationError(f"unknown subject key kind: {kind!r}")
        if ":" in key:
            raise IdentityValidationError("subject key body must not contain ':'")
        try:
            canonical = normalize_subject_key_slug(key)
        except (CanonicalizationError, TypeError) as exc:
            raise IdentityValidationError(
                f"subject key body is not a valid lowercase ASCII slug: {key!r}"
            ) from exc
        if key != canonical:
            raise IdentityValidationError(
                "subject key body must already be canonical lowercase ASCII slug "
                f"(got {key!r})"
            )
        return s
    if body.startswith(f"{SUBJECT_KIND_UUID}:"):
        u = body[len(SUBJECT_KIND_UUID) + 1 :]
        try:
            parsed = uuid.UUID(u)
        except ValueError as exc:
            raise IdentityValidationError(f"malformed subject UUID: {u!r}") from exc
        if str(parsed).lower() != u:
            raise IdentityValidationError(
                "subject UUID must be lowercase RFC 4122 8-4-4-4-12 form"
            )
        return s
    if body.startswith(f"{SUBJECT_KIND_PENDING}:"):
        h = body[len(SUBJECT_KIND_PENDING) + 1 :]
        validate_source_hash(h)
        return s
    raise IdentityValidationError(
        "subject_id must be subj:key:..., subj:uuid:..., or subj:pending:..."
    )


def require_nonneg_int_ordinal(ordinal: object) -> str:
    """Return canonical decimal text for a non-negative int ordinal.

    Rejects bools (which are ``int`` subclasses), floats, and negatives.
    """
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise IdentityValidationError(
            f"ordinal must be a non-negative int (not bool/float); got {type(ordinal).__name__}"
        )
    if ordinal < 0:
        raise IdentityValidationError("ordinal must be >= 0")
    return str(ordinal)

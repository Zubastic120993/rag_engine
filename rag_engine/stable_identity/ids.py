"""Identity construction helpers for stable-id-v1."""

from __future__ import annotations

import uuid
from pathlib import Path

from rag_engine.stable_identity.canonical import (
    CanonicalizationError,
    normalize_subject_key_slug,
    validate_subject_key_kind,
)
from rag_engine.stable_identity.constants import (
    CHUNK_ID_BODY_HEX_LEN,
    CHUNK_ID_PREFIX,
    DOCUMENT_ID_PREFIX,
    IDENTITY_SCHEME_VERSION,
    SUBJECT_KIND_KEY,
    SUBJECT_KIND_PENDING,
    SUBJECT_KIND_UUID,
    SUBJECT_PREFIX,
)
from rag_engine.stable_identity.hashing import (
    sha256_utf8,
    source_hash_from_bytes,
    source_hash_from_file,
)
from rag_engine.stable_identity.validation import (
    IdentityValidationError,
    require_nonneg_int_ordinal,
    validate_chunking_fingerprint,
    validate_document_id,
    validate_source_hash,
)


def document_id_from_source_hash(source_hash: str) -> str:
    """``document_id = "docrev:" + source_hash`` (Spec §6.1)."""
    validate_source_hash(source_hash)
    return f"{DOCUMENT_ID_PREFIX}{source_hash}"


def document_id_from_bytes(data: bytes) -> str:
    return document_id_from_source_hash(source_hash_from_bytes(data))


def document_id_from_file(path: str | Path) -> str:
    return document_id_from_source_hash(source_hash_from_file(path))


# Phase 2 plan alias
def make_document_id(source_hash: str) -> str:
    return document_id_from_source_hash(source_hash)


def subject_id_from_key(kind: str, key: str) -> str:
    """Build ``subj:key:<kind>:<normalized_key>`` (Spec §5.1).

    Does not derive keys from paths and does not infer document equivalence.
    """
    kind_n = validate_subject_key_kind(kind)
    key_n = normalize_subject_key_slug(key)
    return f"{SUBJECT_PREFIX}{SUBJECT_KIND_KEY}:{kind_n}:{key_n}"


make_key_subject_id = subject_id_from_key


def subject_id_from_uuid(value: str | uuid.UUID) -> str:
    """Build ``subj:uuid:<canonical-uuid>`` (lowercase 8-4-4-4-12)."""
    try:
        if isinstance(value, uuid.UUID):
            u = value
        else:
            u = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise IdentityValidationError(f"malformed UUID for subject_id: {value!r}") from exc
    return f"{SUBJECT_PREFIX}{SUBJECT_KIND_UUID}:{str(u).lower()}"


def subject_id_pending(source_hash: str) -> str:
    """Build ``subj:pending:<source_hash>`` (deterministic provisional form)."""
    validate_source_hash(source_hash)
    return f"{SUBJECT_PREFIX}{SUBJECT_KIND_PENDING}:{source_hash}"


make_provisional_subject_id = subject_id_pending


def chunk_id_preimage(
    document_id: str,
    chunking_fingerprint: str,
    ordinal: int,
    *,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
) -> str:
    """Exact UTF-8 preimage string hashed for ``chunk_id`` (Spec §7.1)."""
    validate_document_id(document_id)
    validate_chunking_fingerprint(chunking_fingerprint)
    ord_text = require_nonneg_int_ordinal(ordinal)
    return (
        f"{identity_scheme_version}|{document_id}|{chunking_fingerprint}|{ord_text}"
    )


def chunk_id(
    document_id: str,
    chunking_fingerprint: str,
    ordinal: int,
    *,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
) -> str:
    """``chunk:`` + first 32 hex chars of SHA-256(UTF-8(preimage))."""
    token = chunk_id_preimage(
        document_id,
        chunking_fingerprint,
        ordinal,
        identity_scheme_version=identity_scheme_version,
    )
    digest = sha256_utf8(token)[:CHUNK_ID_BODY_HEX_LEN]
    return f"{CHUNK_ID_PREFIX}{digest}"


def make_chunk_id(
    *,
    document_id: str,
    chunking_fingerprint: str,
    ordinal: int,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
) -> str:
    return chunk_id(
        document_id,
        chunking_fingerprint,
        ordinal,
        identity_scheme_version=identity_scheme_version,
    )


# Re-export for callers that hit CanonicalizationError via ids
__all__ = [
    "CanonicalizationError",
    "chunk_id",
    "chunk_id_preimage",
    "document_id_from_bytes",
    "document_id_from_file",
    "document_id_from_source_hash",
    "make_chunk_id",
    "make_document_id",
    "make_key_subject_id",
    "make_provisional_subject_id",
    "subject_id_from_key",
    "subject_id_from_uuid",
    "subject_id_pending",
]

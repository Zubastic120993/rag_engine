"""stable-id-v1 identity primitives (Phase 2 — not wired into production ingest).

Public API for deterministic subject / document / chunk identifiers.
This package is stdlib-only and must not import Chroma or LangChain.
"""

from __future__ import annotations

from rag_engine.stable_identity.canonical import (
    CanonicalizationError,
    canonicalize_chunking_contract,
    chunking_fingerprint,
    default_chunking_contract,
    normalize_content_text,
    normalize_subject_key_slug,
)
from rag_engine.stable_identity.collision import (
    IdentityCollisionError,
    assert_chunk_record_consistent,
    reject_conflicting_chunk_reuse,
    verify_chunk_id_matches_preimage,
)
from rag_engine.stable_identity.constants import (
    CHUNK_ID_BODY_HEX_LEN,
    CHUNK_ID_PREFIX,
    DOCUMENT_ID_PREFIX,
    IDENTITY_SCHEME_VERSION,
    REGISTRY_DOCUMENT_ID_COLUMN,
    REGISTRY_SUBJECT_ID_COLUMN,
    SUBJECT_KEY_KINDS,
)
from rag_engine.stable_identity.hashing import (
    content_hash,
    content_hash_text,
    sha256_bytes,
    sha256_utf8,
    source_hash_bytes,
    source_hash_file,
    source_hash_from_bytes,
    source_hash_from_file,
)
from rag_engine.stable_identity.ids import (
    chunk_id,
    chunk_id_preimage,
    document_id_from_bytes,
    document_id_from_file,
    document_id_from_source_hash,
    make_chunk_id,
    make_document_id,
    make_key_subject_id,
    make_provisional_subject_id,
    subject_id_from_key,
    subject_id_from_uuid,
    subject_id_pending,
)
from rag_engine.stable_identity.paths import (
    PathNormalizationError,
    normalize_relative_path,
)
from rag_engine.stable_identity.validation import (
    IdentityValidationError,
    require_nonneg_int_ordinal,
    validate_chunk_id,
    validate_chunking_fingerprint,
    validate_content_hash,
    validate_document_id,
    validate_source_hash,
    validate_subject_id,
)

__all__ = [
    "CHUNK_ID_BODY_HEX_LEN",
    "CHUNK_ID_PREFIX",
    "CanonicalizationError",
    "DOCUMENT_ID_PREFIX",
    "IDENTITY_SCHEME_VERSION",
    "IdentityCollisionError",
    "IdentityValidationError",
    "PathNormalizationError",
    "REGISTRY_DOCUMENT_ID_COLUMN",
    "REGISTRY_SUBJECT_ID_COLUMN",
    "SUBJECT_KEY_KINDS",
    "assert_chunk_record_consistent",
    "canonicalize_chunking_contract",
    "chunk_id",
    "chunk_id_preimage",
    "chunking_fingerprint",
    "content_hash",
    "content_hash_text",
    "default_chunking_contract",
    "document_id_from_bytes",
    "document_id_from_file",
    "document_id_from_source_hash",
    "make_chunk_id",
    "make_document_id",
    "make_key_subject_id",
    "make_provisional_subject_id",
    "normalize_content_text",
    "normalize_relative_path",
    "normalize_subject_key_slug",
    "reject_conflicting_chunk_reuse",
    "require_nonneg_int_ordinal",
    "sha256_bytes",
    "sha256_utf8",
    "source_hash_bytes",
    "source_hash_file",
    "source_hash_from_bytes",
    "source_hash_from_file",
    "subject_id_from_key",
    "subject_id_from_uuid",
    "subject_id_pending",
    "validate_chunk_id",
    "validate_chunking_fingerprint",
    "validate_content_hash",
    "validate_document_id",
    "validate_source_hash",
    "validate_subject_id",
    "verify_chunk_id_matches_preimage",
]

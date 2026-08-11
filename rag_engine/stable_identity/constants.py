"""Stable identity scheme constants (stable-id-v1)."""

from __future__ import annotations

IDENTITY_SCHEME_VERSION = "stable-id-v1"

DOCUMENT_ID_PREFIX = "docrev:"
CHUNK_ID_PREFIX = "chunk:"
SUBJECT_PREFIX = "subj:"

SUBJECT_KIND_KEY = "key"
SUBJECT_KIND_UUID = "uuid"
SUBJECT_KIND_PENDING = "pending"

# Spec §5.1 strong business key kinds.
SUBJECT_KEY_KINDS = frozenset(
    {
        "imo_doc",
        "sms",
        "sire",
        "reg",
        "maker_doc",
        "manual_family",
    }
)

SHA256_HEX_LEN = 64
CHUNK_ID_BODY_HEX_LEN = 32

# Terminology map (Phase-1 Spec §4.1) — do not silently rename the public API.
# Phase-1 subject_id  ↔ registry documents.document_id (logical lineage PK)
# Phase-1 document_id ↔ registry document_versions.document_version_id
REGISTRY_SUBJECT_ID_COLUMN = "document_id"  # registry logical PK column name
REGISTRY_DOCUMENT_ID_COLUMN = "document_version_id"  # registry revision PK

DEFAULT_CHUNKING_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ".", " ")
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100
DEFAULT_MIN_CHUNK_CHARS = 51
DEFAULT_MAX_CHUNK_CHARS = 2999
DEFAULT_NORMALIZATION = "nfkc"
DEFAULT_EXTRACTOR = "unknown"
DEFAULT_EXTRACTOR_VERSION = "UNKNOWN"

MAPPING_STATUS_LEGACY_UUID = "legacy_uuid"
MAPPING_STATUS_NATIVE_CHUNK_ID = "native_chunk_id"
MAPPING_STATUS_PENDING = "pending"
MAPPING_STATUSES = frozenset(
    {
        MAPPING_STATUS_LEGACY_UUID,
        MAPPING_STATUS_NATIVE_CHUNK_ID,
        MAPPING_STATUS_PENDING,
    }
)

"""Phase 6B embedding-fp-v1 constants (frozen Phase 6A contract)."""

from __future__ import annotations

from typing import Final

FINGERPRINT_SCHEMA_VERSION: Final = "embedding-fp-v1"

# Interim authoritative sidecar (distinct from v0 index_fingerprint.json).
SIDECAR_V1_NAME: Final = "index_embedding_fingerprint_v1.json"
SIDECAR_V0_NAME: Final = "index_fingerprint.json"

# Default physical collection used by langchain_chroma.Chroma.
DEFAULT_PHYSICAL_COLLECTION: Final = "langchain"
DEFAULT_VECTOR_STORE: Final = "chroma"
DEFAULT_DISTANCE_SPACE: Final = "l2"

DEFAULT_EMBEDDING_PROVIDER: Final = "ollama"
DEFAULT_EMBEDDING_MODE: Final = "symmetric_ollama_v1"
# Production-era mxbai-embed-large dimension (guardrail, not sufficiency).
DEFAULT_EMBEDDING_DIMENSION: Final = 1024

EMBEDDED_TEXT_COMPOSITION_VERSION: Final = "page_content_nfkc_v1"

SHA256_HEX_LEN: Final = 64

COMPAT_KNOWN_COMPATIBLE: Final = "KNOWN_COMPATIBLE"
COMPAT_KNOWN_INCOMPATIBLE: Final = "KNOWN_INCOMPATIBLE"
COMPAT_UNKNOWN_LEGACY: Final = "UNKNOWN_LEGACY"
COMPAT_CORRUPT: Final = "CORRUPT"
COMPAT_CONFLICT: Final = "CONFLICT"
COMPAT_UNSUPPORTED_SCHEMA: Final = "UNSUPPORTED_SCHEMA"
COMPAT_CONFIGURATION_ERROR: Final = "CONFIGURATION_ERROR"
COMPAT_EMPTY_UNINITIALIZED: Final = "EMPTY_UNINITIALIZED"

COMPATIBILITY_STATES: Final = frozenset(
    {
        COMPAT_KNOWN_COMPATIBLE,
        COMPAT_KNOWN_INCOMPATIBLE,
        COMPAT_UNKNOWN_LEGACY,
        COMPAT_CORRUPT,
        COMPAT_CONFLICT,
        COMPAT_UNSUPPORTED_SCHEMA,
        COMPAT_CONFIGURATION_ERROR,
        COMPAT_EMPTY_UNINITIALIZED,
    }
)

EMBEDDING_CONTRACT_FIELDS: Final = (
    "fingerprint_schema_version",
    "embedding_provider",
    "embedding_model",
    "embedding_model_revision",
    "embedding_dimension",
    "embedding_normalization",
    "embedding_mode",
    "tokenizer_id",
    "max_input_tokens",
)

CORPUS_CONTRACT_FIELDS: Final = (
    "identity_scheme_version",
    "chunk_size",
    "chunk_overlap",
    "separators",
    "normalization",
    "min_chunk_chars",
    "max_chunk_chars",
    "extractor",
    "extractor_version",
    "embedded_text_composition_version",
)

INDEX_CONTRACT_FIELDS: Final = (
    "fingerprint_schema_version",
    "embedding_fingerprint",
    "corpus_fingerprint",
    "vector_store",
    "distance_space",
    "physical_collection_name",
    "index_schema_notes",
)

# Stored authority envelope fields (not hashed into ifp).
STORED_AUTHORITY_FIELDS: Final = (
    "fingerprint_schema_version",
    "physical_collection_name",
    "index_fingerprint",
    "embedding_fingerprint",
    "corpus_fingerprint",
    "embedding_contract",
    "corpus_contract",
    "index_contract",
)

INGEST_ALLOWED_STATES: Final = frozenset(
    {
        COMPAT_KNOWN_COMPATIBLE,
        COMPAT_EMPTY_UNINITIALIZED,
    }
)

RETRIEVAL_ALLOWED_STATES: Final = frozenset(
    {
        COMPAT_KNOWN_COMPATIBLE,
        COMPAT_UNKNOWN_LEGACY,
        COMPAT_EMPTY_UNINITIALIZED,
    }
)

RETRIEVAL_DEGRADED_STATES: Final = frozenset({COMPAT_UNKNOWN_LEGACY})

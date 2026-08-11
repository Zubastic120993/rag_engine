"""Phase 6B embedding / index fingerprint compatibility enforcement.

Implements the frozen Phase 6A contract (embedding-fp-v1).
Does not certify or mutate production indexes on import.
"""

from __future__ import annotations

from rag_engine.index_compatibility.builders import (
    build_corpus_spec,
    build_embedding_spec,
    build_index_spec,
    build_runtime_contracts_from_config,
    stored_envelope_from_specs,
)
from rag_engine.index_compatibility.compatibility import (
    CompatibilityResult,
    evaluate_compatibility,
)
from rag_engine.index_compatibility.constants import (
    COMPAT_CONFIGURATION_ERROR,
    COMPAT_CONFLICT,
    COMPAT_CORRUPT,
    COMPAT_EMPTY_UNINITIALIZED,
    COMPAT_KNOWN_COMPATIBLE,
    COMPAT_KNOWN_INCOMPATIBLE,
    COMPAT_UNKNOWN_LEGACY,
    COMPAT_UNSUPPORTED_SCHEMA,
    FINGERPRINT_SCHEMA_VERSION,
    SIDECAR_V1_NAME,
)
from rag_engine.index_compatibility.exceptions import (
    FingerprintConfigurationError,
    FingerprintConflictError,
    FingerprintCorruptError,
    FingerprintError,
    FingerprintIncompatibleRetrievalError,
    FingerprintLegacyBlockedError,
    FingerprintMismatchError,
    FingerprintMissingError,
    FingerprintUnsupportedVersionError,
    IndexCompatibilityError,
)
from rag_engine.index_compatibility.policy import (
    doctor_fingerprint_report,
    enforce_ingest_compatibility,
    enforce_retrieval_compatibility,
    ensure_fingerprint_initialized_for_empty_index,
)
from rag_engine.index_compatibility.specs import (
    CorpusFingerprintSpec,
    EmbeddingFingerprintSpec,
    IndexFingerprintSpec,
    StoredIndexFingerprint,
    canonical_json,
)
from rag_engine.index_compatibility.state import (
    initialize_fingerprint_state,
    load_authoritative_state,
    sidecar_v1_path,
    write_sidecar_v1,
)

__all__ = [
    "COMPAT_CONFIGURATION_ERROR",
    "COMPAT_CONFLICT",
    "COMPAT_CORRUPT",
    "COMPAT_EMPTY_UNINITIALIZED",
    "COMPAT_KNOWN_COMPATIBLE",
    "COMPAT_KNOWN_INCOMPATIBLE",
    "COMPAT_UNKNOWN_LEGACY",
    "COMPAT_UNSUPPORTED_SCHEMA",
    "CompatibilityResult",
    "CorpusFingerprintSpec",
    "EmbeddingFingerprintSpec",
    "FINGERPRINT_SCHEMA_VERSION",
    "FingerprintConfigurationError",
    "FingerprintConflictError",
    "FingerprintCorruptError",
    "FingerprintError",
    "FingerprintIncompatibleRetrievalError",
    "FingerprintLegacyBlockedError",
    "FingerprintMismatchError",
    "FingerprintMissingError",
    "FingerprintUnsupportedVersionError",
    "IndexCompatibilityError",
    "IndexFingerprintSpec",
    "SIDECAR_V1_NAME",
    "StoredIndexFingerprint",
    "build_corpus_spec",
    "build_embedding_spec",
    "build_index_spec",
    "build_runtime_contracts_from_config",
    "canonical_json",
    "doctor_fingerprint_report",
    "enforce_ingest_compatibility",
    "enforce_retrieval_compatibility",
    "ensure_fingerprint_initialized_for_empty_index",
    "evaluate_compatibility",
    "initialize_fingerprint_state",
    "load_authoritative_state",
    "sidecar_v1_path",
    "stored_envelope_from_specs",
    "write_sidecar_v1",
]

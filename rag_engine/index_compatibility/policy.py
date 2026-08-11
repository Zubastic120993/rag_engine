"""Ingest / retrieval policy enforcement for embedding-fp-v1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rag_engine.index_compatibility.builders import (
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
    DEFAULT_PHYSICAL_COLLECTION,
)
from rag_engine.index_compatibility.exceptions import (
    FingerprintConfigurationError,
    FingerprintConflictError,
    FingerprintCorruptError,
    FingerprintIncompatibleRetrievalError,
    FingerprintLegacyBlockedError,
    FingerprintMismatchError,
    FingerprintMissingError,
    FingerprintUnsupportedVersionError,
    IndexCompatibilityError,
)
from rag_engine.index_compatibility.state import initialize_fingerprint_state


def _raise_for_ingest(result: CompatibilityResult) -> None:
    state = result.state
    details = result.to_dict()
    if state == COMPAT_KNOWN_COMPATIBLE:
        return
    if state == COMPAT_EMPTY_UNINITIALIZED:
        return
    if state == COMPAT_UNKNOWN_LEGACY:
        raise FingerprintLegacyBlockedError(
            "ingest append blocked: UNKNOWN_LEGACY index (no trustworthy fingerprint)",
            details=details,
        )
    if state == COMPAT_KNOWN_INCOMPATIBLE:
        raise FingerprintMismatchError(
            f"ingest append blocked: {result.reason}",
            details=details,
        )
    if state == COMPAT_CORRUPT:
        raise FingerprintCorruptError(
            f"ingest append blocked: {result.reason}",
            details=details,
        )
    if state == COMPAT_CONFLICT:
        raise FingerprintConflictError(
            f"ingest append blocked: {result.reason}",
            details=details,
        )
    if state == COMPAT_UNSUPPORTED_SCHEMA:
        raise FingerprintUnsupportedVersionError(
            f"ingest append blocked: {result.reason}",
            details=details,
        )
    if state == COMPAT_CONFIGURATION_ERROR:
        raise FingerprintConfigurationError(
            f"ingest append blocked: {result.reason}",
            details=details,
        )
    raise IndexCompatibilityError(
        f"ingest append blocked: unsupported compatibility state {state}",
        details=details,
    )


def enforce_ingest_compatibility(
    persist: str | Path,
    *,
    registry_db: str | Path | None = None,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
    vector_count: int | None = None,
) -> CompatibilityResult:
    """Fail closed before any digest-skip or vector mutation.

    Must be called before consulting embedded.json skip logic.
    """
    result = evaluate_compatibility(
        persist,
        registry_db=registry_db,
        physical_collection_name=physical_collection_name,
        vector_count=vector_count,
    )
    _raise_for_ingest(result)
    return result


def ensure_fingerprint_initialized_for_empty_index(
    persist: str | Path,
    *,
    registry_db: str | Path | None = None,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
    write_registry: bool = False,
    vector_count: int | None = None,
) -> CompatibilityResult:
    """If empty+uninitialized, write fingerprint authority before first vectors.

    Refuses to initialize when vectors already exist (legacy safety).
    """
    result = evaluate_compatibility(
        persist,
        registry_db=registry_db,
        physical_collection_name=physical_collection_name,
        vector_count=vector_count,
    )
    if result.state == COMPAT_KNOWN_COMPATIBLE:
        return result
    if result.state != COMPAT_EMPTY_UNINITIALIZED:
        _raise_for_ingest(result)
        return result

    if result.vector_count != 0:
        raise FingerprintMissingError(
            "refusing to initialize fingerprint: collection is not empty",
            details=result.to_dict(),
        )

    emb, corp, idx = build_runtime_contracts_from_config(
        physical_collection_name=physical_collection_name,
    )
    envelope = stored_envelope_from_specs(emb, corp, idx)
    initialize_fingerprint_state(
        persist,
        envelope,
        registry_db=registry_db,
        write_registry=write_registry,
    )
    # Re-evaluate; must now be compatible.
    after = evaluate_compatibility(
        persist,
        registry_db=registry_db if write_registry else None,
        physical_collection_name=physical_collection_name,
        vector_count=0,
    )
    if after.state != COMPAT_KNOWN_COMPATIBLE:
        raise IndexCompatibilityError(
            "fingerprint initialization did not yield KNOWN_COMPATIBLE",
            details=after.to_dict(),
        )
    return after


def enforce_retrieval_compatibility(
    persist: str | Path,
    *,
    registry_db: str | Path | None = None,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
    vector_count: int | None = None,
) -> CompatibilityResult:
    """Apply Phase 6A retrieval policy. Never mutates fingerprint state."""
    result = evaluate_compatibility(
        persist,
        registry_db=registry_db,
        physical_collection_name=physical_collection_name,
        vector_count=vector_count,
    )
    if result.retrieval_allowed:
        return result
    details = result.to_dict()
    if result.state == COMPAT_KNOWN_INCOMPATIBLE:
        raise FingerprintIncompatibleRetrievalError(
            f"retrieval blocked: {result.reason}",
            details=details,
        )
    if result.state == COMPAT_CORRUPT:
        raise FingerprintCorruptError(
            f"retrieval blocked: {result.reason}",
            details=details,
        )
    if result.state == COMPAT_CONFLICT:
        raise FingerprintConflictError(
            f"retrieval blocked: {result.reason}",
            details=details,
        )
    if result.state == COMPAT_UNSUPPORTED_SCHEMA:
        raise FingerprintUnsupportedVersionError(
            f"retrieval blocked: {result.reason}",
            details=details,
        )
    if result.state == COMPAT_CONFIGURATION_ERROR:
        raise FingerprintConfigurationError(
            f"retrieval blocked: {result.reason}",
            details=details,
        )
    raise FingerprintIncompatibleRetrievalError(
        f"retrieval blocked: {result.state}",
        details=details,
    )


def doctor_fingerprint_report(
    persist: str | Path,
    *,
    registry_db: str | Path | None = None,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
    vector_count: int | None = None,
) -> dict[str, Any]:
    """Diagnostic-only report. Never writes fingerprint state."""
    result = evaluate_compatibility(
        persist,
        registry_db=registry_db,
        physical_collection_name=physical_collection_name,
        vector_count=vector_count,
    )
    if result.state == COMPAT_KNOWN_COMPATIBLE:
        severity = "PASS"
        ok = True
    elif result.state in {COMPAT_UNKNOWN_LEGACY, COMPAT_EMPTY_UNINITIALIZED}:
        severity = "WARNING"
        ok = False  # not a silent pass; doctor should surface legacy/unknown
    else:
        severity = "FAIL"
        ok = False
    return {
        "ok": ok,
        "severity": severity,
        "state": result.state,
        "detail": result.reason,
        "compatibility": result.to_dict(),
    }

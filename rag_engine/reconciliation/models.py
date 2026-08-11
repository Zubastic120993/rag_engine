"""Phase 4 reconciliation models — deterministic, JSON-serializable, read-only."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


class ReconciliationState(str, Enum):
    """Primary Phase 4 roadmap states (exact set)."""

    MATCH = "MATCH"
    REGISTRY_ONLY = "REGISTRY_ONLY"
    CHROMA_ONLY = "CHROMA_ONLY"
    METADATA_MISMATCH = "METADATA_MISMATCH"
    CHUNK_COUNT_MISMATCH = "CHUNK_COUNT_MISMATCH"
    HASH_MISMATCH = "HASH_MISMATCH"
    DUPLICATE_ACTIVE = "DUPLICATE_ACTIVE"
    UNKNOWN = "UNKNOWN"


class ReasonCode(str, Enum):
    """Subordinate reason codes (do not replace primary state)."""

    TRACKER_ONLY = "TRACKER_ONLY"
    MISSING_SOURCE_FILE = "MISSING_SOURCE_FILE"
    MISSING_CHROMA_ID = "MISSING_CHROMA_ID"
    INVALID_LEGACY_ID = "INVALID_LEGACY_ID"
    PATH_DRIFT = "PATH_DRIFT"
    COLLECTION_MISMATCH = "COLLECTION_MISMATCH"
    ORDINAL_UNRESOLVED = "ORDINAL_UNRESOLVED"
    FINGERPRINT_UNKNOWN = "FINGERPRINT_UNKNOWN"
    SOURCE_HASH_UNVERIFIED = "SOURCE_HASH_UNVERIFIED"
    MULTI_PATH = "MULTI_PATH"
    ZERO_CHUNK = "ZERO_CHUNK"
    MALFORMED_TRACKER = "MALFORMED_TRACKER"
    REGISTRY_ENTITY = "REGISTRY_ENTITY"
    CHROMA_UNTRACKED = "CHROMA_UNTRACKED"
    DUPLICATE_CHUNK_OWNERSHIP = "DUPLICATE_CHUNK_OWNERSHIP"
    DUPLICATE_HASH_IDENTITY = "DUPLICATE_HASH_IDENTITY"
    PAGE_NOT_ORDINAL = "PAGE_NOT_ORDINAL"
    STABLE_CHUNK_PROVEN = "STABLE_CHUNK_PROVEN"


class BackfillReadiness(str, Enum):
    """Advisory only — never executed by Phase 4."""

    SAFE_MAPPING_CANDIDATE = "SAFE_MAPPING_CANDIDATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REINDEX_REQUIRED = "REINDEX_REQUIRED"
    UNRESOLVED = "UNRESOLVED"


class FingerprintEvidenceStatus(str, Enum):
    KNOWN_EXACT = "KNOWN_EXACT"
    KNOWN_BY_COHORT = "KNOWN_BY_COHORT"
    UNKNOWN = "UNKNOWN"
    AMBIGUOUS = "AMBIGUOUS"


class ExtractorVersionStatus(str, Enum):
    KNOWN = "KNOWN"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


# Deterministic primary-state precedence (lower index = higher priority).
# Applied when multiple conditions are present on one unit of reconciliation.
STATE_PRECEDENCE: tuple[ReconciliationState, ...] = (
    ReconciliationState.HASH_MISMATCH,
    ReconciliationState.DUPLICATE_ACTIVE,
    ReconciliationState.CHUNK_COUNT_MISMATCH,
    ReconciliationState.METADATA_MISMATCH,
    ReconciliationState.REGISTRY_ONLY,
    ReconciliationState.CHROMA_ONLY,
    ReconciliationState.MATCH,
    ReconciliationState.UNKNOWN,
)

_STATE_RANK = {s: i for i, s in enumerate(STATE_PRECEDENCE)}


def select_primary_state(candidates: set[ReconciliationState]) -> ReconciliationState:
    """Pick the highest-precedence state from a candidate set."""
    if not candidates:
        return ReconciliationState.UNKNOWN
    return min(candidates, key=lambda s: _STATE_RANK[s])


@dataclass(frozen=True)
class TrackerRecord:
    source_hash: str
    source_paths: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    collection: str | None
    ingested_at: str | None = None
    status: str | None = None
    raw_record: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_hash": self.source_hash,
            "source_paths": list(self.source_paths),
            "chunk_ids": list(self.chunk_ids),
            "collection": self.collection,
            "ingested_at": self.ingested_at,
            "status": self.status,
        }


@dataclass(frozen=True)
class ChromaRecord:
    chroma_embedding_id: str
    physical_collection_name: str
    source_path: str | None
    page: int | str | None
    collection_meta: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chroma_embedding_id": self.chroma_embedding_id,
            "physical_collection_name": self.physical_collection_name,
            "source_path": self.source_path,
            "page": self.page,
            "collection_meta": self.collection_meta,
            # metadata intentionally minimal; full blob not required for identity
            "metadata_keys": sorted(self.metadata.keys()),
        }


@dataclass(frozen=True)
class SourceObservation:
    path: str
    exists: bool
    source_hash: str | None
    normalized_relative_path: str | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegistrySnapshotRecord:
    """Minimal registry view for REGISTRY_ONLY comparisons (temp DBs in tests)."""

    subject_id: str | None
    document_id: str | None
    source_hash: str | None
    chunk_ids: tuple[str, ...]
    chroma_embedding_ids: tuple[str, ...]
    source_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "document_id": self.document_id,
            "source_hash": self.source_hash,
            "chunk_ids": list(self.chunk_ids),
            "chroma_embedding_ids": list(self.chroma_embedding_ids),
            "source_paths": list(self.source_paths),
        }


@dataclass(frozen=True)
class ReconciliationResult:
    state: ReconciliationState
    reason_codes: tuple[ReasonCode, ...]
    unit_kind: str  # tracker_digest | chroma_id | registry_document | registry_chunk
    unit_id: str
    subject_id: str | None = None
    document_id: str | None = None
    source_hash: str | None = None
    historical_source_hash: str | None = None
    current_observed_source_hash: str | None = None
    source_paths: tuple[str, ...] = ()
    tracker_chunk_ids: tuple[str, ...] = ()
    chroma_embedding_ids: tuple[str, ...] = ()
    stable_chunk_ids: tuple[str | None, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    backfill_readiness: BackfillReadiness = BackfillReadiness.UNRESOLVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason_codes": [r.value for r in self.reason_codes],
            "unit_kind": self.unit_kind,
            "unit_id": self.unit_id,
            "subject_id": self.subject_id,
            "document_id": self.document_id,
            "source_hash": self.source_hash,
            "historical_source_hash": self.historical_source_hash,
            "current_observed_source_hash": self.current_observed_source_hash,
            "source_paths": list(self.source_paths),
            "tracker_chunk_ids": list(self.tracker_chunk_ids),
            "chroma_embedding_ids": list(self.chroma_embedding_ids),
            "stable_chunk_ids": list(self.stable_chunk_ids),
            "evidence": dict(self.evidence),
            "backfill_readiness": self.backfill_readiness.value,
        }


@dataclass(frozen=True)
class ReconciliationSummary:
    total: int
    by_state: Mapping[str, int]
    tracker_records: int
    tracker_chunk_ids: int
    chroma_records: int
    stable_chunk_ids_proven: int
    stable_chunk_ids_unresolved: int
    missing_source_files: int
    historical_fingerprint_status: str
    extractor_version_status: str
    join: Mapping[str, int]
    hash_stats: Mapping[str, int]
    path_stats: Mapping[str, int]
    chunk_count_stats: Mapping[str, int]
    backfill_readiness: Mapping[str, int]
    extras: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_state": dict(self.by_state),
            "tracker_records": self.tracker_records,
            "tracker_chunk_ids": self.tracker_chunk_ids,
            "chroma_records": self.chroma_records,
            "stable_chunk_ids_proven": self.stable_chunk_ids_proven,
            "stable_chunk_ids_unresolved": self.stable_chunk_ids_unresolved,
            "missing_source_files": self.missing_source_files,
            "historical_fingerprint_status": self.historical_fingerprint_status,
            "extractor_version_status": self.extractor_version_status,
            "join": dict(self.join),
            "hash_stats": dict(self.hash_stats),
            "path_stats": dict(self.path_stats),
            "chunk_count_stats": dict(self.chunk_count_stats),
            "backfill_readiness": dict(self.backfill_readiness),
            "extras": dict(self.extras),
        }

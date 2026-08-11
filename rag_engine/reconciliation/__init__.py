"""Phase 4 read-only Registry ↔ Chroma / tracker reconciliation.

Import has no side effects: does not open production DBs, create registries,
scan the library, or trigger ingest.
"""

from __future__ import annotations

from rag_engine.reconciliation.chroma_reader import (
    ChromaReadError,
    audit_chroma,
    load_chroma_snapshot_readonly,
    open_chroma_sqlite_readonly,
)
from rag_engine.reconciliation.engine import reconcile, reconcile_paths
from rag_engine.reconciliation.fingerprint_evidence import (
    classify_extractor_version,
    classify_fingerprint_evidence,
    read_index_fingerprint_readonly,
)
from rag_engine.reconciliation.models import (
    STATE_PRECEDENCE,
    BackfillReadiness,
    ChromaRecord,
    ExtractorVersionStatus,
    FingerprintEvidenceStatus,
    ReasonCode,
    ReconciliationResult,
    ReconciliationState,
    ReconciliationSummary,
    RegistrySnapshotRecord,
    SourceObservation,
    TrackerRecord,
    select_primary_state,
)
from rag_engine.reconciliation.registry_snapshot import (
    RegistrySnapshotError,
    load_registry_snapshot_readonly,
)
from rag_engine.reconciliation.report import (
    results_to_jsonable,
    sample_by_state,
    summarize_reconciliation,
    write_json_report,
    write_markdown_summary,
)
from rag_engine.reconciliation.source_observer import (
    SourceHashCache,
    observe_paths,
    observe_source_path,
)
from rag_engine.reconciliation.tracker_reader import (
    TrackerReadError,
    audit_tracker,
    load_tracker_readonly,
)

__all__ = [
    "STATE_PRECEDENCE",
    "BackfillReadiness",
    "ChromaReadError",
    "ChromaRecord",
    "ExtractorVersionStatus",
    "FingerprintEvidenceStatus",
    "ReasonCode",
    "ReconciliationResult",
    "ReconciliationState",
    "ReconciliationSummary",
    "RegistrySnapshotError",
    "RegistrySnapshotRecord",
    "SourceHashCache",
    "SourceObservation",
    "TrackerReadError",
    "TrackerRecord",
    "audit_chroma",
    "audit_tracker",
    "classify_extractor_version",
    "classify_fingerprint_evidence",
    "load_chroma_snapshot_readonly",
    "load_registry_snapshot_readonly",
    "load_tracker_readonly",
    "observe_paths",
    "observe_source_path",
    "open_chroma_sqlite_readonly",
    "read_index_fingerprint_readonly",
    "reconcile",
    "reconcile_paths",
    "results_to_jsonable",
    "sample_by_state",
    "select_primary_state",
    "summarize_reconciliation",
    "write_json_report",
    "write_markdown_summary",
]

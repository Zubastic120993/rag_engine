"""Read-only pre-move evidence collection (Roadmap v7 Phase E0).

Collects source-side indexed authority and destination absence proof before any
filesystem MOVE or metadata mutation. Never writes to library, registry,
tracker, Chroma, journals, or locks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from rag_engine.library_state.contract import (
    CLASS_INDEXED_OK,
    INTENT_PLAN_MOVE,
    RESULT_AMBIGUOUS,
    RESULT_BLOCKED,
    RESULT_FAILED,
)
from rag_engine.library_state.evidence import gather_store_handles
from rag_engine.library_state.plan import OperationPlan, TargetClassification
from rag_engine.library_state.resolver import resolve_library_state
from rag_engine.stable_identity import PathNormalizationError, normalize_relative_path

_NON_BLOCKING_COMPAT_STATES = frozenset({"KNOWN_COMPATIBLE", "EMPTY_UNINITIALIZED"})
_CONTAINMENT_GAP_PREFIX = "candidate_path_containment:"


class PreMoveEvidenceError(ValueError):
    """Raised when pre-move evidence preconditions fail."""


@dataclass(frozen=True)
class PreMoveEvidence:
    """Immutable source-side pre-move authority snapshot."""

    source_path: str
    destination_path: str
    source_file_sha256: str
    document_id: str
    source_hash: str
    plan: OperationPlan
    request_id: str
    plan_digest: str
    approved_vector_ids: tuple[str, ...]
    resolver_classification: str
    proposed_operation: str
    compatibility_state: str | None
    library_root: str
    persist_dir: str
    registry_db: str
    tracker_path: str | None


def collect_pre_move_evidence(
    *,
    library_root: str | Path,
    persist_dir: str | Path,
    registry_db: str | Path,
    tracker_path: str | Path,
    source_path: str,
    destination_path: str,
    journal_path: str | Path | None = None,
    operation_id: str | None = None,
) -> PreMoveEvidence:
    """Collect read-only pre-move evidence for a source ? absent destination MOVE."""
    root = _require_absolute_dir(library_root, "library_root")
    normalized_source = _validate_library_relative_path(source_path, field="source_path")
    normalized_destination = _validate_library_relative_path(
        destination_path,
        field="destination_path",
    )
    if normalized_source == normalized_destination:
        raise PreMoveEvidenceError(
            "source_path and destination_path must differ"
        )

    source_abs = root / normalized_source
    if not source_abs.is_file():
        raise PreMoveEvidenceError(
            "source path must exist as a regular file inside library_root"
        )

    dest_abs = root / normalized_destination
    if dest_abs.exists():
        raise PreMoveEvidenceError("destination path must not exist")

    _validate_destination_parent(root, normalized_destination)

    handles = gather_store_handles(
        persist_dir=persist_dir,
        registry_db=registry_db,
        tracker_path=tracker_path,
    )
    persist = _require_absolute_dir(handles["persist_dir"], "persist_dir")
    registry = _require_absolute_file(handles["registry_db"], "registry_db")
    tracker = handles["tracker_path"]
    tracker_text = str(tracker.resolve()) if tracker is not None else None

    source_file_sha256 = hashlib.sha256(source_abs.read_bytes()).hexdigest()

    plan = resolve_library_state(
        INTENT_PLAN_MOVE,
        [normalized_source],
        library_root=root,
        persist_dir=persist,
        registry_db=registry,
        tracker_path=tracker,
        journal_path=journal_path,
        operation_id=operation_id,
    )
    _validate_source_plan(plan, source_path=normalized_source)

    source_item = _classification_for_target(plan, target_path=normalized_source)
    if source_item.document_id is None or source_item.source_hash is None:
        raise PreMoveEvidenceError(
            "source classification missing document_id or source_hash"
        )
    if source_item.source_hash != source_file_sha256:
        raise PreMoveEvidenceError(
            "source hash disagrees with filesystem SHA-256"
        )

    approved_vector_ids = _approved_vector_ids(plan, source_item=source_item)
    generation = plan.authority_snapshot.get("generation") or {}
    compatibility_state = (
        str(generation["state"]) if generation.get("state") is not None else None
    )

    digest = _plan_digest(plan)
    return PreMoveEvidence(
        source_path=normalized_source,
        destination_path=normalized_destination,
        source_file_sha256=source_file_sha256,
        document_id=str(source_item.document_id),
        source_hash=str(source_item.source_hash),
        plan=plan,
        request_id=plan.request_id,
        plan_digest=digest,
        approved_vector_ids=approved_vector_ids,
        resolver_classification=plan.classification,
        proposed_operation=plan.proposed_operation,
        compatibility_state=compatibility_state,
        library_root=str(root),
        persist_dir=str(persist),
        registry_db=str(registry),
        tracker_path=tracker_text,
    )


def _plan_digest(plan: OperationPlan) -> str:
    return hashlib.sha256(plan.to_canonical_json().encode("utf-8")).hexdigest()


def _validate_source_plan(plan: OperationPlan, *, source_path: str) -> None:
    if plan.intent != INTENT_PLAN_MOVE:
        raise PreMoveEvidenceError(
            f"resolver plan intent must be {INTENT_PLAN_MOVE!r}"
        )
    if plan.classification != CLASS_INDEXED_OK:
        raise PreMoveEvidenceError(
            f"source-side classification must be {CLASS_INDEXED_OK!r}; "
            f"got {plan.classification!r}"
        )
    if plan.result in {RESULT_AMBIGUOUS, RESULT_BLOCKED, RESULT_FAILED}:
        raise PreMoveEvidenceError(
            f"source-side resolver result {plan.result!r} is ineligible for MOVE"
        )
    if plan.ambiguity_flags:
        raise PreMoveEvidenceError(
            "source-side resolver reported ambiguity flags"
        )

    generation = plan.authority_snapshot.get("generation") or {}
    compat_state = generation.get("state")
    if compat_state and compat_state not in _NON_BLOCKING_COMPAT_STATES:
        raise PreMoveEvidenceError(
            f"generation compatibility conflict: {compat_state!r}"
        )

    for gap in plan.evidence_gaps:
        if gap.startswith(_CONTAINMENT_GAP_PREFIX):
            raise PreMoveEvidenceError(
                f"source authority path containment issue: {gap}"
            )

    source_item = _classification_for_target(plan, target_path=source_path)
    if source_item.classification != CLASS_INDEXED_OK:
        raise PreMoveEvidenceError(
            f"source target classification must be {CLASS_INDEXED_OK!r}"
        )
    _validate_source_id_evidence(source_item)


def _validate_source_id_evidence(item: TargetClassification) -> None:
    comparisons = dict(item.evidence.get("id_comparisons") or {})
    for key, comp in comparisons.items():
        counts = dict(comp.get("counts") or {})
        if int(counts.get("missing", 0)) > 0 or int(counts.get("unexpected", 0)) > 0:
            raise PreMoveEvidenceError(
                f"source vector evidence disagreement in {key!r}"
            )


def _approved_vector_ids(
    plan: OperationPlan,
    *,
    source_item: TargetClassification,
) -> tuple[str, ...]:
    raw = plan.authority_snapshot.get("query_ids") or []
    ids = tuple(str(x) for x in raw if x)
    if ids:
        return ids
    comparisons = dict(source_item.evidence.get("id_comparisons") or {})
    for comp in comparisons.values():
        only = comp.get("only_in_first") or comp.get("only_in_second")
        if isinstance(only, list) and only:
            return tuple(str(x) for x in only)
    return ()


def _classification_for_target(
    plan: OperationPlan,
    *,
    target_path: str,
) -> TargetClassification:
    matches = [c for c in plan.classifications if c.target == target_path]
    if len(matches) == 1:
        return matches[0]
    raise PreMoveEvidenceError(
        f"resolver plan has no classification for source_path {target_path!r}"
    )


def _validate_destination_parent(library_root: Path, destination_rel: str) -> None:
    dest_abs = library_root / destination_rel
    parent = dest_abs.parent
    if not parent.exists():
        raise PreMoveEvidenceError("destination parent directory does not exist")
    if not parent.is_dir():
        raise PreMoveEvidenceError("destination parent is not a directory")
    try:
        parent.resolve().relative_to(library_root.resolve())
    except ValueError as exc:
        raise PreMoveEvidenceError(
            "destination parent resolves outside library_root"
        ) from exc
    try:
        dest_abs.resolve().relative_to(library_root.resolve())
    except (ValueError, OSError) as exc:
        raise PreMoveEvidenceError(
            "destination path resolves outside library_root via symlink"
        ) from exc


def _validate_library_relative_path(raw: str, *, field: str) -> str:
    if not isinstance(raw, str):
        raise PreMoveEvidenceError(f"{field} must be a relative path string")
    if "\0" in raw:
        raise PreMoveEvidenceError(f"{field} must not contain NUL")
    if not raw.strip():
        raise PreMoveEvidenceError(f"{field} must be non-empty")
    if raw != raw.strip():
        raise PreMoveEvidenceError(
            f"{field} must not contain leading/trailing whitespace"
        )
    if raw.startswith("/") or raw.startswith("\\"):
        raise PreMoveEvidenceError(f"{field} must not be absolute")
    if ".." in raw.replace("\\", "/").split("/"):
        raise PreMoveEvidenceError(f"{field} must not contain traversal segments")
    try:
        return normalize_relative_path(raw)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        raise PreMoveEvidenceError(f"invalid {field}: {exc}") from exc


def _require_absolute_dir(raw: Path | str | None, field: str) -> Path:
    if raw is None:
        raise PreMoveEvidenceError(f"{field} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise PreMoveEvidenceError(f"{field} must be an absolute path")
    if not path.is_dir():
        raise PreMoveEvidenceError(f"{field} must be an existing directory")
    return path.resolve()


def _require_absolute_file(raw: Path | str | None, field: str) -> Path:
    if raw is None:
        raise PreMoveEvidenceError(f"{field} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise PreMoveEvidenceError(f"{field} must be an absolute path")
    if not path.is_file():
        raise PreMoveEvidenceError(f"{field} must be an existing file")
    return path.resolve()

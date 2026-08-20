"""Read-only quarantine-delete preflight evidence (DELETE Phase A).

Collects target-side duplicate authority, operator-designated retained copy proof,
and quarantine destination absence before any filesystem DELETE or store mutation.
Never writes to library, registry, tracker, Chroma, journals, locks, or approvals.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from rag_engine.library_state.contract import (
    APPROVAL_REQUIRED,
    CLASS_EXACT_DUPLICATE,
    EMBEDDING_NONE,
    INTENT_PLAN_DELETE,
    OP_RETIREMENT_PROPOSAL,
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


class QuarantineDeletePreflightError(ValueError):
    """Raised when quarantine-delete preflight preconditions fail."""


@dataclass(frozen=True)
class QuarantineDeleteEvidence:
    """Immutable pre-quarantine-delete authority snapshot."""

    target_path: str
    retained_path: str
    quarantine_path: str
    target_file_sha256: str
    document_id: str
    source_hash: str
    plan: OperationPlan
    request_id: str
    plan_digest: str
    approved_vector_ids: tuple[str, ...]
    retained_aliases: tuple[str, ...]
    resolver_classification: str
    proposed_operation: str
    compatibility_state: str | None
    library_root: str
    persist_dir: str
    registry_db: str
    tracker_path: str | None


def collect_quarantine_delete_evidence(
    *,
    library_root: str | Path,
    persist_dir: str | Path,
    registry_db: str | Path,
    tracker_path: str | Path,
    target_path: str,
    retained_path: str,
    quarantine_path: str,
) -> QuarantineDeleteEvidence:
    """Collect read-only quarantine-delete evidence for one bounded exact duplicate."""
    root = _require_absolute_dir(library_root, "library_root")
    normalized_target = _validate_library_relative_path(target_path, field="target_path")
    normalized_retained = _validate_library_relative_path(retained_path, field="retained_path")
    normalized_quarantine = _validate_library_relative_path(
        quarantine_path,
        field="quarantine_path",
    )
    _reject_path_collisions(
        normalized_target,
        normalized_retained,
        normalized_quarantine,
    )

    target_abs = _require_regular_file(root, normalized_target, field="target_path")
    retained_abs = _require_regular_file(root, normalized_retained, field="retained_path")
    _reject_symlink_entry(target_abs, field="target_path")
    _reject_symlink_entry(retained_abs, field="retained_path")

    quarantine_abs = root / normalized_quarantine
    if quarantine_abs.exists():
        raise QuarantineDeletePreflightError("quarantine path must not exist")
    _validate_quarantine_parent(root, normalized_quarantine)

    handles = gather_store_handles(
        persist_dir=persist_dir,
        registry_db=registry_db,
        tracker_path=tracker_path,
    )
    persist = _require_absolute_dir(handles["persist_dir"], "persist_dir")
    registry = _require_absolute_file(handles["registry_db"], "registry_db")
    tracker = handles["tracker_path"]
    tracker_text = str(tracker.resolve()) if tracker is not None else None

    target_sha256 = hashlib.sha256(target_abs.read_bytes()).hexdigest()
    retained_sha256 = hashlib.sha256(retained_abs.read_bytes()).hexdigest()
    if retained_sha256 != target_sha256:
        raise QuarantineDeletePreflightError(
            "retained file SHA-256 must exactly equal target SHA-256"
        )

    plan = resolve_library_state(
        INTENT_PLAN_DELETE,
        [normalized_target],
        library_root=root,
        persist_dir=persist,
        registry_db=registry,
        tracker_path=tracker,
    )
    _validate_delete_plan(plan, target_path=normalized_target)

    target_item = _classification_for_target(plan, target_path=normalized_target)
    if target_item.document_id is None or target_item.source_hash is None:
        raise QuarantineDeletePreflightError(
            "target classification missing document_id or source_hash"
        )
    if target_item.source_hash != target_sha256:
        raise QuarantineDeletePreflightError(
            "target source_hash disagrees with filesystem SHA-256"
        )

    retained_aliases = _retained_aliases(target_item)
    if normalized_retained not in retained_aliases:
        raise QuarantineDeletePreflightError(
            "retained_path is not among resolver-proven retained aliases"
        )
    if normalized_retained == normalized_target:
        raise QuarantineDeletePreflightError(
            "retained_path must not equal target_path"
        )
    if normalized_retained == normalized_quarantine:
        raise QuarantineDeletePreflightError(
            "retained_path must not equal quarantine_path"
        )

    approved_vector_ids = _approved_vector_ids(plan, target_item=target_item)
    generation = plan.authority_snapshot.get("generation") or {}
    compatibility_state = (
        str(generation["state"]) if generation.get("state") is not None else None
    )

    digest = _plan_digest(plan)
    return QuarantineDeleteEvidence(
        target_path=normalized_target,
        retained_path=normalized_retained,
        quarantine_path=normalized_quarantine,
        target_file_sha256=target_sha256,
        document_id=str(target_item.document_id),
        source_hash=str(target_item.source_hash),
        plan=plan,
        request_id=plan.request_id,
        plan_digest=digest,
        approved_vector_ids=approved_vector_ids,
        retained_aliases=retained_aliases,
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


def _reject_path_collisions(target: str, retained: str, quarantine: str) -> None:
    if len({target, retained, quarantine}) != 3:
        raise QuarantineDeletePreflightError(
            "target_path, retained_path, and quarantine_path must be distinct"
        )


def _validate_delete_plan(plan: OperationPlan, *, target_path: str) -> None:
    if plan.intent != INTENT_PLAN_DELETE:
        raise QuarantineDeletePreflightError(
            f"resolver plan intent must be {INTENT_PLAN_DELETE!r}"
        )
    if plan.classification != CLASS_EXACT_DUPLICATE:
        raise QuarantineDeletePreflightError(
            f"target-side classification must be {CLASS_EXACT_DUPLICATE!r}; "
            f"got {plan.classification!r}"
        )
    if plan.proposed_operation != OP_RETIREMENT_PROPOSAL:
        raise QuarantineDeletePreflightError(
            f"proposed_operation must be {OP_RETIREMENT_PROPOSAL!r}; "
            f"got {plan.proposed_operation!r}"
        )
    if plan.approval != APPROVAL_REQUIRED:
        raise QuarantineDeletePreflightError(
            f"approval must be {APPROVAL_REQUIRED!r}; got {plan.approval!r}"
        )
    if plan.embedding_action != EMBEDDING_NONE:
        raise QuarantineDeletePreflightError(
            f"embedding_action must be {EMBEDDING_NONE!r}"
        )
    if plan.result in {RESULT_AMBIGUOUS, RESULT_BLOCKED, RESULT_FAILED}:
        raise QuarantineDeletePreflightError(
            f"resolver result {plan.result!r} is ineligible for quarantine delete"
        )
    if plan.ambiguity_flags:
        raise QuarantineDeletePreflightError(
            "resolver reported ambiguity flags"
        )

    generation = plan.authority_snapshot.get("generation") or {}
    compat_state = generation.get("state")
    if compat_state and compat_state not in _NON_BLOCKING_COMPAT_STATES:
        raise QuarantineDeletePreflightError(
            f"generation compatibility conflict: {compat_state!r}"
        )

    for gap in plan.evidence_gaps:
        if gap.startswith(_CONTAINMENT_GAP_PREFIX):
            raise QuarantineDeletePreflightError(
                f"target authority path containment issue: {gap}"
            )
        if _is_blocking_evidence_gap(gap):
            raise QuarantineDeletePreflightError(
                f"resolver reported unresolved store evidence: {gap}"
            )

    target_item = _classification_for_target(plan, target_path=target_path)
    if target_item.classification != CLASS_EXACT_DUPLICATE:
        raise QuarantineDeletePreflightError(
            f"target classification must be {CLASS_EXACT_DUPLICATE!r}"
        )
    _validate_target_id_evidence(target_item)
    retained_aliases = _retained_aliases(target_item)
    if not retained_aliases:
        raise QuarantineDeletePreflightError(
            "last/live retainer not proven: retained_aliases is empty"
        )


_NON_BLOCKING_EVIDENCE_GAPS = frozenset(
    {
        "journal:no_exact_locator",
        "journal:exact_locator_missing",
        "generation_fingerprint_absent",
    }
)


def _is_blocking_evidence_gap(gap: str) -> bool:
    if gap in _NON_BLOCKING_EVIDENCE_GAPS:
        return False
    if gap.startswith("candidate_path_containment:"):
        return False
    blocking_prefixes = (
        "registry_read_error",
        "tracker_read_error",
        "chroma_read_error",
        "registry_absent",
        "tracker_absent",
        "chroma_absent",
        "chroma_query_ids_empty",
        "registry_vector_map_absent",
    )
    return gap.startswith(blocking_prefixes) or gap in blocking_prefixes


def _retained_aliases(item: TargetClassification) -> tuple[str, ...]:
    raw = item.evidence.get("retained_aliases") or item.evidence.get("live_other_paths") or []
    if not isinstance(raw, (list, tuple)):
        raise QuarantineDeletePreflightError("retained_aliases must be a list")
    aliases = tuple(sorted(str(x) for x in raw if x))
    return aliases


def _validate_target_id_evidence(item: TargetClassification) -> None:
    comparisons = dict(item.evidence.get("id_comparisons") or {})
    for key, comp in comparisons.items():
        counts = dict(comp.get("counts") or {})
        if int(counts.get("missing", 0)) > 0 or int(counts.get("unexpected", 0)) > 0:
            raise QuarantineDeletePreflightError(
                f"target vector evidence disagreement in {key!r}"
            )


def _approved_vector_ids(
    plan: OperationPlan,
    *,
    target_item: TargetClassification,
) -> tuple[str, ...]:
    raw = plan.authority_snapshot.get("query_ids") or []
    ids = tuple(str(x) for x in raw if x)
    if ids:
        return ids
    comparisons = dict(target_item.evidence.get("id_comparisons") or {})
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
    raise QuarantineDeletePreflightError(
        f"resolver plan has no classification for target_path {target_path!r}"
    )


def _require_regular_file(root: Path, rel: str, *, field: str) -> Path:
    path = root / rel
    if not path.is_file():
        raise QuarantineDeletePreflightError(
            f"{field} must exist as a regular file inside library_root"
        )
    return path


def _reject_symlink_entry(path: Path, *, field: str) -> None:
    if path.is_symlink():
        raise QuarantineDeletePreflightError(f"{field} must not be a symlink")


def _validate_quarantine_parent(library_root: Path, quarantine_rel: str) -> None:
    dest_abs = library_root / quarantine_rel
    parent = dest_abs.parent
    if not parent.exists():
        raise QuarantineDeletePreflightError("quarantine parent directory does not exist")
    if not parent.is_dir():
        raise QuarantineDeletePreflightError("quarantine parent is not a directory")
    try:
        parent.resolve().relative_to(library_root.resolve())
    except ValueError as exc:
        raise QuarantineDeletePreflightError(
            "quarantine parent resolves outside library_root"
        ) from exc
    try:
        dest_abs.resolve().relative_to(library_root.resolve())
    except (ValueError, OSError) as exc:
        raise QuarantineDeletePreflightError(
            "quarantine path resolves outside library_root via symlink"
        ) from exc


def _validate_library_relative_path(raw: str, *, field: str) -> str:
    if not isinstance(raw, str):
        raise QuarantineDeletePreflightError(f"{field} must be a relative path string")
    if "\0" in raw:
        raise QuarantineDeletePreflightError(f"{field} must not contain NUL")
    if not raw.strip():
        raise QuarantineDeletePreflightError(f"{field} must be non-empty")
    if raw != raw.strip():
        raise QuarantineDeletePreflightError(
            f"{field} must not contain leading/trailing whitespace"
        )
    if raw.startswith("/") or raw.startswith("\\"):
        raise QuarantineDeletePreflightError(f"{field} must not be absolute")
    if ".." in raw.replace("\\", "/").split("/"):
        raise QuarantineDeletePreflightError(f"{field} must not contain traversal segments")
    try:
        return normalize_relative_path(raw)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        raise QuarantineDeletePreflightError(f"invalid {field}: {exc}") from exc


def _require_absolute_dir(raw: Path | str | None, field: str) -> Path:
    if raw is None:
        raise QuarantineDeletePreflightError(f"{field} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise QuarantineDeletePreflightError(f"{field} must be an absolute path")
    if not path.is_dir():
        raise QuarantineDeletePreflightError(f"{field} must be an existing directory")
    return path.resolve()


def _require_absolute_file(raw: Path | str | None, field: str) -> Path:
    if raw is None:
        raise QuarantineDeletePreflightError(f"{field} is required")
    path = Path(raw)
    if not path.is_absolute():
        raise QuarantineDeletePreflightError(f"{field} must be an absolute path")
    if not path.is_file():
        raise QuarantineDeletePreflightError(f"{field} must be an existing file")
    return path.resolve()

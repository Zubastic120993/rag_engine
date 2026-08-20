"""Bounded explicit-target MOVE metadata reconciliation backend.

Updates only an approved pair of V5 locators, one tracker record, and approved
Chroma embedding IDs. Never moves filesystem bytes or scans Chroma by source path.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from rag_engine.index_compatibility.chroma_inspect import chroma_sqlite_path
from rag_engine.index_compatibility.constants import COMPAT_KNOWN_COMPATIBLE
from rag_engine.library_state.evidence import (
    CHROMA_ID_BATCH_SIZE,
    lookup_chroma_by_embedding_ids,
)
from rag_engine.library_state.move_approval import MoveApprovalValidationResult
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    LOCATOR_COMPENSATION_FAILED,
    LOCATOR_COMPENSATION_PENDING,
    LOCATOR_COMPENSATION_REJECTED,
    LOCATOR_EVENT_MOVED,
    LOCATOR_INACTIVE,
    RegistryValidationError,
    get_locator_lifecycle_state,
    list_locator_lifecycle_states_for_document,
    open_registry,
    record_locator_move_transition,
    registry_transaction,
    restore_locator_move_states_after_failure,
    validate_move_compensation_restorable,
    verify_locator_state_event_consistency,
)
from rag_engine.reconciliation.chroma_reader import ChromaReadError
from rag_engine.stable_identity import PathNormalizationError, normalize_relative_path

_COMPENSATION_SOURCE = "explicit_move_reconciliation_compensation"
_APPLY_SOURCE = "explicit_move_reconciliation"

_MOVE_NEW_ELIGIBLE_PRIOR = frozenset(
    {
        LOCATOR_ACTIVE,
        LOCATOR_INACTIVE,
        LOCATOR_COMPENSATION_REJECTED,
        LOCATOR_COMPENSATION_FAILED,
    }
)


class ExplicitMoveReconciliationError(ValueError):
    """Raised when explicit MOVE reconciliation preconditions fail."""


@dataclass(frozen=True)
class ExplicitMoveReconciliationRequest:
    """Bounded explicit-target MOVE reconciliation input."""

    approval: MoveApprovalValidationResult
    registry_db: str
    persist_dir: str
    tracker_path: str
    document_id: str
    source_hash: str
    old_relative_path: str
    new_relative_path: str
    old_source_file_id: str
    new_source_file_id: str
    approved_vector_ids: tuple[str, ...]
    compatibility_evidence: Mapping[str, Any]
    operation_id: str | None = None


@dataclass(frozen=True)
class ExplicitMoveReconciliationPreview:
    """Read-only intended deltas for one bounded MOVE reconciliation."""

    document_id: str
    source_hash: str
    old_relative_path: str
    new_relative_path: str
    old_source_file_id: str
    new_source_file_id: str
    approved_vector_ids: tuple[str, ...]
    compatibility_state: str
    registry_delta: Mapping[str, Any]
    tracker_delta: Mapping[str, Any]
    chroma_delta: Mapping[str, str]


@dataclass(frozen=True)
class ExplicitMoveReconciliationResult:
    """Outcome of an explicit MOVE reconciliation apply attempt."""

    success: bool
    operation_id: str
    registry_transition: Mapping[str, Any] | None = None
    tracker_updated: bool = False
    chroma_ids_updated: tuple[str, ...] = ()
    verified: bool = False
    compensated: bool = False
    residual_unrecovered: tuple[str, ...] = ()
    error_message: str | None = None


@dataclass
class _BoundedSnapshots:
    old_locator_state: dict[str, Any] | None
    new_locator_state: dict[str, Any] | None
    third_locator_states: dict[str, dict[str, Any] | None]
    tracker_record: dict[str, Any]
    tracker_root: dict[str, Any]
    chroma_sources: dict[str, str | None]
    chroma_collections: dict[str, str | None]


TrackerWriter = Callable[[Path, dict[str, Any]], None]
ChromaSourceUpdater = Callable[[Path, Mapping[str, str]], None]


def preview_explicit_move_reconciliation(
    request: ExplicitMoveReconciliationRequest,
) -> ExplicitMoveReconciliationPreview:
    """Read-only preview of exact registry/tracker/Chroma deltas. No writes."""
    ctx = _validate_request(request, require_operation_id=False)
    return ExplicitMoveReconciliationPreview(
        document_id=ctx["document_id"],
        source_hash=ctx["source_hash"],
        old_relative_path=ctx["old_path"],
        new_relative_path=ctx["new_path"],
        old_source_file_id=ctx["old_source_file_id"],
        new_source_file_id=ctx["new_source_file_id"],
        approved_vector_ids=ctx["approved_vector_ids"],
        compatibility_state=ctx["compatibility_state"],
        registry_delta={
            "action": "record_locator_move_transition",
            "document_id": ctx["document_id"],
            "old_source_file_id": ctx["old_source_file_id"],
            "new_source_file_id": ctx["new_source_file_id"],
            "old_activity_state": LOCATOR_ACTIVE,
            "new_activity_state": LOCATOR_ACTIVE,
            "old_result_activity_state": LOCATOR_INACTIVE,
        },
        tracker_delta={
            "source_hash": ctx["source_hash"],
            "paths_before": [ctx["old_path"]],
            "paths_after": [ctx["new_path"]],
            "chunk_ids": list(ctx["approved_vector_ids"]),
        },
        chroma_delta={
            vector_id: ctx["new_path"] for vector_id in ctx["approved_vector_ids"]
        },
    )


def apply_explicit_move_reconciliation(
    request: ExplicitMoveReconciliationRequest,
    *,
    preview: ExplicitMoveReconciliationPreview | None = None,
    tracker_writer: TrackerWriter | None = None,
    chroma_updater: ChromaSourceUpdater | None = None,
) -> ExplicitMoveReconciliationResult:
    """Apply bounded MOVE metadata reconciliation with bounded compensation."""
    ctx = _validate_request(request, require_operation_id=True)
    if preview is not None:
        _assert_preview_matches(ctx, preview)

    operation_id = str(request.operation_id).strip()
    registry_path = Path(ctx["registry_db"])
    tracker_file = Path(ctx["tracker_path"])
    chroma_file = chroma_sqlite_path(ctx["persist_dir"])
    write_tracker_fn = tracker_writer or _write_tracker_atomic
    update_chroma_fn = chroma_updater or _update_chroma_source_metadata

    snapshots = _capture_snapshots(ctx, tracker_file=tracker_file, chroma_file=chroma_file)
    registry_transition: dict[str, Any] | None = None
    tracker_updated = False
    chroma_updated_ids: tuple[str, ...] = ()
    compensated = False
    residual: list[str] = []

    try:
        with open_registry(registry_path) as conn:
            with registry_transaction(conn):
                registry_transition = record_locator_move_transition(
                    conn,
                    document_id=ctx["document_id"],
                    old_source_file_id=ctx["old_source_file_id"],
                    new_source_file_id=ctx["new_source_file_id"],
                    operation_id=operation_id,
                    source=_APPLY_SOURCE,
                )

        updated_tracker = _build_tracker_after_move(snapshots.tracker_root, ctx)
        write_tracker_fn(tracker_file, updated_tracker)
        tracker_updated = True

        chroma_delta = {
            vector_id: ctx["new_path"] for vector_id in ctx["approved_vector_ids"]
        }
        update_chroma_fn(chroma_file, chroma_delta)
        chroma_updated_ids = ctx["approved_vector_ids"]

        _verify_post_apply(
            ctx,
            registry_path=registry_path,
            tracker_file=tracker_file,
            chroma_file=chroma_file,
            operation_id=operation_id,
            snapshots=snapshots,
        )
        return ExplicitMoveReconciliationResult(
            success=True,
            operation_id=operation_id,
            registry_transition=registry_transition,
            tracker_updated=True,
            chroma_ids_updated=chroma_updated_ids,
            verified=True,
        )
    except Exception as exc:  # noqa: BLE001 - bounded compensation boundary
        residual.extend(
            _compensate_bounded_state(
                ctx,
                snapshots=snapshots,
                registry_path=registry_path,
                tracker_file=tracker_file,
                chroma_file=chroma_file,
                operation_id=operation_id,
                registry_transition=registry_transition,
                tracker_updated=tracker_updated,
                chroma_updated_ids=chroma_updated_ids,
                write_tracker_fn=write_tracker_fn,
                update_chroma_fn=update_chroma_fn,
            )
        )
        return ExplicitMoveReconciliationResult(
            success=False,
            operation_id=operation_id,
            registry_transition=registry_transition,
            tracker_updated=tracker_updated and not residual,
            chroma_ids_updated=chroma_updated_ids,
            verified=False,
            compensated=not residual,
            residual_unrecovered=tuple(residual),
            error_message=str(exc),
        )


def _validate_request(
    request: ExplicitMoveReconciliationRequest,
    *,
    require_operation_id: bool,
) -> dict[str, Any]:
    if not isinstance(request.approval, MoveApprovalValidationResult):
        raise ExplicitMoveReconciliationError(
            "approval must be a MoveApprovalValidationResult"
        )
    approval = request.approval

    registry_db = _require_absolute_path(request.registry_db, "registry_db")
    persist_dir = _require_absolute_path(request.persist_dir, "persist_dir")
    tracker_path = _require_absolute_path(request.tracker_path, "tracker_path")

    old_path = _require_relative_path(request.old_relative_path, "old_relative_path")
    new_path = _require_relative_path(request.new_relative_path, "new_relative_path")
    if old_path == new_path:
        raise ExplicitMoveReconciliationError(
            "old_relative_path and new_relative_path must differ"
        )

    document_id = _require_non_empty_str(request.document_id, "document_id")
    source_hash = _require_non_empty_str(request.source_hash, "source_hash")
    old_sf = _require_non_empty_str(request.old_source_file_id, "old_source_file_id")
    new_sf = _require_non_empty_str(request.new_source_file_id, "new_source_file_id")
    if old_sf == new_sf:
        raise ExplicitMoveReconciliationError(
            "old_source_file_id and new_source_file_id must differ"
        )

    if approval.source_path != old_path:
        raise ExplicitMoveReconciliationError("approval source_path mismatch")
    if approval.destination_path != new_path:
        raise ExplicitMoveReconciliationError("approval destination_path mismatch")
    if approval.document_id != document_id:
        raise ExplicitMoveReconciliationError("approval document_id mismatch")
    if approval.source_hash != source_hash:
        raise ExplicitMoveReconciliationError("approval source_hash mismatch")

    approved_vector_ids = _normalize_vector_ids(request.approved_vector_ids)
    compatibility_state = _validate_compatibility_evidence(
        request.compatibility_evidence,
        vector_count=len(approved_vector_ids),
    )

    operation_id: str | None = None
    if require_operation_id:
        if request.operation_id is None or not str(request.operation_id).strip():
            raise ExplicitMoveReconciliationError(
                "operation_id is required for apply"
            )
        operation_id = str(request.operation_id).strip()
    elif request.operation_id is not None and str(request.operation_id).strip():
        operation_id = str(request.operation_id).strip()

    _validate_registry_locators(
        registry_db=registry_db,
        document_id=document_id,
        old_source_file_id=old_sf,
        new_source_file_id=new_sf,
    )
    _validate_tracker_evidence(
        tracker_path=tracker_path,
        source_hash=source_hash,
        old_path=old_path,
        approved_vector_ids=approved_vector_ids,
        document_id=document_id,
    )
    _validate_chroma_physical_row_uniqueness(
        persist_dir=persist_dir,
        approved_vector_ids=approved_vector_ids,
    )
    _validate_chroma_evidence(
        persist_dir=persist_dir,
        old_path=old_path,
        document_id=document_id,
        source_hash=source_hash,
        approved_vector_ids=approved_vector_ids,
    )

    return {
        "registry_db": registry_db,
        "persist_dir": persist_dir,
        "tracker_path": tracker_path,
        "document_id": document_id,
        "source_hash": source_hash,
        "old_path": old_path,
        "new_path": new_path,
        "old_source_file_id": old_sf,
        "new_source_file_id": new_sf,
        "approved_vector_ids": approved_vector_ids,
        "compatibility_state": compatibility_state,
        "operation_id": operation_id,
    }


def _assert_preview_matches(ctx: dict[str, Any], preview: ExplicitMoveReconciliationPreview) -> None:
    if preview.document_id != ctx["document_id"]:
        raise ExplicitMoveReconciliationError("preview document_id mismatch")
    if preview.old_source_file_id != ctx["old_source_file_id"]:
        raise ExplicitMoveReconciliationError("preview old_source_file_id mismatch")
    if preview.new_source_file_id != ctx["new_source_file_id"]:
        raise ExplicitMoveReconciliationError("preview new_source_file_id mismatch")
    if tuple(preview.approved_vector_ids) != ctx["approved_vector_ids"]:
        raise ExplicitMoveReconciliationError("preview approved_vector_ids mismatch")
    if preview.old_relative_path != ctx["old_path"]:
        raise ExplicitMoveReconciliationError("preview old_relative_path mismatch")
    if preview.new_relative_path != ctx["new_path"]:
        raise ExplicitMoveReconciliationError("preview new_relative_path mismatch")


def _require_absolute_path(raw: str, field: str) -> str:
    text = _require_non_empty_str(raw, field)
    path = Path(text)
    if not path.is_absolute():
        raise ExplicitMoveReconciliationError(f"{field} must be an absolute path")
    return str(path)


def _require_relative_path(raw: str, field: str) -> str:
    if "\0" in raw:
        raise ExplicitMoveReconciliationError(f"{field} must not contain NUL")
    try:
        return normalize_relative_path(raw)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        raise ExplicitMoveReconciliationError(f"invalid {field}: {exc}") from exc


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ExplicitMoveReconciliationError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ExplicitMoveReconciliationError(
            f"{field} must not contain leading/trailing whitespace"
        )
    return value


def _normalize_vector_ids(raw: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ExplicitMoveReconciliationError(
            "approved_vector_ids must be a sequence of strings"
        )
    if not raw:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _require_non_empty_str(item, "approved_vector_ids entry")
        if text in seen:
            raise ExplicitMoveReconciliationError(
                "approved_vector_ids must not contain duplicates"
            )
        seen.add(text)
        out.append(text)
    return tuple(out)


def _validate_compatibility_evidence(
    evidence: Mapping[str, Any],
    *,
    vector_count: int,
) -> str:
    if not isinstance(evidence, Mapping):
        raise ExplicitMoveReconciliationError(
            "compatibility_evidence must be a mapping"
        )
    state = evidence.get("state")
    if not isinstance(state, str) or not state.strip():
        raise ExplicitMoveReconciliationError(
            "compatibility_evidence.state is required"
        )
    state = state.strip()
    if vector_count > 0 and state != COMPAT_KNOWN_COMPATIBLE:
        raise ExplicitMoveReconciliationError(
            "nonzero vector count requires compatibility_evidence.state "
            f"{COMPAT_KNOWN_COMPATIBLE!r}"
        )
    return state


def _validate_registry_locators(
    *,
    registry_db: str,
    document_id: str,
    old_source_file_id: str,
    new_source_file_id: str,
) -> None:
    with open_registry(registry_db) as conn:
        old_row = conn.execute(
            "SELECT source_file_id, document_id FROM source_files WHERE source_file_id = ?",
            (old_source_file_id,),
        ).fetchone()
        new_row = conn.execute(
            "SELECT source_file_id, document_id FROM source_files WHERE source_file_id = ?",
            (new_source_file_id,),
        ).fetchone()
        if old_row is None:
            raise ExplicitMoveReconciliationError(
                f"old_source_file_id {old_source_file_id!r} is not registered"
            )
        if new_row is None:
            raise ExplicitMoveReconciliationError(
                f"new_source_file_id {new_source_file_id!r} is not registered"
            )
        if old_row["document_id"] != document_id:
            raise ExplicitMoveReconciliationError(
                "old_source_file_id is not bound to document_id"
            )
        if new_row["document_id"] != document_id:
            raise ExplicitMoveReconciliationError(
                "new_source_file_id is not bound to document_id"
            )

        old_state = get_locator_lifecycle_state(conn, source_file_id=old_source_file_id)
        new_state = get_locator_lifecycle_state(conn, source_file_id=new_source_file_id)
        if old_state is None:
            raise ExplicitMoveReconciliationError(
                "old locator has no V5 lifecycle state projection"
            )
        if new_state is None:
            raise ExplicitMoveReconciliationError(
                "new locator has no V5 lifecycle state projection"
            )
        if old_state["activity_state"] != LOCATOR_ACTIVE:
            raise ExplicitMoveReconciliationError(
                "old locator activity_state must be ACTIVE"
            )
        new_activity = new_state["activity_state"]
        if new_activity == LOCATOR_COMPENSATION_PENDING:
            raise ExplicitMoveReconciliationError(
                "new locator activity_state COMPENSATION_PENDING is ineligible"
            )
        if new_activity not in _MOVE_NEW_ELIGIBLE_PRIOR:
            raise ExplicitMoveReconciliationError(
                f"new locator activity_state {new_activity!r} is ineligible for MOVE"
            )
        try:
            validate_move_compensation_restorable(new_prior_activity_state=new_activity)
        except RegistryValidationError as exc:
            raise ExplicitMoveReconciliationError(str(exc)) from exc


def _validate_tracker_evidence(
    *,
    tracker_path: str,
    source_hash: str,
    old_path: str,
    approved_vector_ids: tuple[str, ...],
    document_id: str,
) -> None:
    root = _read_tracker_json(Path(tracker_path))
    record = root.get(source_hash)
    if not isinstance(record, dict):
        raise ExplicitMoveReconciliationError(
            "tracker has no record for source_hash"
        )
    paths = _tracker_paths(record)
    if paths != (old_path,):
        raise ExplicitMoveReconciliationError(
            "tracker paths must exactly match the old relative path"
        )
    chunk_ids = tuple(str(x) for x in (record.get("chunk_ids") or []))
    if chunk_ids != approved_vector_ids:
        raise ExplicitMoveReconciliationError(
            "tracker chunk_ids must exactly match approved_vector_ids"
        )
    tracker_doc = record.get("document_id")
    if tracker_doc is not None and str(tracker_doc) != document_id:
        raise ExplicitMoveReconciliationError("tracker document_id mismatch")


def _validate_chroma_evidence(
    *,
    persist_dir: str,
    old_path: str,
    document_id: str,
    source_hash: str,
    approved_vector_ids: tuple[str, ...],
) -> None:
    if not approved_vector_ids:
        return
    chroma_file = chroma_sqlite_path(persist_dir)
    if not chroma_file.is_file():
        raise ExplicitMoveReconciliationError("chroma.sqlite3 is missing")
    try:
        lookup = lookup_chroma_by_embedding_ids(chroma_file, approved_vector_ids)
    except ChromaReadError as exc:
        raise ExplicitMoveReconciliationError(str(exc)) from exc
    if lookup.missing_ids:
        raise ExplicitMoveReconciliationError(
            f"approved Chroma IDs missing: {list(lookup.missing_ids)}"
        )
    if tuple(sorted(lookup.found_ids)) != tuple(sorted(approved_vector_ids)):
        raise ExplicitMoveReconciliationError(
            "Chroma lookup returned unexpected ID set"
        )
    for rec in lookup.records:
        if rec.source_path != old_path:
            raise ExplicitMoveReconciliationError(
                f"Chroma source metadata mismatch for {rec.chroma_embedding_id!r}"
            )
        meta_doc = rec.metadata.get("document_id")
        if meta_doc is None:
            raise ExplicitMoveReconciliationError(
                f"Chroma document_id metadata missing for {rec.chroma_embedding_id!r}"
            )
        if str(meta_doc) != document_id:
            raise ExplicitMoveReconciliationError(
                f"Chroma document_id metadata mismatch for {rec.chroma_embedding_id!r}"
            )
        meta_hash = rec.metadata.get("source_hash")
        if meta_hash is None:
            raise ExplicitMoveReconciliationError(
                f"Chroma source_hash metadata missing for {rec.chroma_embedding_id!r}"
            )
        if str(meta_hash) != source_hash:
            raise ExplicitMoveReconciliationError(
                f"Chroma source_hash metadata mismatch for {rec.chroma_embedding_id!r}"
            )


def _chroma_id_batches(
    embedding_ids: tuple[str, ...],
    *,
    batch_size: int = CHROMA_ID_BATCH_SIZE,
) -> list[tuple[str, ...]]:
    if batch_size < 1:
        raise ExplicitMoveReconciliationError("Chroma batch_size must be >= 1")
    return [
        tuple(embedding_ids[i : i + batch_size])
        for i in range(0, len(embedding_ids), batch_size)
    ]


def _validate_chroma_physical_row_uniqueness(
    *,
    persist_dir: str,
    approved_vector_ids: tuple[str, ...],
) -> None:
    if not approved_vector_ids:
        return
    chroma_file = chroma_sqlite_path(persist_dir)
    if not chroma_file.is_file():
        raise ExplicitMoveReconciliationError("chroma.sqlite3 is missing")
    conn = sqlite3.connect(str(chroma_file))
    try:
        for batch in _chroma_id_batches(approved_vector_ids):
            placeholders = ",".join("?" * len(batch))
            for row in conn.execute(
                "SELECT embedding_id, COUNT(*) AS c FROM embeddings "
                f"WHERE embedding_id IN ({placeholders}) GROUP BY embedding_id",
                batch,
            ):
                eid = str(row[0])
                count = int(row[1])
                if count != 1:
                    raise ExplicitMoveReconciliationError(
                        f"Chroma embedding_id {eid!r} must have exactly one physical row; "
                        f"found {count}"
                    )
            found = {
                str(row[0])
                for row in conn.execute(
                    "SELECT embedding_id FROM embeddings "
                    f"WHERE embedding_id IN ({placeholders})",
                    batch,
                )
            }
            missing = [eid for eid in batch if eid not in found]
            if missing:
                raise ExplicitMoveReconciliationError(
                    f"approved Chroma IDs missing physical rows: {missing}"
                )
    finally:
        conn.close()


def _capture_snapshots(
    ctx: dict[str, Any],
    *,
    tracker_file: Path,
    chroma_file: Path,
) -> _BoundedSnapshots:
    with open_registry(ctx["registry_db"]) as conn:
        all_states = list_locator_lifecycle_states_for_document(
            conn, document_id=ctx["document_id"]
        )
        state_by_id = {row["source_file_id"]: row for row in all_states}
        old_state = copy.deepcopy(state_by_id.get(ctx["old_source_file_id"]))
        new_state = copy.deepcopy(state_by_id.get(ctx["new_source_file_id"]))
        third = {
            sf: copy.deepcopy(state_by_id[sf])
            for sf in state_by_id
            if sf not in {ctx["old_source_file_id"], ctx["new_source_file_id"]}
        }

    tracker_root = _read_tracker_json(tracker_file)
    tracker_record = copy.deepcopy(tracker_root[ctx["source_hash"]])

    chroma_sources: dict[str, str | None] = {}
    chroma_collections: dict[str, str | None] = {}
    if ctx["approved_vector_ids"] and chroma_file.is_file():
        lookup = lookup_chroma_by_embedding_ids(chroma_file, ctx["approved_vector_ids"])
        for rec in lookup.records:
            chroma_sources[rec.chroma_embedding_id] = rec.source_path
            chroma_collections[rec.chroma_embedding_id] = rec.collection_meta

    return _BoundedSnapshots(
        old_locator_state=old_state,
        new_locator_state=new_state,
        third_locator_states=third,
        tracker_record=tracker_record,
        tracker_root=copy.deepcopy(tracker_root),
        chroma_sources=chroma_sources,
        chroma_collections=chroma_collections,
    )


def _build_tracker_after_move(tracker_root: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(tracker_root)
    record = copy.deepcopy(updated[ctx["source_hash"]])
    record["paths"] = [ctx["new_path"]]
    updated[ctx["source_hash"]] = record
    return updated


def _build_tracker_after_quarantine(
    tracker_root: dict[str, Any],
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Remove target path from bounded tracker record; retain selected retained path."""
    updated = copy.deepcopy(tracker_root)
    record = copy.deepcopy(updated[ctx["source_hash"]])
    retained = ctx["retained_path"]
    target = ctx["target_path"]
    paths = list(record.get("paths") or [])
    new_paths = [p for p in paths if p != target]
    if retained not in new_paths:
        new_paths = [retained]
    else:
        new_paths = [retained]
    record["paths"] = new_paths
    updated[ctx["source_hash"]] = record
    return updated


def _write_tracker_atomic(path: Path, tracker: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(tracker, indent=2, ensure_ascii=False) + "\n"
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_tracker_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ExplicitMoveReconciliationError(f"tracker file missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ExplicitMoveReconciliationError("tracker root must be a JSON object")
    return data


def _tracker_paths(record: Mapping[str, Any]) -> tuple[str, ...]:
    raw = record.get("paths") or []
    if isinstance(raw, str):
        return (normalize_relative_path(raw),)
    if isinstance(raw, list):
        return tuple(normalize_relative_path(str(p)) for p in raw if p)
    return ()


def _update_chroma_source_metadata(
    chroma_file: Path,
    updates: Mapping[str, str],
) -> None:
    if not updates:
        return
    conn = sqlite3.connect(str(chroma_file))
    try:
        for embedding_id, new_source in updates.items():
            rows = conn.execute(
                "SELECT id FROM embeddings WHERE embedding_id = ?",
                (embedding_id,),
            ).fetchall()
            if len(rows) == 0:
                raise ExplicitMoveReconciliationError(
                    f"Chroma embedding_id missing during update: {embedding_id!r}"
                )
            if len(rows) != 1:
                raise ExplicitMoveReconciliationError(
                    f"Chroma embedding_id {embedding_id!r} has duplicate physical rows "
                    f"during update: found {len(rows)}"
                )
            row_id = int(rows[0][0])
            existing = conn.execute(
                "SELECT string_value FROM embedding_metadata "
                "WHERE id = ? AND key = 'source'",
                (row_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) "
                    "VALUES (?, 'source', ?)",
                    (row_id, new_source),
                )
            else:
                conn.execute(
                    "UPDATE embedding_metadata SET string_value = ? "
                    "WHERE id = ? AND key = 'source'",
                    (new_source, row_id),
                )
        conn.commit()
    finally:
        conn.close()


def _verify_post_apply(
    ctx: dict[str, Any],
    *,
    registry_path: Path,
    tracker_file: Path,
    chroma_file: Path,
    operation_id: str,
    snapshots: _BoundedSnapshots,
) -> None:
    with open_registry(registry_path) as conn:
        old_state = get_locator_lifecycle_state(
            conn, source_file_id=ctx["old_source_file_id"]
        )
        new_state = get_locator_lifecycle_state(
            conn, source_file_id=ctx["new_source_file_id"]
        )
        if old_state is None or old_state["activity_state"] != LOCATOR_INACTIVE:
            raise ExplicitMoveReconciliationError(
                "post-apply old locator is not INACTIVE"
            )
        if new_state is None or new_state["activity_state"] != LOCATOR_ACTIVE:
            raise ExplicitMoveReconciliationError(
                "post-apply new locator is not ACTIVE"
            )

        move_events = conn.execute(
            "SELECT source_file_id, related_event_id, new_state FROM "
            "source_file_locator_lifecycle_events "
            "WHERE operation_id = ? AND event_type = ?",
            (operation_id, LOCATOR_EVENT_MOVED),
        ).fetchall()
        if len(move_events) != 2:
            raise ExplicitMoveReconciliationError(
                "post-apply MOVE linkage must contain exactly two events"
            )

        for sf in (ctx["old_source_file_id"], ctx["new_source_file_id"]):
            check = verify_locator_state_event_consistency(conn, source_file_id=sf)
            if not check["consistent"]:
                raise ExplicitMoveReconciliationError(
                    f"post-apply locator consistency failed for {sf!r}"
                )

        current_states = {
            row["source_file_id"]: row
            for row in list_locator_lifecycle_states_for_document(
                conn, document_id=ctx["document_id"]
            )
        }
        for sf, before in snapshots.third_locator_states.items():
            after = current_states.get(sf)
            if after is None or after["activity_state"] != before["activity_state"]:
                raise ExplicitMoveReconciliationError(
                    f"unexpected third locator state change for {sf!r}"
                )

    tracker_root = _read_tracker_json(tracker_file)
    record = tracker_root[ctx["source_hash"]]
    if _tracker_paths(record) != (ctx["new_path"],):
        raise ExplicitMoveReconciliationError("post-apply tracker path mismatch")
    if tuple(str(x) for x in (record.get("chunk_ids") or [])) != ctx["approved_vector_ids"]:
        raise ExplicitMoveReconciliationError("post-apply tracker chunk_ids changed")

    if ctx["approved_vector_ids"]:
        lookup = lookup_chroma_by_embedding_ids(chroma_file, ctx["approved_vector_ids"])
        if tuple(sorted(lookup.found_ids)) != tuple(sorted(ctx["approved_vector_ids"])):
            raise ExplicitMoveReconciliationError("post-apply Chroma ID set mismatch")
        for rec in lookup.records:
            if rec.source_path != ctx["new_path"]:
                raise ExplicitMoveReconciliationError(
                    "post-apply Chroma source metadata mismatch"
                )


def _compensate_bounded_state(
    ctx: dict[str, Any],
    *,
    snapshots: _BoundedSnapshots,
    registry_path: Path,
    tracker_file: Path,
    chroma_file: Path,
    operation_id: str,
    registry_transition: dict[str, Any] | None,
    tracker_updated: bool,
    chroma_updated_ids: tuple[str, ...],
    write_tracker_fn: TrackerWriter,
    update_chroma_fn: ChromaSourceUpdater,
) -> list[str]:
    residual: list[str] = []

    if chroma_updated_ids:
        try:
            restore = {
                eid: snapshots.chroma_sources.get(eid)
                for eid in chroma_updated_ids
                if snapshots.chroma_sources.get(eid) is not None
            }
            if restore:
                update_chroma_fn(
                    chroma_file,
                    {eid: src for eid, src in restore.items() if src is not None},
                )
        except Exception as exc:  # noqa: BLE001
            residual.append(f"chroma_restore_failed: {exc}")

    if tracker_updated:
        try:
            write_tracker_fn(tracker_file, snapshots.tracker_root)
        except Exception as exc:  # noqa: BLE001
            residual.append(f"tracker_restore_failed: {exc}")

    if registry_transition is not None:
        try:
            old_target = snapshots.old_locator_state["activity_state"]
            new_target = snapshots.new_locator_state["activity_state"]
            with open_registry(registry_path) as conn:
                with registry_transaction(conn):
                    restore_locator_move_states_after_failure(
                        conn,
                        document_id=ctx["document_id"],
                        old_source_file_id=ctx["old_source_file_id"],
                        new_source_file_id=ctx["new_source_file_id"],
                        old_target_activity_state=old_target,
                        new_target_activity_state=new_target,
                        failed_move_operation_id=operation_id,
                        operation_id=f"{operation_id}:compensate",
                        source=_COMPENSATION_SOURCE,
                        reason="bounded explicit move compensation",
                    )
            with open_registry(registry_path) as conn:
                old_after = get_locator_lifecycle_state(
                    conn, source_file_id=ctx["old_source_file_id"]
                )
                new_after = get_locator_lifecycle_state(
                    conn, source_file_id=ctx["new_source_file_id"]
                )
                if (
                    old_after is None
                    or old_after["activity_state"] != old_target
                ):
                    residual.append("registry_old_locator_not_restored")
                if (
                    new_after is None
                    or new_after["activity_state"] != new_target
                ):
                    residual.append("registry_new_locator_not_restored")
        except Exception as exc:  # noqa: BLE001
            residual.append(f"registry_compensation_failed: {exc}")

    return residual


# ---------------------------------------------------------------------------
# Phase E3 bounded side-store helpers (public; used by governed_move executor)
# ---------------------------------------------------------------------------

build_tracker_after_move = _build_tracker_after_move
build_tracker_after_quarantine = _build_tracker_after_quarantine
write_tracker_atomic = _write_tracker_atomic
read_tracker_json = _read_tracker_json
update_chroma_source_metadata = _update_chroma_source_metadata
validate_bounded_tracker_evidence = _validate_tracker_evidence
validate_bounded_chroma_evidence = _validate_chroma_evidence
validate_bounded_chroma_physical_rows = _validate_chroma_physical_row_uniqueness
tracker_paths_from_record = _tracker_paths

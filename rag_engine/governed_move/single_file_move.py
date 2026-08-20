"""Bounded single-file filesystem MOVE executor (Roadmap v7 Phase E3/E4).

Performs one approved source ? destination file move with registry transition,
tracker update, approved Chroma metadata updates, and durable journaling.
Never bulk-scans, never re-embeds, never uses copy/delete fallback for
cross-device rename.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from rag_engine.governed_reconciliation.explicit_move import (
    build_tracker_after_move,
    read_tracker_json,
    update_chroma_source_metadata,
    validate_bounded_chroma_evidence,
    validate_bounded_chroma_physical_rows,
    validate_bounded_tracker_evidence,
    write_tracker_atomic,
)
from rag_engine.index_compatibility.chroma_inspect import chroma_sqlite_path
from rag_engine.library_state.evidence import lookup_chroma_by_embedding_ids
from rag_engine.library_state.move_approval import (
    MoveApprovalContext,
    MoveApprovalValidationError,
    MoveApprovalValidationResult,
    validate_move_approval,
)
from rag_engine.library_state.move_preflight import (
    PreMoveEvidence,
    PreMoveEvidenceError,
    collect_pre_move_evidence,
)
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    LOCATOR_EVENT_MOVED,
    LOCATOR_INACTIVE,
    get_locator_lifecycle_state,
    list_locator_lifecycle_states_for_document,
    open_registry,
    prepare_move_registry_transition,
    registry_transaction,
    verify_locator_state_event_consistency,
)
from rag_engine.governed_move.move_journal import (
    PHASE_CHROMA_UPDATED,
    PHASE_COMPENSATED,
    PHASE_FILESYSTEM_MOVED,
    PHASE_PREPARED,
    PHASE_RECOVERY_REQUIRED,
    PHASE_REGISTRY_COMMITTED,
    PHASE_REGISTRY_PREPARED,
    PHASE_TRACKER_UPDATED,
    PHASE_VERIFIED,
    PRE_COMMIT_PHASES,
    TERMINAL_PHASES,
    MoveJournalError,
    advance_journal_phase,
    create_prepared_journal,
    journal_path_for_operation,
    read_journal_exact,
    validate_journal_bindings,
    validate_operation_id,
    validate_persist_dir_path,
)
from rag_engine.metadata_registry.move_transaction import MoveRegistryTransitionResult
from rag_engine.stable_identity import PathNormalizationError, normalize_relative_path

OUTCOME_DRY_RUN = "dry_run"
OUTCOME_SUCCESS = "success"
OUTCOME_COMPENSATED_BEFORE_COMMIT = "compensated_before_commit"
OUTCOME_RECOVERY_REQUIRED = "recovery_required"
OUTCOME_BLOCKED = "blocked"

MOVE_LOCK_NAME = "governed_single_file_move.lock"

TrackerWriter = Callable[[Path, dict[str, Any]], None]
ChromaUpdater = Callable[[Path, Mapping[str, str]], None]
FilesystemRenamer = Callable[[Path, Path], None]
NowUtcProvider = Callable[[], str]


class SingleFileMoveError(ValueError):
    """Raised when single-file MOVE preconditions fail."""


@dataclass(frozen=True)
class SingleFileMovePreview:
    """Read-only intended deltas for one bounded single-file MOVE."""

    source_relative_path: str
    destination_relative_path: str
    document_id: str
    source_hash: str
    old_source_file_id: str
    approved_vector_ids: tuple[str, ...]
    operation_id: str
    registry_action: str = "prepare_move_registry_transition"


@dataclass(frozen=True)
class SingleFileMoveRecoveryResult:
    """Outcome of explicit single-file MOVE journal recovery."""

    operation_id: str
    phase: str
    success: bool
    verified: bool
    compensated: bool
    recovery_required: bool
    residual_recovery_notes: tuple[str, ...] = ()
    error_message: str | None = None


@dataclass(frozen=True)
class SingleFileMoveResult:
    """Outcome of a single-file MOVE dry-run or execute attempt."""

    outcome: str
    execute: bool
    operation_id: str
    success: bool
    dry_run: bool
    compensated: bool
    recovery_required: bool
    registry_committed: bool = False
    filesystem_moved: bool = False
    tracker_updated: bool = False
    chroma_updated: bool = False
    preview: SingleFileMovePreview | None = None
    approval: MoveApprovalValidationResult | None = None
    registry_transition: MoveRegistryTransitionResult | None = None
    residual_unrecovered: tuple[str, ...] = ()
    error_message: str | None = None


@dataclass
class _FilesystemSnapshot:
    source_relative_path: str
    destination_relative_path: str
    source_hash: str
    source_existed: bool
    destination_existed: bool


@dataclass
class _ChromaIdentitySnapshot:
    source_path: str | None
    document_id: str | None
    source_hash: str | None


@dataclass
class _SideStoreSnapshot:
    tracker_root: dict[str, Any]
    chroma_sources: dict[str, str | None]
    chroma_identity: dict[str, _ChromaIdentitySnapshot]
    old_locator_state: dict[str, Any] | None
    third_locator_states: dict[str, dict[str, Any] | None]


@dataclass
class _ExecutionProgress:
    registry_prepared: bool = False
    filesystem_moved: bool = False
    tracker_updated: bool = False
    chroma_updated: bool = False
    registry_committed: bool = False


@dataclass(frozen=True)
class SingleFileMoveRequest:
    """Bounded input for one approved single-file MOVE."""

    approval_artifact: Mapping[str, Any]
    pre_move_evidence: PreMoveEvidence
    approval_context: MoveApprovalContext
    source_relative_path: str
    destination_relative_path: str
    library_root: str
    persist_dir: str
    registry_db: str
    tracker_path: str
    old_source_file_id: str
    approved_vector_ids: tuple[str, ...]
    registry_collection: str
    operation_id: str
    execute: bool
    actor: str | None = None
    journal_path: str | None = None


def _default_now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def execute_single_file_move(
    request: SingleFileMoveRequest,
    *,
    tracker_writer: TrackerWriter | None = None,
    chroma_updater: ChromaUpdater | None = None,
    filesystem_renamer: FilesystemRenamer | None = None,
    now_utc_provider: NowUtcProvider | None = None,
) -> SingleFileMoveResult:
    """Dry-run or execute one bounded approved single-file MOVE."""
    write_tracker_fn = tracker_writer or write_tracker_atomic
    update_chroma_fn = chroma_updater or update_chroma_source_metadata
    rename_fn = filesystem_renamer or _atomic_same_filesystem_rename
    now_fn = now_utc_provider or _default_now_utc

    try:
        ctx = _validate_request(request)
    except (
        SingleFileMoveError,
        PathNormalizationError,
        ValueError,
    ) as exc:
        return _blocked_result(request, str(exc))

    if not request.execute:
        try:
            fresh, approval = _collect_validate_and_bind(
                request, ctx, now_utc=now_fn()
            )
        except (
            SingleFileMoveError,
            PreMoveEvidenceError,
            MoveApprovalValidationError,
        ) as exc:
            return _blocked_result(request, str(exc))
        preview = _build_preview(ctx, approval)
        return SingleFileMoveResult(
            outcome=OUTCOME_DRY_RUN,
            execute=False,
            operation_id=ctx["operation_id"],
            success=True,
            dry_run=True,
            compensated=False,
            recovery_required=False,
            preview=preview,
            approval=approval,
        )

    progress = _ExecutionProgress()
    registry_path = Path(ctx["registry_db"])
    tracker_file = Path(ctx["tracker_path"])
    chroma_file = chroma_sqlite_path(ctx["persist_dir"])
    library_root = Path(ctx["library_root"])
    source_abs: Path | None = None
    dest_abs: Path | None = None
    registry_transition: MoveRegistryTransitionResult | None = None
    residual: list[str] = []
    fs_snapshot: _FilesystemSnapshot | None = None
    side_snapshot: _SideStoreSnapshot | None = None
    journal_file: Path | None = None
    preview: SingleFileMovePreview | None = None
    approval_locked: MoveApprovalValidationResult | None = None

    try:
        with _governed_move_lock(ctx["persist_dir"]):
            now_utc = now_fn()
            try:
                fresh_locked, approval_locked = _collect_validate_and_bind(
                    request, ctx, now_utc=now_utc
                )
            except (SingleFileMoveError, PreMoveEvidenceError, MoveApprovalValidationError) as exc:
                raise SingleFileMoveError(str(exc)) from exc

            preview = _build_preview(ctx, approval_locked)
            source_abs = _resolve_under_library_root(library_root, ctx["source_path"])
            dest_abs = _resolve_under_library_root(library_root, ctx["destination_path"])
            fs_snapshot = _capture_filesystem_snapshot(ctx)
            side_snapshot = _capture_side_store_snapshot(ctx)
            registry_summary = _capture_initial_registry_state_summary(ctx, side_snapshot)

            journal_file = create_prepared_journal(
                persist_dir=ctx["persist_dir"],
                operation_id=ctx["operation_id"],
                source_relative_path=ctx["source_path"],
                destination_relative_path=ctx["destination_path"],
                expected_sha256=ctx["source_hash"],
                document_id=ctx["document_id"],
                source_hash=ctx["source_hash"],
                approved_vector_ids=ctx["approved_vector_ids"],
                old_source_file_id=ctx["old_source_file_id"],
                tracker_snapshot=side_snapshot.tracker_root,
                chroma_identity_snapshot=_chroma_identity_for_journal(side_snapshot),
                initial_registry_state_summary=registry_summary,
                library_root=ctx["library_root"],
                registry_db=ctx["registry_db"],
                tracker_path=ctx["tracker_path"],
                registry_collection=ctx["registry_collection"],
                created_at=now_utc,
            )

            with open_registry(registry_path) as conn:
                with registry_transaction(conn):
                    registry_transition = prepare_move_registry_transition(
                        conn,
                        approval=approval_locked,
                        pre_move_evidence=fresh_locked,
                        old_source_file_id=ctx["old_source_file_id"],
                        registry_collection=ctx["registry_collection"],
                        operation_id=ctx["operation_id"],
                        actor=request.actor,
                    )
                    progress.registry_prepared = True
                    advance_journal_phase(
                        journal_file,
                        PHASE_REGISTRY_PREPARED,
                        updated_at=now_utc,
                    )

                    _validate_destination_parent(library_root, ctx["destination_path"])
                    rename_fn(source_abs, dest_abs)
                    progress.filesystem_moved = True
                    _verify_destination_hash(dest_abs, fs_snapshot.source_hash)
                    advance_journal_phase(
                        journal_file,
                        PHASE_FILESYSTEM_MOVED,
                        updated_at=now_utc,
                    )

                    tracker_after = build_tracker_after_move(side_snapshot.tracker_root, ctx)
                    write_tracker_fn(tracker_file, tracker_after)
                    progress.tracker_updated = True
                    advance_journal_phase(
                        journal_file,
                        PHASE_TRACKER_UPDATED,
                        updated_at=now_utc,
                    )

                    if ctx["approved_vector_ids"]:
                        chroma_delta = {
                            vector_id: ctx["destination_path"]
                            for vector_id in ctx["approved_vector_ids"]
                        }
                        update_chroma_fn(chroma_file, chroma_delta)
                        progress.chroma_updated = True
                        advance_journal_phase(
                            journal_file,
                            PHASE_CHROMA_UPDATED,
                            updated_at=now_utc,
                        )

                    _verify_pre_commit(
                        ctx,
                        registry_transition=registry_transition,
                        library_root=library_root,
                        tracker_file=tracker_file,
                        chroma_file=chroma_file,
                        side_snapshot=side_snapshot,
                        conn=conn,
                    )
                progress.registry_committed = True
                advance_journal_phase(
                    journal_file,
                    PHASE_REGISTRY_COMMITTED,
                    updated_at=now_utc,
                )

            _verify_post_commit(
                ctx,
                registry_path=registry_path,
                library_root=library_root,
                tracker_file=tracker_file,
                chroma_file=chroma_file,
                registry_transition=registry_transition,
                side_snapshot=side_snapshot,
            )
            advance_journal_phase(journal_file, PHASE_VERIFIED, updated_at=now_utc)
            return SingleFileMoveResult(
                outcome=OUTCOME_SUCCESS,
                execute=True,
                operation_id=ctx["operation_id"],
                success=True,
                dry_run=False,
                compensated=False,
                recovery_required=False,
                registry_committed=True,
                filesystem_moved=True,
                tracker_updated=True,
                chroma_updated=progress.chroma_updated,
                preview=preview,
                approval=approval_locked,
                registry_transition=registry_transition,
            )
    except Exception as exc:  # noqa: BLE001 - bounded compensation boundary
        if preview is None:
            return SingleFileMoveResult(
                outcome=OUTCOME_BLOCKED,
                execute=True,
                operation_id=ctx["operation_id"],
                success=False,
                dry_run=False,
                compensated=False,
                recovery_required=False,
                error_message=str(exc),
            )
        if progress.registry_committed and journal_file is not None:
            residual.append(f"post_commit_failure: {exc}")
            advance_journal_phase(
                journal_file,
                PHASE_RECOVERY_REQUIRED,
                residual_recovery_notes=residual,
            )
            return SingleFileMoveResult(
                outcome=OUTCOME_RECOVERY_REQUIRED,
                execute=True,
                operation_id=ctx["operation_id"],
                success=False,
                dry_run=False,
                compensated=False,
                recovery_required=True,
                registry_committed=True,
                filesystem_moved=progress.filesystem_moved,
                tracker_updated=progress.tracker_updated,
                chroma_updated=progress.chroma_updated,
                preview=preview,
                approval=approval_locked,
                registry_transition=registry_transition,
                residual_unrecovered=tuple(residual),
                error_message=str(exc),
            )

        mutated = any(
            (
                progress.registry_prepared,
                progress.filesystem_moved,
                progress.tracker_updated,
                progress.chroma_updated,
            )
        )
        if not mutated:
            return SingleFileMoveResult(
                outcome=OUTCOME_BLOCKED,
                execute=True,
                operation_id=ctx["operation_id"],
                success=False,
                dry_run=False,
                compensated=False,
                recovery_required=False,
                preview=preview,
                approval=approval_locked,
                error_message=str(exc),
            )

        if fs_snapshot is None or side_snapshot is None or source_abs is None or dest_abs is None:
            return SingleFileMoveResult(
                outcome=OUTCOME_BLOCKED,
                execute=True,
                operation_id=ctx["operation_id"],
                success=False,
                dry_run=False,
                compensated=False,
                recovery_required=False,
                preview=preview,
                approval=approval_locked,
                error_message=str(exc),
            )

        residual.extend(
            _compensate_before_commit(
                ctx,
                fs_snapshot=fs_snapshot,
                side_snapshot=side_snapshot,
                library_root=library_root,
                source_abs=source_abs,
                dest_abs=dest_abs,
                tracker_file=tracker_file,
                chroma_file=chroma_file,
                progress=progress,
                write_tracker_fn=write_tracker_fn,
                update_chroma_fn=update_chroma_fn,
            )
        )
        compensated = not residual
        outcome = (
            OUTCOME_COMPENSATED_BEFORE_COMMIT
            if compensated
            else OUTCOME_RECOVERY_REQUIRED
        )
        if journal_file is not None:
            advance_journal_phase(
                journal_file,
                PHASE_COMPENSATED if compensated else PHASE_RECOVERY_REQUIRED,
                residual_recovery_notes=residual,
            )
        return SingleFileMoveResult(
            outcome=outcome,
            execute=True,
            operation_id=ctx["operation_id"],
            success=False,
            dry_run=False,
            compensated=compensated,
            recovery_required=not compensated,
            registry_committed=False,
            filesystem_moved=progress.filesystem_moved and not residual,
            tracker_updated=progress.tracker_updated and not residual,
            chroma_updated=progress.chroma_updated and not residual,
            preview=preview,
            approval=approval_locked,
            registry_transition=registry_transition if progress.registry_prepared else None,
            residual_unrecovered=tuple(residual),
            error_message=str(exc),
        )


def recover_single_file_move(
    *,
    persist_dir: str,
    operation_id: str,
    library_root: str,
    registry_db: str,
    tracker_path: str,
) -> SingleFileMoveRecoveryResult:
    """Explicitly recover one bounded single-file MOVE from its exact journal."""
    try:
        op = validate_operation_id(operation_id)
        validate_persist_dir_path(persist_dir)
    except MoveJournalError as exc:
        return SingleFileMoveRecoveryResult(
            operation_id=operation_id,
            phase="",
            success=False,
            verified=False,
            compensated=False,
            recovery_required=True,
            error_message=str(exc),
        )

    try:
        with _governed_move_lock(persist_dir):
            try:
                journal = read_journal_exact(persist_dir, op)
                validate_journal_bindings(
                    journal,
                    persist_dir=persist_dir,
                    library_root=library_root,
                    registry_db=registry_db,
                    tracker_path=tracker_path,
                )
            except (MoveJournalError, SingleFileMoveError) as exc:
                return SingleFileMoveRecoveryResult(
                    operation_id=operation_id,
                    phase="",
                    success=False,
                    verified=False,
                    compensated=False,
                    recovery_required=True,
                    error_message=str(exc),
                )

            phase = str(journal.get("phase") or "")
            notes = tuple(str(x) for x in (journal.get("residual_recovery_notes") or []))

            if phase in TERMINAL_PHASES:
                return SingleFileMoveRecoveryResult(
                    operation_id=operation_id,
                    phase=phase,
                    success=phase == PHASE_VERIFIED,
                    verified=phase == PHASE_VERIFIED,
                    compensated=phase == PHASE_COMPENSATED,
                    recovery_required=phase == PHASE_RECOVERY_REQUIRED,
                    residual_recovery_notes=notes,
                )

            ctx = _journal_to_recovery_ctx(journal)
            library = Path(library_root).resolve()
            tracker_file = Path(tracker_path).resolve()
            chroma_file = chroma_sqlite_path(persist_dir)
            journal_file = journal_path_for_operation(persist_dir, op, create_root=False)
            side_snapshot = _side_snapshot_from_journal(journal)

            committed, commit_ambiguous = _inspect_registry_commit_state(
                registry_db, journal, ctx
            )
            if committed or commit_ambiguous or phase == PHASE_REGISTRY_COMMITTED:
                findings = _inspect_post_commit_state(
                    ctx,
                    registry_db=registry_db,
                    library_root=library,
                    tracker_file=tracker_file,
                    chroma_file=chroma_file,
                )
                if findings:
                    advance_journal_phase(
                        journal_file,
                        PHASE_RECOVERY_REQUIRED,
                        residual_recovery_notes=findings,
                    )
                    return SingleFileMoveRecoveryResult(
                        operation_id=operation_id,
                        phase=PHASE_RECOVERY_REQUIRED,
                        success=False,
                        verified=False,
                        compensated=False,
                        recovery_required=True,
                        residual_recovery_notes=tuple(findings),
                    )
                advance_journal_phase(journal_file, PHASE_VERIFIED)
                return SingleFileMoveRecoveryResult(
                    operation_id=operation_id,
                    phase=PHASE_VERIFIED,
                    success=True,
                    verified=True,
                    compensated=False,
                    recovery_required=False,
                )

            if phase not in PRE_COMMIT_PHASES:
                return SingleFileMoveRecoveryResult(
                    operation_id=operation_id,
                    phase=phase,
                    success=False,
                    verified=False,
                    compensated=False,
                    recovery_required=True,
                    error_message=f"unsupported recovery phase: {phase!r}",
                )

            progress = _progress_from_journal_phase(phase, ctx)
            fs_snapshot = _filesystem_snapshot_from_journal(journal)
            source_abs = _resolve_under_library_root(library, ctx["source_path"])
            dest_abs = _resolve_under_library_root(library, ctx["destination_path"])

            residual = list(
                _compensate_before_commit(
                    ctx,
                    fs_snapshot=fs_snapshot,
                    side_snapshot=side_snapshot,
                    library_root=library,
                    source_abs=source_abs,
                    dest_abs=dest_abs,
                    tracker_file=tracker_file,
                    chroma_file=chroma_file,
                    progress=progress,
                    write_tracker_fn=write_tracker_atomic,
                    update_chroma_fn=update_chroma_source_metadata,
                )
            )
            if residual:
                advance_journal_phase(
                    journal_file,
                    PHASE_RECOVERY_REQUIRED,
                    residual_recovery_notes=residual,
                )
                return SingleFileMoveRecoveryResult(
                    operation_id=operation_id,
                    phase=PHASE_RECOVERY_REQUIRED,
                    success=False,
                    verified=False,
                    compensated=False,
                    recovery_required=True,
                    residual_recovery_notes=tuple(residual),
                )

            verify_notes = _verify_pre_commit_restoration(
                ctx,
                library_root=library,
                tracker_file=tracker_file,
                chroma_file=chroma_file,
                side_snapshot=side_snapshot,
            )
            if verify_notes:
                advance_journal_phase(
                    journal_file,
                    PHASE_RECOVERY_REQUIRED,
                    residual_recovery_notes=verify_notes,
                )
                return SingleFileMoveRecoveryResult(
                    operation_id=operation_id,
                    phase=PHASE_RECOVERY_REQUIRED,
                    success=False,
                    verified=False,
                    compensated=False,
                    recovery_required=True,
                    residual_recovery_notes=tuple(verify_notes),
                )

            advance_journal_phase(journal_file, PHASE_COMPENSATED)
            return SingleFileMoveRecoveryResult(
                operation_id=operation_id,
                phase=PHASE_COMPENSATED,
                success=True,
                verified=False,
                compensated=True,
                recovery_required=False,
            )
    except SingleFileMoveError as exc:
        return SingleFileMoveRecoveryResult(
            operation_id=operation_id,
            phase="",
            success=False,
            verified=False,
            compensated=False,
            recovery_required=True,
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return SingleFileMoveRecoveryResult(
            operation_id=operation_id,
            phase="",
            success=False,
            verified=False,
            compensated=False,
            recovery_required=True,
            error_message=str(exc),
        )


def _build_preview(
    ctx: dict[str, Any],
    approval: MoveApprovalValidationResult,
) -> SingleFileMovePreview:
    return SingleFileMovePreview(
        source_relative_path=ctx["source_path"],
        destination_relative_path=ctx["destination_path"],
        document_id=approval.document_id,
        source_hash=approval.source_hash,
        old_source_file_id=ctx["old_source_file_id"],
        approved_vector_ids=ctx["approved_vector_ids"],
        operation_id=ctx["operation_id"],
    )


def _capture_initial_registry_state_summary(
    ctx: dict[str, Any],
    side_snapshot: _SideStoreSnapshot,
) -> dict[str, Any]:
    old_state = side_snapshot.old_locator_state or {}
    return {
        "document_id": ctx["document_id"],
        "old_source_file_id": ctx["old_source_file_id"],
        "old_locator_activity_state": old_state.get("activity_state"),
        "old_locator_relative_path": ctx["source_path"],
        "third_locator_activity_states": {
            sf: (state or {}).get("activity_state")
            for sf, state in side_snapshot.third_locator_states.items()
        },
    }


def _chroma_identity_for_journal(
    side_snapshot: _SideStoreSnapshot,
) -> dict[str, dict[str, str | None]]:
    out: dict[str, dict[str, str | None]] = {}
    for vector_id, identity in side_snapshot.chroma_identity.items():
        out[vector_id] = {
            "source_path": identity.source_path,
            "document_id": identity.document_id,
            "source_hash": identity.source_hash,
        }
    return out


def _journal_to_recovery_ctx(journal: Mapping[str, Any]) -> dict[str, Any]:
    vector_ids = journal.get("approved_vector_ids") or []
    if not isinstance(vector_ids, list):
        raise SingleFileMoveError("journal approved_vector_ids must be a list")
    return {
        "library_root": str(journal["library_root"]),
        "persist_dir": str(journal["persist_dir"]),
        "registry_db": str(journal["registry_db"]),
        "tracker_path": str(journal["tracker_path"]),
        "source_path": str(journal["source_relative_path"]),
        "destination_path": str(journal["destination_relative_path"]),
        "source_hash": str(journal["source_hash"]),
        "document_id": str(journal["document_id"]),
        "old_source_file_id": str(journal["old_source_file_id"]),
        "approved_vector_ids": tuple(str(x) for x in vector_ids),
        "registry_collection": str(journal["registry_collection"]),
        "operation_id": str(journal["operation_id"]),
    }


def _side_snapshot_from_journal(journal: Mapping[str, Any]) -> _SideStoreSnapshot:
    tracker_root = journal.get("tracker_snapshot") or {}
    if not isinstance(tracker_root, dict):
        raise SingleFileMoveError("journal tracker_snapshot must be a mapping")
    chroma_raw = journal.get("chroma_identity_snapshot") or {}
    chroma_identity: dict[str, _ChromaIdentitySnapshot] = {}
    if isinstance(chroma_raw, dict):
        for vector_id, identity in chroma_raw.items():
            if not isinstance(identity, dict):
                continue
            chroma_identity[str(vector_id)] = _ChromaIdentitySnapshot(
                source_path=identity.get("source_path"),
                document_id=identity.get("document_id"),
                source_hash=identity.get("source_hash"),
            )
    chroma_sources = {
        vector_id: snap.source_path for vector_id, snap in chroma_identity.items()
    }
    summary = journal.get("initial_registry_state_summary") or {}
    old_state = None
    if isinstance(summary, dict):
        old_state = {
            "source_file_id": summary.get("old_source_file_id"),
            "activity_state": summary.get("old_locator_activity_state"),
        }
    third: dict[str, dict[str, Any] | None] = {}
    third_states = summary.get("third_locator_activity_states") if isinstance(summary, dict) else {}
    if isinstance(third_states, dict):
        for sf, activity in third_states.items():
            third[str(sf)] = {"activity_state": activity}
    return _SideStoreSnapshot(
        tracker_root=copy.deepcopy(tracker_root),
        chroma_sources=chroma_sources,
        chroma_identity=chroma_identity,
        old_locator_state=old_state,
        third_locator_states=third,
    )


def _filesystem_snapshot_from_journal(journal: Mapping[str, Any]) -> _FilesystemSnapshot:
    return _FilesystemSnapshot(
        source_relative_path=str(journal["source_relative_path"]),
        destination_relative_path=str(journal["destination_relative_path"]),
        source_hash=str(journal["expected_sha256"]),
        source_existed=True,
        destination_existed=False,
    )


def _progress_from_journal_phase(phase: str, ctx: dict[str, Any]) -> _ExecutionProgress:
    order = (
        PHASE_PREPARED,
        PHASE_REGISTRY_PREPARED,
        PHASE_FILESYSTEM_MOVED,
        PHASE_TRACKER_UPDATED,
        PHASE_CHROMA_UPDATED,
    )
    idx = order.index(phase) if phase in order else -1
    chroma_applicable = bool(ctx["approved_vector_ids"])
    return _ExecutionProgress(
        registry_prepared=idx >= order.index(PHASE_REGISTRY_PREPARED),
        filesystem_moved=idx >= order.index(PHASE_FILESYSTEM_MOVED),
        tracker_updated=idx >= order.index(PHASE_TRACKER_UPDATED),
        chroma_updated=chroma_applicable and idx >= order.index(PHASE_CHROMA_UPDATED),
    )


def _inspect_registry_commit_state(
    registry_db: str,
    journal: Mapping[str, Any],
    ctx: dict[str, Any],
) -> tuple[bool, bool]:
    """Return (committed, ambiguous)."""
    dest_path = ctx["destination_path"]
    old_sf = ctx["old_source_file_id"]
    with open_registry(registry_db) as conn:
        dest_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (dest_path,),
        ).fetchone()["c"]
        move_events = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE operation_id = ? AND event_type = ?",
            (ctx["operation_id"], LOCATOR_EVENT_MOVED),
        ).fetchone()["c"]
        old_state = get_locator_lifecycle_state(conn, source_file_id=old_sf)
    old_inactive = (
        old_state is not None and old_state["activity_state"] == LOCATOR_INACTIVE
    )
    committed = dest_count >= 1 and move_events >= 2 and old_inactive
    ambiguous = (dest_count >= 1 or move_events >= 1) and not committed
    if str(journal.get("phase")) == PHASE_REGISTRY_COMMITTED:
        ambiguous = ambiguous or not committed
    return committed, ambiguous


def _inspect_post_commit_state(
    ctx: dict[str, Any],
    *,
    registry_db: str,
    library_root: Path,
    tracker_file: Path,
    chroma_file: Path,
) -> list[str]:
    findings: list[str] = []
    source_abs = _resolve_under_library_root(library_root, ctx["source_path"])
    dest_abs = _resolve_under_library_root(library_root, ctx["destination_path"])
    if source_abs.exists():
        findings.append("post_commit_source_still_present")
    if not dest_abs.is_file():
        findings.append("post_commit_destination_missing")
    elif hashlib.sha256(dest_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        findings.append("post_commit_destination_hash_mismatch")

    with open_registry(registry_db) as conn:
        old_state = get_locator_lifecycle_state(
            conn, source_file_id=ctx["old_source_file_id"]
        )
        if old_state is None or old_state["activity_state"] != LOCATOR_INACTIVE:
            findings.append("post_commit_old_locator_not_inactive")
        dest_rows = conn.execute(
            "SELECT source_file_id FROM source_files WHERE relative_path = ?",
            (ctx["destination_path"],),
        ).fetchall()
        if len(dest_rows) != 1:
            findings.append("post_commit_destination_registry_row_count")

    tracker = read_tracker_json(tracker_file)
    record = tracker.get(ctx["source_hash"])
    if record is None or list(record.get("paths") or []) != [ctx["destination_path"]]:
        findings.append("post_commit_tracker_path_mismatch")

    if ctx["approved_vector_ids"]:
        try:
            validate_bounded_chroma_evidence(
                persist_dir=ctx["persist_dir"],
                old_path=ctx["destination_path"],
                document_id=ctx["document_id"],
                source_hash=ctx["source_hash"],
                approved_vector_ids=ctx["approved_vector_ids"],
            )
        except ValueError as exc:
            findings.append(f"post_commit_chroma_mismatch: {exc}")

    return findings


def _verify_pre_commit_restoration(
    ctx: dict[str, Any],
    *,
    library_root: Path,
    tracker_file: Path,
    chroma_file: Path,
    side_snapshot: _SideStoreSnapshot,
) -> list[str]:
    notes: list[str] = []
    source_abs = _resolve_under_library_root(library_root, ctx["source_path"])
    dest_abs = _resolve_under_library_root(library_root, ctx["destination_path"])
    if not source_abs.is_file():
        notes.append("restored_source_missing")
    elif hashlib.sha256(source_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        notes.append("restored_source_hash_mismatch")
    if dest_abs.exists():
        notes.append("restored_destination_still_present")

    tracker = read_tracker_json(tracker_file)
    if tracker != side_snapshot.tracker_root:
        notes.append("restored_tracker_mismatch")

    if ctx["approved_vector_ids"]:
        try:
            validate_bounded_chroma_evidence(
                persist_dir=ctx["persist_dir"],
                old_path=ctx["source_path"],
                document_id=ctx["document_id"],
                source_hash=ctx["source_hash"],
                approved_vector_ids=ctx["approved_vector_ids"],
            )
        except ValueError as exc:
            notes.append(f"restored_chroma_mismatch: {exc}")

    with open_registry(ctx["registry_db"]) as conn:
        dest_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (ctx["destination_path"],),
        ).fetchone()["c"]
        if dest_count != 0:
            notes.append("restored_registry_destination_row_present")
        old_state = get_locator_lifecycle_state(
            conn, source_file_id=ctx["old_source_file_id"]
        )
        if old_state is None or old_state["activity_state"] != LOCATOR_ACTIVE:
            notes.append("restored_old_locator_not_active")

    return notes


def _blocked_result(request: SingleFileMoveRequest, message: str) -> SingleFileMoveResult:
    try:
        operation_id = _require_operation_id(request.operation_id)
    except SingleFileMoveError:
        operation_id = request.operation_id.strip() if isinstance(request.operation_id, str) else ""
    return SingleFileMoveResult(
        outcome=OUTCOME_BLOCKED,
        execute=request.execute,
        operation_id=operation_id,
        success=False,
        dry_run=not request.execute,
        compensated=False,
        recovery_required=False,
        error_message=message,
    )


def _collect_validate_and_bind(
    request: SingleFileMoveRequest,
    ctx: dict[str, Any],
    *,
    now_utc: str,
) -> tuple[PreMoveEvidence, MoveApprovalValidationResult]:
    try:
        fresh = collect_pre_move_evidence(
            library_root=ctx["library_root"],
            persist_dir=ctx["persist_dir"],
            registry_db=ctx["registry_db"],
            tracker_path=ctx["tracker_path"],
            source_path=ctx["source_path"],
            destination_path=ctx["destination_path"],
            journal_path=request.journal_path,
            operation_id=ctx["operation_id"],
        )
        approval = validate_move_approval(
            request.approval_artifact,
            fresh.plan,
            source_path=ctx["source_path"],
            destination_path=ctx["destination_path"],
            context=request.approval_context,
            pre_move_evidence=fresh,
            now_utc=now_utc,
        )
    except (PreMoveEvidenceError, MoveApprovalValidationError) as exc:
        raise SingleFileMoveError(str(exc)) from exc
    _compare_evidence_with_approved(
        fresh=fresh,
        approved=request.pre_move_evidence,
        approval_context=request.approval_context,
        ctx=ctx,
    )
    _validate_side_store_evidence(ctx)
    return fresh, approval


def _validate_request(request: SingleFileMoveRequest) -> dict[str, Any]:
    if not isinstance(request.approval_artifact, Mapping):
        raise SingleFileMoveError("approval_artifact must be a mapping")
    if not isinstance(request.pre_move_evidence, PreMoveEvidence):
        raise SingleFileMoveError("pre_move_evidence must be a PreMoveEvidence")
    if not isinstance(request.approval_context, MoveApprovalContext):
        raise SingleFileMoveError("approval_context must be a MoveApprovalContext")

    library_root = _require_absolute_path(request.library_root, "library_root")
    persist_dir = _require_absolute_path(request.persist_dir, "persist_dir")
    registry_db = _require_absolute_path(request.registry_db, "registry_db")
    tracker_path = _require_absolute_path(request.tracker_path, "tracker_path")

    source_path = _require_relative_path(request.source_relative_path, "source_relative_path")
    destination_path = _require_relative_path(
        request.destination_relative_path,
        "destination_relative_path",
    )
    if source_path == destination_path:
        raise SingleFileMoveError("source and destination paths must differ")

    old_sf = _require_non_empty_str(request.old_source_file_id, "old_source_file_id")
    operation_id = _require_operation_id(request.operation_id)
    registry_collection = _require_non_empty_str(
        request.registry_collection,
        "registry_collection",
    )
    approved_vector_ids = _normalize_vector_ids(request.approved_vector_ids)

    affected = request.approval_context.affected_source_file_ids
    if not isinstance(affected, tuple) or len(affected) != 2:
        raise SingleFileMoveError(
            "approval_context.affected_source_file_ids must be a tuple of exactly two IDs"
        )
    if affected[0] != old_sf:
        raise SingleFileMoveError(
            "old_source_file_id must match approval_context affected_source_file_ids[0]"
        )
    if affected[0] == affected[1]:
        raise SingleFileMoveError(
            "approval_context.affected_source_file_ids must contain two distinct IDs"
        )

    if not _same_resolved_path(request.pre_move_evidence.library_root, library_root):
        raise SingleFileMoveError("library_root mismatch with pre_move_evidence")
    if not _same_resolved_path(request.pre_move_evidence.persist_dir, persist_dir):
        raise SingleFileMoveError("persist_dir mismatch with pre_move_evidence")
    if not _same_resolved_path(request.pre_move_evidence.registry_db, registry_db):
        raise SingleFileMoveError("registry_db mismatch with pre_move_evidence")
    if request.pre_move_evidence.tracker_path is not None:
        if not _same_resolved_path(request.pre_move_evidence.tracker_path, tracker_path):
            raise SingleFileMoveError("tracker_path mismatch with pre_move_evidence")

    if not _same_resolved_path(request.approval_context.registry_db_path, registry_db):
        raise SingleFileMoveError("registry_db mismatch with approval_context")
    if not _same_resolved_path(request.approval_context.library_root, library_root):
        raise SingleFileMoveError("library_root mismatch with approval_context")
    if not _same_resolved_path(request.approval_context.persist_dir, persist_dir):
        raise SingleFileMoveError("persist_dir mismatch with approval_context")

    if not isinstance(request.execute, bool):
        raise SingleFileMoveError("execute must be a boolean")

    source_entry = Path(library_root) / source_path
    if source_entry.is_symlink():
        raise SingleFileMoveError(
            "source path must be a regular file entry, not a symlink"
        )

    return {
        "library_root": library_root,
        "persist_dir": persist_dir,
        "registry_db": registry_db,
        "tracker_path": tracker_path,
        "source_path": source_path,
        "destination_path": destination_path,
        "old_source_file_id": old_sf,
        "new_source_file_id": affected[1],
        "approved_vector_ids": approved_vector_ids,
        "registry_collection": registry_collection,
        "operation_id": operation_id,
        "document_id": request.pre_move_evidence.document_id,
        "source_hash": request.pre_move_evidence.source_hash,
        "old_path": source_path,
        "new_path": destination_path,
    }


def _compare_evidence_with_approved(
    *,
    fresh: PreMoveEvidence,
    approved: PreMoveEvidence,
    approval_context: MoveApprovalContext,
    ctx: dict[str, Any],
) -> None:
    pairs = (
        ("source_path", fresh.source_path, approved.source_path),
        ("destination_path", fresh.destination_path, approved.destination_path),
        ("source_hash", fresh.source_hash, approved.source_hash),
        ("document_id", fresh.document_id, approved.document_id),
        ("request_id", fresh.request_id, approved.request_id),
        ("plan_digest", fresh.plan_digest, approved.plan_digest),
    )
    for field, current, expected in pairs:
        if current != expected:
            raise SingleFileMoveError(f"fresh evidence {field} drift from approved evidence")

    path_pairs = (
        ("library_root", fresh.library_root, approved.library_root),
        ("persist_dir", fresh.persist_dir, approved.persist_dir),
        ("registry_db", fresh.registry_db, approved.registry_db),
    )
    for field, current, expected in path_pairs:
        if not _same_resolved_path(current, expected):
            raise SingleFileMoveError(f"fresh evidence {field} drift from approved evidence")

    if tuple(sorted(fresh.approved_vector_ids)) != tuple(sorted(approved.approved_vector_ids)):
        raise SingleFileMoveError("fresh evidence approved_vector_ids drift")
    if tuple(sorted(fresh.approved_vector_ids)) != tuple(sorted(ctx["approved_vector_ids"])):
        raise SingleFileMoveError("approved_vector_ids mismatch with request")
    if fresh.compatibility_state != approved.compatibility_state:
        raise SingleFileMoveError("fresh evidence compatibility_state drift")
    if fresh.resolver_classification != approved.resolver_classification:
        raise SingleFileMoveError("fresh evidence resolver_classification drift")
    if fresh.proposed_operation != approved.proposed_operation:
        raise SingleFileMoveError("fresh evidence proposed_operation drift")

    if fresh.tracker_path is not None or approved.tracker_path is not None:
        if fresh.tracker_path is None or approved.tracker_path is None:
            raise SingleFileMoveError("fresh evidence tracker_path drift")
        if not _same_resolved_path(fresh.tracker_path, approved.tracker_path):
            raise SingleFileMoveError("fresh evidence tracker_path drift")

    if approval_context.affected_source_file_ids[0] != ctx["old_source_file_id"]:
        raise SingleFileMoveError("affected_source_file_ids drift")


def _validate_side_store_evidence(ctx: dict[str, Any]) -> None:
    try:
        validate_bounded_tracker_evidence(
            tracker_path=ctx["tracker_path"],
            source_hash=ctx["source_hash"],
            old_path=ctx["source_path"],
            approved_vector_ids=ctx["approved_vector_ids"],
            document_id=ctx["document_id"],
        )
        validate_bounded_chroma_physical_rows(
            persist_dir=ctx["persist_dir"],
            approved_vector_ids=ctx["approved_vector_ids"],
        )
        validate_bounded_chroma_evidence(
            persist_dir=ctx["persist_dir"],
            old_path=ctx["source_path"],
            document_id=ctx["document_id"],
            source_hash=ctx["source_hash"],
            approved_vector_ids=ctx["approved_vector_ids"],
        )
    except ValueError as exc:
        raise SingleFileMoveError(str(exc)) from exc


def _capture_filesystem_snapshot(ctx: dict[str, Any]) -> _FilesystemSnapshot:
    library_root = Path(ctx["library_root"])
    source_entry = library_root / ctx["source_path"]
    if source_entry.is_symlink():
        raise SingleFileMoveError(
            "source path must be a regular file entry, not a symlink"
        )
    source_abs = _resolve_under_library_root(library_root, ctx["source_path"])
    dest_abs = _resolve_under_library_root(library_root, ctx["destination_path"])
    _validate_destination_parent(library_root, ctx["destination_path"])
    if not source_abs.is_file():
        if source_abs.is_dir():
            raise SingleFileMoveError(
                "source_relative_path must refer to a single file, not a directory"
            )
        raise SingleFileMoveError("source file must exist before MOVE")
    if dest_abs.exists():
        raise SingleFileMoveError("destination path must be absent before MOVE")
    digest = hashlib.sha256(source_abs.read_bytes()).hexdigest()
    if digest != ctx["source_hash"]:
        raise SingleFileMoveError("source file SHA-256 mismatch")
    return _FilesystemSnapshot(
        source_relative_path=ctx["source_path"],
        destination_relative_path=ctx["destination_path"],
        source_hash=digest,
        source_existed=True,
        destination_existed=False,
    )


def _capture_side_store_snapshot(ctx: dict[str, Any]) -> _SideStoreSnapshot:
    tracker_file = Path(ctx["tracker_path"])
    chroma_file = chroma_sqlite_path(ctx["persist_dir"])
    tracker_root = read_tracker_json(tracker_file)

    chroma_sources: dict[str, str | None] = {}
    chroma_identity: dict[str, _ChromaIdentitySnapshot] = {}
    if ctx["approved_vector_ids"] and chroma_file.is_file():
        lookup = lookup_chroma_by_embedding_ids(chroma_file, ctx["approved_vector_ids"])
        for rec in lookup.records:
            chroma_sources[rec.chroma_embedding_id] = rec.source_path
            meta_doc = rec.metadata.get("document_id")
            meta_hash = rec.metadata.get("source_hash")
            chroma_identity[rec.chroma_embedding_id] = _ChromaIdentitySnapshot(
                source_path=rec.source_path,
                document_id=str(meta_doc) if meta_doc is not None else None,
                source_hash=str(meta_hash) if meta_hash is not None else None,
            )

    with open_registry(ctx["registry_db"]) as conn:
        states = list_locator_lifecycle_states_for_document(
            conn, document_id=ctx["document_id"]
        )
        by_id = {row["source_file_id"]: row for row in states}
        old_state = copy.deepcopy(by_id.get(ctx["old_source_file_id"]))
        third = {
            sf: copy.deepcopy(st)
            for sf, st in by_id.items()
            if sf != ctx["old_source_file_id"]
        }

    if old_state is None or old_state["activity_state"] != LOCATOR_ACTIVE:
        raise SingleFileMoveError("old locator must be ACTIVE before MOVE")

    return _SideStoreSnapshot(
        tracker_root=copy.deepcopy(tracker_root),
        chroma_sources=chroma_sources,
        chroma_identity=chroma_identity,
        old_locator_state=old_state,
        third_locator_states=third,
    )


def _validate_destination_parent(library_root: Path, destination_relative_path: str) -> None:
    root = library_root.resolve()
    parent_entry = (library_root / destination_relative_path).parent
    if not parent_entry.exists():
        raise SingleFileMoveError("destination parent directory must exist before MOVE")
    if parent_entry.is_symlink():
        resolved_parent = parent_entry.resolve()
        _assert_path_under_root(resolved_parent, root)
        if not resolved_parent.is_dir():
            raise SingleFileMoveError("destination parent must be a directory")
        return
    if not parent_entry.is_dir():
        raise SingleFileMoveError("destination parent must be a directory")
    _assert_path_under_root(parent_entry.resolve(), root)


def _atomic_same_filesystem_rename(source_abs: Path, dest_abs: Path) -> None:
    if dest_abs.exists():
        raise SingleFileMoveError("destination path already exists")
    parent = dest_abs.parent
    if not parent.exists():
        raise SingleFileMoveError("destination parent directory must exist before MOVE")
    if not parent.is_dir():
        raise SingleFileMoveError("destination parent must be a directory")
    try:
        os.replace(source_abs, dest_abs)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise SingleFileMoveError(
                "cross-device rename is not supported; refusing copy/delete fallback"
            ) from exc
        raise SingleFileMoveError(f"filesystem rename failed: {exc}") from exc


def _verify_destination_hash(dest_abs: Path, expected_hash: str) -> None:
    if not dest_abs.is_file():
        raise SingleFileMoveError("destination file missing after rename")
    actual = hashlib.sha256(dest_abs.read_bytes()).hexdigest()
    if actual != expected_hash:
        raise SingleFileMoveError("destination SHA-256 mismatch after rename")


def _verify_pre_commit(
    ctx: dict[str, Any],
    *,
    registry_transition: MoveRegistryTransitionResult,
    library_root: Path,
    tracker_file: Path,
    chroma_file: Path,
    side_snapshot: _SideStoreSnapshot,
    conn: Any,
) -> None:
    source_abs = _resolve_under_library_root(library_root, ctx["source_path"])
    dest_abs = _resolve_under_library_root(library_root, ctx["destination_path"])
    if source_abs.exists():
        raise SingleFileMoveError("pre-commit source path still present")
    if not dest_abs.is_file():
        raise SingleFileMoveError("pre-commit destination file missing")
    if hashlib.sha256(dest_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        raise SingleFileMoveError("pre-commit destination hash mismatch")

    old_state = get_locator_lifecycle_state(
        conn, source_file_id=registry_transition.old_source_file_id
    )
    new_state = get_locator_lifecycle_state(
        conn, source_file_id=registry_transition.new_source_file_id
    )
    if old_state is None or old_state["activity_state"] != LOCATOR_INACTIVE:
        raise SingleFileMoveError("pre-commit old locator is not INACTIVE")
    if new_state is None or new_state["activity_state"] != LOCATOR_ACTIVE:
        raise SingleFileMoveError("pre-commit new locator is not ACTIVE")

    move_events = conn.execute(
        "SELECT source_file_id, related_event_id, related_v4_event_id FROM "
        "source_file_locator_lifecycle_events "
        "WHERE operation_id = ? AND event_type = ?",
        (ctx["operation_id"], LOCATOR_EVENT_MOVED),
    ).fetchall()
    if len(move_events) != 2:
        raise SingleFileMoveError("pre-commit MOVE linkage must contain exactly two events")
    new_move = next(
        e for e in move_events if e["source_file_id"] == registry_transition.new_source_file_id
    )
    if new_move["related_v4_event_id"] != registry_transition.destination_v4_event_id:
        raise SingleFileMoveError("pre-commit new MOVE must reference destination V4 alias")

    tracker = read_tracker_json(tracker_file)
    record = tracker[ctx["source_hash"]]
    paths = record.get("paths") or []
    if list(paths) != [ctx["destination_path"]]:
        raise SingleFileMoveError("pre-commit tracker path mismatch")
    if tuple(str(x) for x in (record.get("chunk_ids") or [])) != ctx["approved_vector_ids"]:
        raise SingleFileMoveError("pre-commit tracker chunk_ids changed")

    if ctx["approved_vector_ids"]:
        _verify_bounded_chroma_identity(
            ctx,
            expected_source_path=ctx["destination_path"],
        )

    current_states = {
        row["source_file_id"]: row
        for row in list_locator_lifecycle_states_for_document(
            conn, document_id=ctx["document_id"]
        )
    }
    for sf, before in side_snapshot.third_locator_states.items():
        after = current_states.get(sf)
        if after is None or after["activity_state"] != before["activity_state"]:
            raise SingleFileMoveError(f"pre-commit unexpected third locator change for {sf!r}")


def _verify_post_commit(
    ctx: dict[str, Any],
    *,
    registry_path: Path,
    library_root: Path,
    tracker_file: Path,
    chroma_file: Path,
    registry_transition: MoveRegistryTransitionResult,
    side_snapshot: _SideStoreSnapshot,
) -> None:
    source_abs = _resolve_under_library_root(library_root, ctx["source_path"])
    dest_abs = _resolve_under_library_root(library_root, ctx["destination_path"])
    if source_abs.exists():
        raise SingleFileMoveError("post-commit source path still present")
    if not dest_abs.is_file():
        raise SingleFileMoveError("post-commit destination file missing")
    if hashlib.sha256(dest_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        raise SingleFileMoveError("post-commit destination hash mismatch")

    with open_registry(registry_path) as conn:
        old_state = get_locator_lifecycle_state(
            conn, source_file_id=registry_transition.old_source_file_id
        )
        new_state = get_locator_lifecycle_state(
            conn, source_file_id=registry_transition.new_source_file_id
        )
        if old_state is None or old_state["activity_state"] != LOCATOR_INACTIVE:
            raise SingleFileMoveError("post-commit old locator is not INACTIVE")
        if new_state is None or new_state["activity_state"] != LOCATOR_ACTIVE:
            raise SingleFileMoveError("post-commit new locator is not ACTIVE")
        for sf in (
            registry_transition.old_source_file_id,
            registry_transition.new_source_file_id,
        ):
            check = verify_locator_state_event_consistency(conn, source_file_id=sf)
            if not check["consistent"]:
                raise SingleFileMoveError(
                    f"post-commit locator consistency failed for {sf!r}"
                )

    tracker = read_tracker_json(tracker_file)
    record = tracker[ctx["source_hash"]]
    if list(record.get("paths") or []) != [ctx["destination_path"]]:
        raise SingleFileMoveError("post-commit tracker path mismatch")

    if ctx["approved_vector_ids"]:
        _verify_bounded_chroma_identity(
            ctx,
            expected_source_path=ctx["destination_path"],
        )


def _verify_bounded_chroma_identity(
    ctx: dict[str, Any],
    *,
    expected_source_path: str,
) -> None:
    try:
        validate_bounded_chroma_physical_rows(
            persist_dir=ctx["persist_dir"],
            approved_vector_ids=ctx["approved_vector_ids"],
        )
        validate_bounded_chroma_evidence(
            persist_dir=ctx["persist_dir"],
            old_path=expected_source_path,
            document_id=ctx["document_id"],
            source_hash=ctx["source_hash"],
            approved_vector_ids=ctx["approved_vector_ids"],
        )
    except ValueError as exc:
        raise SingleFileMoveError(str(exc)) from exc


def _compensate_before_commit(
    ctx: dict[str, Any],
    *,
    fs_snapshot: _FilesystemSnapshot,
    side_snapshot: _SideStoreSnapshot,
    library_root: Path,
    source_abs: Path,
    dest_abs: Path,
    tracker_file: Path,
    chroma_file: Path,
    progress: _ExecutionProgress,
    write_tracker_fn: TrackerWriter,
    update_chroma_fn: ChromaUpdater,
) -> list[str]:
    residual: list[str] = []

    if progress.chroma_updated and ctx["approved_vector_ids"]:
        try:
            _restore_chroma_identity(
                chroma_file,
                vector_ids=ctx["approved_vector_ids"],
                side_snapshot=side_snapshot,
            )
        except Exception as exc:  # noqa: BLE001
            residual.append(f"chroma_restore_failed: {exc}")

    if progress.tracker_updated:
        try:
            write_tracker_fn(tracker_file, side_snapshot.tracker_root)
        except Exception as exc:  # noqa: BLE001
            residual.append(f"tracker_restore_failed: {exc}")

    if progress.filesystem_moved:
        try:
            _restore_filesystem_move(
                library_root=library_root,
                source_abs=source_abs,
                dest_abs=dest_abs,
                expected_hash=fs_snapshot.source_hash,
            )
        except Exception as exc:  # noqa: BLE001
            residual.append(f"filesystem_restore_failed: {exc}")

    return residual


def _restore_chroma_identity(
    chroma_file: Path,
    *,
    vector_ids: tuple[str, ...],
    side_snapshot: _SideStoreSnapshot,
) -> None:
    conn = sqlite3.connect(str(chroma_file))
    try:
        for embedding_id in vector_ids:
            identity = side_snapshot.chroma_identity.get(embedding_id)
            if identity is None:
                continue
            row = conn.execute(
                "SELECT id FROM embeddings WHERE embedding_id = ?",
                (embedding_id,),
            ).fetchone()
            if row is None:
                raise SingleFileMoveError(
                    f"Chroma embedding_id missing during identity restore: {embedding_id!r}"
                )
            row_id = int(row[0])
            for key, value in (
                ("source", identity.source_path),
                ("document_id", identity.document_id),
                ("source_hash", identity.source_hash),
            ):
                if value is None:
                    continue
                existing = conn.execute(
                    "SELECT string_value FROM embedding_metadata "
                    "WHERE id = ? AND key = ?",
                    (row_id, key),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO embedding_metadata (id, key, string_value) "
                        "VALUES (?, ?, ?)",
                        (row_id, key, value),
                    )
                else:
                    conn.execute(
                        "UPDATE embedding_metadata SET string_value = ? "
                        "WHERE id = ? AND key = ?",
                        (value, row_id, key),
                    )
        conn.commit()
    finally:
        conn.close()


def _restore_filesystem_move(
    *,
    library_root: Path,
    source_abs: Path,
    dest_abs: Path,
    expected_hash: str,
) -> None:
    root = library_root.resolve()
    _assert_path_under_root(source_abs.resolve(), root)
    _assert_path_under_root(dest_abs.resolve(), root)
    if source_abs.is_file():
        return
    if not dest_abs.is_file():
        raise SingleFileMoveError("filesystem rollback requires destination file")
    actual = hashlib.sha256(dest_abs.read_bytes()).hexdigest()
    if actual != expected_hash:
        raise SingleFileMoveError("filesystem rollback destination hash mismatch")
    os.replace(dest_abs, source_abs)


@contextmanager
def _governed_move_lock(persist_dir: str) -> Iterator[Path]:
    persist = Path(persist_dir).resolve()
    if not persist.is_dir():
        raise SingleFileMoveError("persist_dir must exist before governed MOVE")
    lock_path = (persist / MOVE_LOCK_NAME).resolve()
    _assert_path_under_root(lock_path, persist)
    deadline = None
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode())
            break
        except FileExistsError:
            raise SingleFileMoveError(
                "another governed single-file MOVE holds the persist_dir lock"
            ) from None

    try:
        yield lock_path
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _resolve_under_library_root(library_root: Path, relative_path: str) -> Path:
    root = library_root.resolve()
    target = (root / relative_path).resolve()
    _assert_path_under_root(target, root)
    return target


def _assert_path_under_root(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SingleFileMoveError(
            f"path {path!s} resolves outside library_root {root!s}"
        ) from exc


def _same_resolved_path(left: str, right: str) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def _require_absolute_path(raw: str, field: str) -> str:
    text = _require_non_empty_str(raw, field)
    path = Path(text)
    if not path.is_absolute():
        raise SingleFileMoveError(f"{field} must be an absolute path")
    return str(path.resolve())


def _require_relative_path(raw: str, field: str) -> str:
    if "\0" in raw:
        raise SingleFileMoveError(f"{field} must not contain NUL")
    try:
        return normalize_relative_path(raw)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        raise SingleFileMoveError(f"invalid {field}: {exc}") from exc


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise SingleFileMoveError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise SingleFileMoveError(f"{field} must not contain leading/trailing whitespace")
    return value


def _require_operation_id(operation_id: str) -> str:
    op = _require_non_empty_str(operation_id, "operation_id")
    if "/" in op or "\\" in op:
        raise SingleFileMoveError("operation_id must be a safe opaque identifier")
    return op


def _normalize_vector_ids(raw: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise SingleFileMoveError("approved_vector_ids must be a sequence of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _require_non_empty_str(item, "approved_vector_ids entry")
        if text in seen:
            raise SingleFileMoveError("approved_vector_ids must not contain duplicates")
        seen.add(text)
        out.append(text)
    return tuple(out)

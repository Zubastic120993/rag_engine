"""Bounded single-file quarantine-delete executor (DELETE Phase B).

Moves one resolver-proven exact duplicate from its library path to an explicit
quarantine path with registry transition, bounded tracker/Chroma updates, and
durable journaling. Never permanently deletes files, registry rows, or vectors.
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

from rag_engine.governed_delete.quarantine_approval import (
    QuarantineDeleteApprovalContext,
    QuarantineDeleteApprovalValidationError,
    QuarantineDeleteApprovalValidationResult,
    validate_quarantine_delete_approval,
)
from rag_engine.governed_delete.quarantine_journal import (
    PHASE_CHROMA_UPDATED,
    PHASE_COMPENSATED,
    PHASE_FILESYSTEM_QUARANTINED,
    PHASE_PREPARED,
    PHASE_RECOVERY_REQUIRED,
    PHASE_REGISTRY_COMMITTED,
    PHASE_REGISTRY_PREPARED,
    PHASE_TRACKER_UPDATED,
    PHASE_VERIFIED,
    PRE_COMMIT_PHASES,
    TERMINAL_PHASES,
    QuarantineDeleteJournalError,
    advance_journal_phase,
    create_prepared_journal,
    journal_path_for_operation,
    read_journal_exact,
    validate_journal_bindings,
    validate_operation_id,
    validate_persist_dir_path,
)
from rag_engine.governed_delete.quarantine_preflight import (
    QuarantineDeleteEvidence,
    QuarantineDeletePreflightError,
    collect_quarantine_delete_evidence,
)
from rag_engine.governed_reconciliation.explicit_move import (
    build_tracker_after_quarantine,
    read_tracker_json,
    update_chroma_source_metadata,
    validate_bounded_chroma_evidence,
    validate_bounded_chroma_physical_rows,
    validate_bounded_tracker_evidence,
    write_tracker_atomic,
)
from rag_engine.index_compatibility.chroma_inspect import chroma_sqlite_path
from rag_engine.library_state.evidence import lookup_chroma_by_embedding_ids
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    LOCATOR_EVENT_COMPENSATION_COMPLETED,
    LOCATOR_INACTIVE,
    get_locator_lifecycle_state,
    list_locator_lifecycle_states_for_document,
    make_source_file_id,
    open_registry,
    registry_transaction,
    restore_locator_quarantine_state_after_failure,
    verify_locator_state_event_consistency,
)
from rag_engine.metadata_registry.quarantine_transaction import (
    QuarantineRegistryTransitionError,
    QuarantineRegistryTransitionResult,
    prepare_quarantine_registry_transition,
    validate_quarantine_registry_preconditions,
)
from rag_engine.stable_identity import PathNormalizationError, normalize_relative_path

OUTCOME_DRY_RUN = "dry_run"
OUTCOME_SUCCESS = "success"
OUTCOME_BLOCKED = "blocked"
OUTCOME_COMPENSATED_BEFORE_COMMIT = "compensated_before_commit"
OUTCOME_RECOVERY_REQUIRED = "recovery_required"

QUARANTINE_DELETE_LOCK_NAME = "governed_quarantine_delete.lock"

TrackerWriter = Callable[[Path, dict[str, Any]], None]
ChromaUpdater = Callable[[Path, Mapping[str, str]], None]
FilesystemRenamer = Callable[[Path, Path], None]
NowUtcProvider = Callable[[], str]


class QuarantineDeleteError(ValueError):
    """Raised when quarantine-delete preconditions fail."""


@dataclass(frozen=True)
class QuarantineDeletePreview:
    """Read-only intended deltas for one bounded quarantine delete."""

    target_relative_path: str
    retained_relative_path: str
    quarantine_relative_path: str
    document_id: str
    source_hash: str
    target_source_file_id: str
    retained_source_file_id: str
    approved_vector_ids: tuple[str, ...]
    operation_id: str
    registry_action: str = "prepare_quarantine_registry_transition"


@dataclass(frozen=True)
class QuarantineDeleteRecoveryResult:
    """Outcome of explicit quarantine-delete journal recovery."""

    operation_id: str
    phase: str
    success: bool
    verified: bool
    compensated: bool
    recovery_required: bool
    residual_recovery_notes: tuple[str, ...] = ()
    error_message: str | None = None


@dataclass(frozen=True)
class QuarantineDeleteResult:
    """Outcome of a quarantine-delete dry-run or execute attempt."""

    outcome: str
    execute: bool
    operation_id: str
    success: bool
    dry_run: bool
    compensated: bool
    recovery_required: bool
    registry_committed: bool = False
    filesystem_quarantined: bool = False
    tracker_updated: bool = False
    chroma_updated: bool = False
    preview: QuarantineDeletePreview | None = None
    approval: QuarantineDeleteApprovalValidationResult | None = None
    registry_transition: QuarantineRegistryTransitionResult | None = None
    residual_unrecovered: tuple[str, ...] = ()
    error_message: str | None = None


@dataclass
class _FilesystemSnapshot:
    target_relative_path: str
    retained_relative_path: str
    quarantine_relative_path: str
    target_hash: str
    retained_hash: str


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
    target_locator_state: dict[str, Any] | None
    retained_locator_state: dict[str, Any] | None
    third_locator_states: dict[str, dict[str, Any] | None]


@dataclass
class _ExecutionProgress:
    registry_prepared: bool = False
    filesystem_quarantined: bool = False
    tracker_updated: bool = False
    chroma_updated: bool = False
    registry_committed: bool = False


@dataclass(frozen=True)
class QuarantineDeleteRequest:
    """Bounded input for one approved quarantine delete."""

    approval_artifact: Mapping[str, Any]
    preflight_evidence: QuarantineDeleteEvidence
    approval_context: QuarantineDeleteApprovalContext
    target_relative_path: str
    retained_relative_path: str
    quarantine_relative_path: str
    library_root: str
    persist_dir: str
    registry_db: str
    tracker_path: str
    target_source_file_id: str
    retained_source_file_id: str
    approved_vector_ids: tuple[str, ...]
    registry_collection: str
    operation_id: str
    execute: bool = False
    actor: str | None = None


def _default_now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def execute_quarantine_delete(
    request: QuarantineDeleteRequest,
    *,
    tracker_writer: TrackerWriter | None = None,
    chroma_updater: ChromaUpdater | None = None,
    filesystem_renamer: FilesystemRenamer | None = None,
    now_utc_provider: NowUtcProvider | None = None,
) -> QuarantineDeleteResult:
    """Dry-run or execute one bounded approved quarantine delete."""
    write_tracker_fn = tracker_writer or write_tracker_atomic
    update_chroma_fn = chroma_updater or update_chroma_source_metadata
    rename_fn = filesystem_renamer or _atomic_same_filesystem_rename
    now_fn = now_utc_provider or _default_now_utc

    try:
        ctx = _validate_request(request)
    except (QuarantineDeleteError, PathNormalizationError, ValueError) as exc:
        return _blocked_result(request, str(exc))

    if not request.execute:
        try:
            fresh, approval = _collect_validate_and_bind(
                request, ctx, now_utc=now_fn()
            )
        except (
            QuarantineDeleteError,
            QuarantineDeletePreflightError,
            QuarantineDeleteApprovalValidationError,
        ) as exc:
            return _blocked_result(request, str(exc))
        preview = _build_preview(ctx, approval)
        return QuarantineDeleteResult(
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
    target_abs: Path | None = None
    quarantine_abs: Path | None = None
    retained_abs: Path | None = None
    registry_transition: QuarantineRegistryTransitionResult | None = None
    residual: list[str] = []
    fs_snapshot: _FilesystemSnapshot | None = None
    side_snapshot: _SideStoreSnapshot | None = None
    journal_file: Path | None = None
    preview: QuarantineDeletePreview | None = None
    approval_locked: QuarantineDeleteApprovalValidationResult | None = None

    try:
        with _governed_quarantine_delete_lock(ctx["persist_dir"]):
            now_utc = now_fn()
            try:
                fresh_locked, approval_locked = _collect_validate_and_bind(
                    request, ctx, now_utc=now_utc
                )
            except (
                QuarantineDeleteError,
                QuarantineDeletePreflightError,
                QuarantineDeleteApprovalValidationError,
            ) as exc:
                raise QuarantineDeleteError(str(exc)) from exc

            preview = _build_preview(ctx, approval_locked)
            with open_registry(registry_path) as conn:
                try:
                    validate_quarantine_registry_preconditions(
                        conn,
                        approval=approval_locked,
                        preflight_evidence=fresh_locked,
                        target_source_file_id=ctx["target_source_file_id"],
                        retained_source_file_id=ctx["retained_source_file_id"],
                        registry_collection=ctx["registry_collection"],
                        operation_id=ctx["operation_id"],
                    )
                except QuarantineRegistryTransitionError as exc:
                    raise QuarantineDeleteError(str(exc)) from exc

            target_abs = _resolve_under_library_root(library_root, ctx["target_path"])
            retained_abs = _resolve_under_library_root(library_root, ctx["retained_path"])
            quarantine_abs = _resolve_under_library_root(
                library_root, ctx["quarantine_path"]
            )
            fs_snapshot = _capture_filesystem_snapshot(ctx, library_root=library_root)
            side_snapshot = _capture_side_store_snapshot(ctx)
            registry_summary = _capture_initial_registry_state_summary(
                ctx, side_snapshot
            )

            journal_file = create_prepared_journal(
                persist_dir=ctx["persist_dir"],
                operation_id=ctx["operation_id"],
                target_relative_path=ctx["target_path"],
                retained_relative_path=ctx["retained_path"],
                quarantine_relative_path=ctx["quarantine_path"],
                expected_sha256=ctx["source_hash"],
                document_id=ctx["document_id"],
                source_hash=ctx["source_hash"],
                approved_vector_ids=ctx["approved_vector_ids"],
                target_source_file_id=ctx["target_source_file_id"],
                retained_source_file_id=ctx["retained_source_file_id"],
                approval_digest=approval_locked.approval_digest,
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
                    registry_transition = prepare_quarantine_registry_transition(
                        conn,
                        approval=approval_locked,
                        preflight_evidence=fresh_locked,
                        target_source_file_id=ctx["target_source_file_id"],
                        retained_source_file_id=ctx["retained_source_file_id"],
                        registry_collection=ctx["registry_collection"],
                        operation_id=ctx["operation_id"],
                        actor=request.actor,
                        created_at=now_utc,
                    )
                    progress.registry_prepared = True
                    advance_journal_phase(
                        journal_file,
                        PHASE_REGISTRY_PREPARED,
                        updated_at=now_utc,
                    )

                    _validate_quarantine_parent(library_root, ctx["quarantine_path"])
                    rename_fn(target_abs, quarantine_abs)
                    progress.filesystem_quarantined = True
                    _verify_quarantine_hash(quarantine_abs, fs_snapshot.target_hash)
                    advance_journal_phase(
                        journal_file,
                        PHASE_FILESYSTEM_QUARANTINED,
                        updated_at=now_utc,
                    )

                    tracker_after = build_tracker_after_quarantine(
                        side_snapshot.tracker_root, ctx
                    )
                    write_tracker_fn(tracker_file, tracker_after)
                    progress.tracker_updated = True
                    advance_journal_phase(
                        journal_file,
                        PHASE_TRACKER_UPDATED,
                        updated_at=now_utc,
                    )

                    if ctx["approved_vector_ids"]:
                        chroma_delta = {
                            vector_id: ctx["retained_path"]
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
                retained_abs=retained_abs,
                fs_snapshot=fs_snapshot,
            )
            advance_journal_phase(journal_file, PHASE_VERIFIED, updated_at=now_utc)
            return QuarantineDeleteResult(
                outcome=OUTCOME_SUCCESS,
                execute=True,
                operation_id=ctx["operation_id"],
                success=True,
                dry_run=False,
                compensated=False,
                recovery_required=False,
                registry_committed=True,
                filesystem_quarantined=True,
                tracker_updated=progress.tracker_updated,
                chroma_updated=progress.chroma_updated,
                preview=preview,
                approval=approval_locked,
                registry_transition=registry_transition,
            )
    except Exception as exc:  # noqa: BLE001 - bounded compensation boundary
        if preview is None:
            return QuarantineDeleteResult(
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
            return QuarantineDeleteResult(
                outcome=OUTCOME_RECOVERY_REQUIRED,
                execute=True,
                operation_id=ctx["operation_id"],
                success=False,
                dry_run=False,
                compensated=False,
                recovery_required=True,
                registry_committed=True,
                filesystem_quarantined=progress.filesystem_quarantined,
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
                progress.filesystem_quarantined,
                progress.tracker_updated,
                progress.chroma_updated,
            )
        )
        if not mutated:
            return QuarantineDeleteResult(
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

        if (
            fs_snapshot is None
            or side_snapshot is None
            or target_abs is None
            or quarantine_abs is None
        ):
            return QuarantineDeleteResult(
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
                target_abs=target_abs,
                quarantine_abs=quarantine_abs,
                tracker_file=tracker_file,
                chroma_file=chroma_file,
                progress=progress,
                registry_path=registry_path,
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
        return QuarantineDeleteResult(
            outcome=outcome,
            execute=True,
            operation_id=ctx["operation_id"],
            success=False,
            dry_run=False,
            compensated=compensated,
            recovery_required=not compensated,
            registry_committed=False,
            filesystem_quarantined=progress.filesystem_quarantined and not residual,
            tracker_updated=progress.tracker_updated and not residual,
            chroma_updated=progress.chroma_updated and not residual,
            preview=preview,
            approval=approval_locked,
            registry_transition=registry_transition if progress.registry_prepared else None,
            residual_unrecovered=tuple(residual),
            error_message=str(exc),
        )


def recover_quarantine_delete(
    *,
    persist_dir: str,
    operation_id: str,
    library_root: str,
    registry_db: str,
    tracker_path: str,
) -> QuarantineDeleteRecoveryResult:
    """Explicitly recover one bounded quarantine delete from its exact journal."""
    try:
        op = validate_operation_id(operation_id)
        validate_persist_dir_path(persist_dir)
    except QuarantineDeleteJournalError as exc:
        return QuarantineDeleteRecoveryResult(
            operation_id=operation_id,
            phase="",
            success=False,
            verified=False,
            compensated=False,
            recovery_required=True,
            error_message=str(exc),
        )

    try:
        with _governed_quarantine_delete_lock(persist_dir):
            try:
                journal = read_journal_exact(persist_dir, op)
                validate_journal_bindings(
                    journal,
                    persist_dir=persist_dir,
                    library_root=library_root,
                    registry_db=registry_db,
                    tracker_path=tracker_path,
                )
            except (QuarantineDeleteJournalError, QuarantineDeleteError) as exc:
                return QuarantineDeleteRecoveryResult(
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
                return QuarantineDeleteRecoveryResult(
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
                    return QuarantineDeleteRecoveryResult(
                        operation_id=operation_id,
                        phase=PHASE_RECOVERY_REQUIRED,
                        success=False,
                        verified=False,
                        compensated=False,
                        recovery_required=True,
                        residual_recovery_notes=tuple(findings),
                    )
                advance_journal_phase(journal_file, PHASE_VERIFIED)
                return QuarantineDeleteRecoveryResult(
                    operation_id=operation_id,
                    phase=PHASE_VERIFIED,
                    success=True,
                    verified=True,
                    compensated=False,
                    recovery_required=False,
                )

            if phase not in PRE_COMMIT_PHASES:
                return QuarantineDeleteRecoveryResult(
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
            target_abs = _resolve_under_library_root(library, ctx["target_path"])
            quarantine_abs = _resolve_under_library_root(library, ctx["quarantine_path"])

            residual = list(
                _compensate_before_commit(
                    ctx,
                    fs_snapshot=fs_snapshot,
                    side_snapshot=side_snapshot,
                    library_root=library,
                    target_abs=target_abs,
                    quarantine_abs=quarantine_abs,
                    tracker_file=tracker_file,
                    chroma_file=chroma_file,
                    progress=progress,
                    registry_path=Path(registry_db),
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
                return QuarantineDeleteRecoveryResult(
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
                return QuarantineDeleteRecoveryResult(
                    operation_id=operation_id,
                    phase=PHASE_RECOVERY_REQUIRED,
                    success=False,
                    verified=False,
                    compensated=False,
                    recovery_required=True,
                    residual_recovery_notes=tuple(verify_notes),
                )

            advance_journal_phase(journal_file, PHASE_COMPENSATED)
            return QuarantineDeleteRecoveryResult(
                operation_id=operation_id,
                phase=PHASE_COMPENSATED,
                success=True,
                verified=False,
                compensated=True,
                recovery_required=False,
            )
    except QuarantineDeleteError as exc:
        return QuarantineDeleteRecoveryResult(
            operation_id=operation_id,
            phase="",
            success=False,
            verified=False,
            compensated=False,
            recovery_required=True,
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return QuarantineDeleteRecoveryResult(
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
    approval: QuarantineDeleteApprovalValidationResult,
) -> QuarantineDeletePreview:
    return QuarantineDeletePreview(
        target_relative_path=ctx["target_path"],
        retained_relative_path=ctx["retained_path"],
        quarantine_relative_path=ctx["quarantine_path"],
        document_id=approval.document_id,
        source_hash=approval.source_hash,
        target_source_file_id=ctx["target_source_file_id"],
        retained_source_file_id=ctx["retained_source_file_id"],
        approved_vector_ids=ctx["approved_vector_ids"],
        operation_id=ctx["operation_id"],
    )


def _capture_initial_registry_state_summary(
    ctx: dict[str, Any],
    side_snapshot: _SideStoreSnapshot,
) -> dict[str, Any]:
    target_state = side_snapshot.target_locator_state or {}
    retained_state = side_snapshot.retained_locator_state or {}
    return {
        "document_id": ctx["document_id"],
        "target_source_file_id": ctx["target_source_file_id"],
        "retained_source_file_id": ctx["retained_source_file_id"],
        "target_locator_activity_state": target_state.get("activity_state"),
        "retained_locator_activity_state": retained_state.get("activity_state"),
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
        raise QuarantineDeleteError("journal approved_vector_ids must be a list")
    return {
        "library_root": str(journal["library_root"]),
        "persist_dir": str(journal["persist_dir"]),
        "registry_db": str(journal["registry_db"]),
        "tracker_path": str(journal["tracker_path"]),
        "target_path": str(journal["target_relative_path"]),
        "retained_path": str(journal["retained_relative_path"]),
        "quarantine_path": str(journal["quarantine_relative_path"]),
        "source_hash": str(journal["source_hash"]),
        "document_id": str(journal["document_id"]),
        "target_source_file_id": str(journal["target_source_file_id"]),
        "retained_source_file_id": str(journal["retained_source_file_id"]),
        "approved_vector_ids": tuple(str(x) for x in vector_ids),
        "registry_collection": str(journal["registry_collection"]),
        "operation_id": str(journal["operation_id"]),
    }


def _side_snapshot_from_journal(journal: Mapping[str, Any]) -> _SideStoreSnapshot:
    tracker_root = journal.get("tracker_snapshot") or {}
    if not isinstance(tracker_root, dict):
        raise QuarantineDeleteError("journal tracker_snapshot must be a mapping")
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
    target_state = None
    retained_state = None
    if isinstance(summary, dict):
        target_state = {
            "source_file_id": summary.get("target_source_file_id"),
            "activity_state": summary.get("target_locator_activity_state"),
        }
        retained_state = {
            "source_file_id": summary.get("retained_source_file_id"),
            "activity_state": summary.get("retained_locator_activity_state"),
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
        target_locator_state=target_state,
        retained_locator_state=retained_state,
        third_locator_states=third,
    )


def _filesystem_snapshot_from_journal(journal: Mapping[str, Any]) -> _FilesystemSnapshot:
    retained_hash = str(journal["expected_sha256"])
    return _FilesystemSnapshot(
        target_relative_path=str(journal["target_relative_path"]),
        retained_relative_path=str(journal["retained_relative_path"]),
        quarantine_relative_path=str(journal["quarantine_relative_path"]),
        target_hash=retained_hash,
        retained_hash=retained_hash,
    )


def _progress_from_journal_phase(phase: str, ctx: dict[str, Any]) -> _ExecutionProgress:
    order = (
        PHASE_PREPARED,
        PHASE_REGISTRY_PREPARED,
        PHASE_FILESYSTEM_QUARANTINED,
        PHASE_TRACKER_UPDATED,
        PHASE_CHROMA_UPDATED,
    )
    idx = order.index(phase) if phase in order else -1
    chroma_applicable = bool(ctx["approved_vector_ids"])
    tracker_applicable = True
    return _ExecutionProgress(
        registry_prepared=idx >= order.index(PHASE_REGISTRY_PREPARED),
        filesystem_quarantined=idx >= order.index(PHASE_FILESYSTEM_QUARANTINED),
        tracker_updated=tracker_applicable and idx >= order.index(PHASE_TRACKER_UPDATED),
        chroma_updated=chroma_applicable and idx >= order.index(PHASE_CHROMA_UPDATED),
    )


def _inspect_registry_commit_state(
    registry_db: str,
    journal: Mapping[str, Any],
    ctx: dict[str, Any],
) -> tuple[bool, bool]:
    target_sf = ctx["target_source_file_id"]
    with open_registry(registry_db) as conn:
        target_state = get_locator_lifecycle_state(conn, source_file_id=target_sf)
        completed = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE operation_id = ? AND event_type = ?",
            (ctx["operation_id"], LOCATOR_EVENT_COMPENSATION_COMPLETED),
        ).fetchone()["c"]
    target_inactive = (
        target_state is not None and target_state["activity_state"] == LOCATOR_INACTIVE
    )
    committed = target_inactive and completed >= 1
    ambiguous = (completed >= 1 or target_inactive) and not committed
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
    target_abs = _resolve_under_library_root(library_root, ctx["target_path"])
    quarantine_abs = _resolve_under_library_root(library_root, ctx["quarantine_path"])
    retained_abs = _resolve_under_library_root(library_root, ctx["retained_path"])
    if target_abs.exists():
        findings.append("post_commit_target_still_present")
    if not quarantine_abs.is_file():
        findings.append("post_commit_quarantine_missing")
    elif hashlib.sha256(quarantine_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        findings.append("post_commit_quarantine_hash_mismatch")
    if not retained_abs.is_file():
        findings.append("post_commit_retained_missing")
    elif hashlib.sha256(retained_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        findings.append("post_commit_retained_hash_mismatch")

    with open_registry(registry_db) as conn:
        target_state = get_locator_lifecycle_state(
            conn, source_file_id=ctx["target_source_file_id"]
        )
        retained_state = get_locator_lifecycle_state(
            conn, source_file_id=ctx["retained_source_file_id"]
        )
        if target_state is None or target_state["activity_state"] != LOCATOR_INACTIVE:
            findings.append("post_commit_target_locator_not_inactive")
        if retained_state is None or retained_state["activity_state"] != LOCATOR_ACTIVE:
            findings.append("post_commit_retained_locator_not_active")

    tracker = read_tracker_json(tracker_file)
    record = tracker.get(ctx["source_hash"])
    if record is None or list(record.get("paths") or []) != [ctx["retained_path"]]:
        findings.append("post_commit_tracker_path_mismatch")

    if ctx["approved_vector_ids"]:
        try:
            validate_bounded_chroma_evidence(
                persist_dir=ctx["persist_dir"],
                old_path=ctx["retained_path"],
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
    target_abs = _resolve_under_library_root(library_root, ctx["target_path"])
    quarantine_abs = _resolve_under_library_root(library_root, ctx["quarantine_path"])
    retained_abs = _resolve_under_library_root(library_root, ctx["retained_path"])
    if not target_abs.is_file():
        notes.append("restored_target_missing")
    elif hashlib.sha256(target_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        notes.append("restored_target_hash_mismatch")
    if quarantine_abs.exists():
        notes.append("restored_quarantine_still_present")
    if not retained_abs.is_file():
        notes.append("restored_retained_missing")
    elif hashlib.sha256(retained_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        notes.append("restored_retained_hash_mismatch")

    tracker = read_tracker_json(tracker_file)
    if tracker != side_snapshot.tracker_root:
        notes.append("restored_tracker_mismatch")

    if ctx["approved_vector_ids"]:
        try:
            validate_bounded_chroma_evidence(
                persist_dir=ctx["persist_dir"],
                old_path=ctx["retained_path"],
                document_id=ctx["document_id"],
                source_hash=ctx["source_hash"],
                approved_vector_ids=ctx["approved_vector_ids"],
            )
        except ValueError as exc:
            notes.append(f"restored_chroma_mismatch: {exc}")

    with open_registry(ctx["registry_db"]) as conn:
        target_state = get_locator_lifecycle_state(
            conn, source_file_id=ctx["target_source_file_id"]
        )
        if target_state is None or target_state["activity_state"] != LOCATOR_ACTIVE:
            notes.append("restored_target_locator_not_active")
        retained_state = get_locator_lifecycle_state(
            conn, source_file_id=ctx["retained_source_file_id"]
        )
        if retained_state is None or retained_state["activity_state"] != LOCATOR_ACTIVE:
            notes.append("restored_retained_locator_not_active")

    return notes


def _blocked_result(request: QuarantineDeleteRequest, message: str) -> QuarantineDeleteResult:
    try:
        operation_id = _require_operation_id(request.operation_id)
    except QuarantineDeleteError:
        operation_id = (
            request.operation_id.strip() if isinstance(request.operation_id, str) else ""
        )
    return QuarantineDeleteResult(
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
    request: QuarantineDeleteRequest,
    ctx: dict[str, Any],
    *,
    now_utc: str,
) -> tuple[QuarantineDeleteEvidence, QuarantineDeleteApprovalValidationResult]:
    try:
        fresh = collect_quarantine_delete_evidence(
            library_root=ctx["library_root"],
            persist_dir=ctx["persist_dir"],
            registry_db=ctx["registry_db"],
            tracker_path=ctx["tracker_path"],
            target_path=ctx["target_path"],
            retained_path=ctx["retained_path"],
            quarantine_path=ctx["quarantine_path"],
        )
        approval = validate_quarantine_delete_approval(
            request.approval_artifact,
            fresh.plan,
            target_path=ctx["target_path"],
            retained_path=ctx["retained_path"],
            quarantine_path=ctx["quarantine_path"],
            context=request.approval_context,
            preflight_evidence=fresh,
            now_utc=now_utc,
        )
    except (QuarantineDeletePreflightError, QuarantineDeleteApprovalValidationError) as exc:
        raise QuarantineDeleteError(str(exc)) from exc
    _compare_evidence_with_approved(
        fresh=fresh,
        approved=request.preflight_evidence,
        ctx=ctx,
    )
    _validate_side_store_evidence(ctx)
    return fresh, approval


def _validate_request(request: QuarantineDeleteRequest) -> dict[str, Any]:
    if not isinstance(request.approval_artifact, Mapping):
        raise QuarantineDeleteError("approval_artifact must be a mapping")
    if not isinstance(request.preflight_evidence, QuarantineDeleteEvidence):
        raise QuarantineDeleteError("preflight_evidence must be a QuarantineDeleteEvidence")
    if not isinstance(request.approval_context, QuarantineDeleteApprovalContext):
        raise QuarantineDeleteError("approval_context must be a QuarantineDeleteApprovalContext")

    library_root = _require_absolute_path(request.library_root, "library_root")
    persist_dir = _require_absolute_path(request.persist_dir, "persist_dir")
    registry_db = _require_absolute_path(request.registry_db, "registry_db")
    tracker_path = _require_absolute_path(request.tracker_path, "tracker_path")

    target_path = _require_relative_path(request.target_relative_path, "target_relative_path")
    retained_path = _require_relative_path(
        request.retained_relative_path,
        "retained_relative_path",
    )
    quarantine_path = _require_relative_path(
        request.quarantine_relative_path,
        "quarantine_relative_path",
    )
    if len({target_path, retained_path, quarantine_path}) != 3:
        raise QuarantineDeleteError(
            "target, retained, and quarantine paths must be distinct"
        )

    target_sf = _require_non_empty_str(request.target_source_file_id, "target_source_file_id")
    retained_sf = _require_non_empty_str(
        request.retained_source_file_id,
        "retained_source_file_id",
    )
    if target_sf == retained_sf:
        raise QuarantineDeleteError(
            "target_source_file_id and retained_source_file_id must differ"
        )
    expected_target_sf = make_source_file_id(
        document_id=request.preflight_evidence.document_id,
        relative_path=target_path,
    )
    if target_sf != expected_target_sf:
        raise QuarantineDeleteError(
            "target_source_file_id must match deterministic locator id for target_path"
        )

    operation_id = _require_operation_id(request.operation_id)
    registry_collection = _require_non_empty_str(
        request.registry_collection,
        "registry_collection",
    )
    approved_vector_ids = _normalize_vector_ids(request.approved_vector_ids)

    if not _same_resolved_path(request.preflight_evidence.library_root, library_root):
        raise QuarantineDeleteError("library_root mismatch with preflight_evidence")
    if not _same_resolved_path(request.preflight_evidence.persist_dir, persist_dir):
        raise QuarantineDeleteError("persist_dir mismatch with preflight_evidence")
    if not _same_resolved_path(request.preflight_evidence.registry_db, registry_db):
        raise QuarantineDeleteError("registry_db mismatch with preflight_evidence")
    if request.preflight_evidence.tracker_path is not None:
        if not _same_resolved_path(request.preflight_evidence.tracker_path, tracker_path):
            raise QuarantineDeleteError("tracker_path mismatch with preflight_evidence")

    if not _same_resolved_path(request.approval_context.registry_db_path, registry_db):
        raise QuarantineDeleteError("registry_db mismatch with approval_context")
    if not _same_resolved_path(request.approval_context.library_root, library_root):
        raise QuarantineDeleteError("library_root mismatch with approval_context")
    if not _same_resolved_path(request.approval_context.persist_dir, persist_dir):
        raise QuarantineDeleteError("persist_dir mismatch with approval_context")
    if not _same_resolved_path(request.approval_context.tracker_path, tracker_path):
        raise QuarantineDeleteError("tracker_path mismatch with approval_context")

    if target_path != request.preflight_evidence.target_path:
        raise QuarantineDeleteError("target_path mismatch with preflight_evidence")
    if retained_path != request.preflight_evidence.retained_path:
        raise QuarantineDeleteError("retained_path mismatch with preflight_evidence")
    if quarantine_path != request.preflight_evidence.quarantine_path:
        raise QuarantineDeleteError("quarantine_path mismatch with preflight_evidence")

    if not isinstance(request.execute, bool):
        raise QuarantineDeleteError("execute must be a boolean")

    for rel, field in (
        (target_path, "target"),
        (retained_path, "retained"),
    ):
        entry = Path(library_root) / rel
        if entry.is_symlink():
            raise QuarantineDeleteError(f"{field} path must be a regular file, not a symlink")

    return {
        "library_root": library_root,
        "persist_dir": persist_dir,
        "registry_db": registry_db,
        "tracker_path": tracker_path,
        "target_path": target_path,
        "retained_path": retained_path,
        "quarantine_path": quarantine_path,
        "target_source_file_id": target_sf,
        "retained_source_file_id": retained_sf,
        "approved_vector_ids": approved_vector_ids,
        "registry_collection": registry_collection,
        "operation_id": operation_id,
        "document_id": request.preflight_evidence.document_id,
        "source_hash": request.preflight_evidence.source_hash,
    }


def _compare_evidence_with_approved(
    *,
    fresh: QuarantineDeleteEvidence,
    approved: QuarantineDeleteEvidence,
    ctx: dict[str, Any],
) -> None:
    pairs = (
        ("target_path", fresh.target_path, approved.target_path),
        ("retained_path", fresh.retained_path, approved.retained_path),
        ("quarantine_path", fresh.quarantine_path, approved.quarantine_path),
        ("source_hash", fresh.source_hash, approved.source_hash),
        ("document_id", fresh.document_id, approved.document_id),
        ("request_id", fresh.request_id, approved.request_id),
        ("plan_digest", fresh.plan_digest, approved.plan_digest),
    )
    for field, current, expected in pairs:
        if current != expected:
            raise QuarantineDeleteError(f"fresh evidence {field} drift from approved evidence")

    path_pairs = (
        ("library_root", fresh.library_root, approved.library_root),
        ("persist_dir", fresh.persist_dir, approved.persist_dir),
        ("registry_db", fresh.registry_db, approved.registry_db),
    )
    for field, current, expected in path_pairs:
        if not _same_resolved_path(current, expected):
            raise QuarantineDeleteError(f"fresh evidence {field} drift from approved evidence")

    if tuple(sorted(fresh.approved_vector_ids)) != tuple(sorted(approved.approved_vector_ids)):
        raise QuarantineDeleteError("fresh evidence approved_vector_ids drift")
    if tuple(sorted(fresh.approved_vector_ids)) != tuple(sorted(ctx["approved_vector_ids"])):
        raise QuarantineDeleteError("approved_vector_ids mismatch with request")
    if fresh.compatibility_state != approved.compatibility_state:
        raise QuarantineDeleteError("fresh evidence compatibility_state drift")
    if fresh.resolver_classification != approved.resolver_classification:
        raise QuarantineDeleteError("fresh evidence resolver_classification drift")
    if fresh.proposed_operation != approved.proposed_operation:
        raise QuarantineDeleteError("fresh evidence proposed_operation drift")

    if fresh.tracker_path is not None or approved.tracker_path is not None:
        if fresh.tracker_path is None or approved.tracker_path is None:
            raise QuarantineDeleteError("fresh evidence tracker_path drift")
        if not _same_resolved_path(fresh.tracker_path, approved.tracker_path):
            raise QuarantineDeleteError("fresh evidence tracker_path drift")


def _validate_side_store_evidence(ctx: dict[str, Any]) -> None:
    try:
        validate_bounded_tracker_evidence(
            tracker_path=ctx["tracker_path"],
            source_hash=ctx["source_hash"],
            old_path=ctx["retained_path"],
            approved_vector_ids=ctx["approved_vector_ids"],
            document_id=ctx["document_id"],
        )
        validate_bounded_chroma_physical_rows(
            persist_dir=ctx["persist_dir"],
            approved_vector_ids=ctx["approved_vector_ids"],
        )
        validate_bounded_chroma_evidence(
            persist_dir=ctx["persist_dir"],
            old_path=ctx["retained_path"],
            document_id=ctx["document_id"],
            source_hash=ctx["source_hash"],
            approved_vector_ids=ctx["approved_vector_ids"],
        )
    except ValueError as exc:
        raise QuarantineDeleteError(str(exc)) from exc


def _capture_filesystem_snapshot(
    ctx: dict[str, Any],
    *,
    library_root: Path,
) -> _FilesystemSnapshot:
    target_entry = library_root / ctx["target_path"]
    retained_entry = library_root / ctx["retained_path"]
    quarantine_entry = library_root / ctx["quarantine_path"]
    if target_entry.is_symlink():
        raise QuarantineDeleteError("target path must be a regular file, not a symlink")
    if retained_entry.is_symlink():
        raise QuarantineDeleteError("retained path must be a regular file, not a symlink")
    target_abs = _resolve_under_library_root(library_root, ctx["target_path"])
    retained_abs = _resolve_under_library_root(library_root, ctx["retained_path"])
    quarantine_abs = _resolve_under_library_root(library_root, ctx["quarantine_path"])
    _validate_quarantine_parent(library_root, ctx["quarantine_path"])
    if not target_abs.is_file():
        raise QuarantineDeleteError("target file must exist before quarantine")
    if not retained_abs.is_file():
        raise QuarantineDeleteError("retained file must exist before quarantine")
    if quarantine_abs.exists():
        raise QuarantineDeleteError("quarantine path must be absent before quarantine")
    target_digest = hashlib.sha256(target_abs.read_bytes()).hexdigest()
    retained_digest = hashlib.sha256(retained_abs.read_bytes()).hexdigest()
    if target_digest != ctx["source_hash"]:
        raise QuarantineDeleteError("target file SHA-256 mismatch")
    if retained_digest != ctx["source_hash"]:
        raise QuarantineDeleteError("retained file SHA-256 mismatch")
    if target_digest != retained_digest:
        raise QuarantineDeleteError("target and retained SHA-256 must match")
    return _FilesystemSnapshot(
        target_relative_path=ctx["target_path"],
        retained_relative_path=ctx["retained_path"],
        quarantine_relative_path=ctx["quarantine_path"],
        target_hash=target_digest,
        retained_hash=retained_digest,
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
        target_state = copy.deepcopy(by_id.get(ctx["target_source_file_id"]))
        retained_state = copy.deepcopy(by_id.get(ctx["retained_source_file_id"]))
        third = {
            sf: copy.deepcopy(st)
            for sf, st in by_id.items()
            if sf not in {ctx["target_source_file_id"], ctx["retained_source_file_id"]}
        }

    if retained_state is None or retained_state["activity_state"] != LOCATOR_ACTIVE:
        raise QuarantineDeleteError("retained locator must be ACTIVE before quarantine")

    return _SideStoreSnapshot(
        tracker_root=copy.deepcopy(tracker_root),
        chroma_sources=chroma_sources,
        chroma_identity=chroma_identity,
        target_locator_state=target_state,
        retained_locator_state=retained_state,
        third_locator_states=third,
    )


def _validate_quarantine_parent(library_root: Path, quarantine_relative_path: str) -> None:
    root = library_root.resolve()
    parent_entry = (library_root / quarantine_relative_path).parent
    if not parent_entry.exists():
        raise QuarantineDeleteError("quarantine parent directory must exist before quarantine")
    if parent_entry.is_symlink():
        resolved_parent = parent_entry.resolve()
        _assert_path_under_root(resolved_parent, root)
        if not resolved_parent.is_dir():
            raise QuarantineDeleteError("quarantine parent must be a directory")
        return
    if not parent_entry.is_dir():
        raise QuarantineDeleteError("quarantine parent must be a directory")
    _assert_path_under_root(parent_entry.resolve(), root)


def _atomic_same_filesystem_rename(source_abs: Path, dest_abs: Path) -> None:
    if dest_abs.exists():
        raise QuarantineDeleteError("quarantine path already exists")
    parent = dest_abs.parent
    if not parent.exists():
        raise QuarantineDeleteError("quarantine parent directory must exist before quarantine")
    if not parent.is_dir():
        raise QuarantineDeleteError("quarantine parent must be a directory")
    try:
        os.replace(source_abs, dest_abs)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise QuarantineDeleteError(
                "cross-device rename is not supported; refusing copy/delete fallback"
            ) from exc
        raise QuarantineDeleteError(f"filesystem rename failed: {exc}") from exc


def _verify_quarantine_hash(quarantine_abs: Path, expected_hash: str) -> None:
    if not quarantine_abs.is_file():
        raise QuarantineDeleteError("quarantine file missing after rename")
    actual = hashlib.sha256(quarantine_abs.read_bytes()).hexdigest()
    if actual != expected_hash:
        raise QuarantineDeleteError("quarantine SHA-256 mismatch after rename")


def _verify_pre_commit(
    ctx: dict[str, Any],
    *,
    registry_transition: QuarantineRegistryTransitionResult,
    library_root: Path,
    tracker_file: Path,
    side_snapshot: _SideStoreSnapshot,
    conn: Any,
) -> None:
    target_abs = _resolve_under_library_root(library_root, ctx["target_path"])
    quarantine_abs = _resolve_under_library_root(library_root, ctx["quarantine_path"])
    retained_abs = _resolve_under_library_root(library_root, ctx["retained_path"])
    if target_abs.exists():
        raise QuarantineDeleteError("pre-commit target path still present")
    if not quarantine_abs.is_file():
        raise QuarantineDeleteError("pre-commit quarantine file missing")
    if hashlib.sha256(quarantine_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        raise QuarantineDeleteError("pre-commit quarantine hash mismatch")
    if not retained_abs.is_file():
        raise QuarantineDeleteError("pre-commit retained file missing")
    if hashlib.sha256(retained_abs.read_bytes()).hexdigest() != ctx["source_hash"]:
        raise QuarantineDeleteError("pre-commit retained hash mismatch")

    target_state = get_locator_lifecycle_state(
        conn, source_file_id=registry_transition.target_source_file_id
    )
    retained_state = get_locator_lifecycle_state(
        conn, source_file_id=registry_transition.retained_source_file_id
    )
    if target_state is None or target_state["activity_state"] != LOCATOR_INACTIVE:
        raise QuarantineDeleteError("pre-commit target locator is not INACTIVE")
    if retained_state is None or retained_state["activity_state"] != LOCATOR_ACTIVE:
        raise QuarantineDeleteError("pre-commit retained locator is not ACTIVE")

    tracker = read_tracker_json(tracker_file)
    record = tracker[ctx["source_hash"]]
    if list(record.get("paths") or []) != [ctx["retained_path"]]:
        raise QuarantineDeleteError("pre-commit tracker path mismatch")
    if tuple(str(x) for x in (record.get("chunk_ids") or [])) != ctx["approved_vector_ids"]:
        raise QuarantineDeleteError("pre-commit tracker chunk_ids changed")

    if ctx["approved_vector_ids"]:
        _verify_bounded_chroma_identity(ctx, expected_source_path=ctx["retained_path"])

    current_states = {
        row["source_file_id"]: row
        for row in list_locator_lifecycle_states_for_document(
            conn, document_id=ctx["document_id"]
        )
    }
    for sf, before in side_snapshot.third_locator_states.items():
        after = current_states.get(sf)
        if after is None or after["activity_state"] != before["activity_state"]:
            raise QuarantineDeleteError(
                f"pre-commit unexpected third locator change for {sf!r}"
            )


def _verify_post_commit(
    ctx: dict[str, Any],
    *,
    registry_path: Path,
    library_root: Path,
    tracker_file: Path,
    chroma_file: Path,
    registry_transition: QuarantineRegistryTransitionResult,
    side_snapshot: _SideStoreSnapshot,
    retained_abs: Path,
    fs_snapshot: _FilesystemSnapshot,
) -> None:
    target_abs = _resolve_under_library_root(library_root, ctx["target_path"])
    quarantine_abs = _resolve_under_library_root(library_root, ctx["quarantine_path"])
    if target_abs.exists():
        raise QuarantineDeleteError("post-commit target path still present")
    if not quarantine_abs.is_file():
        raise QuarantineDeleteError("post-commit quarantine file missing")
    if hashlib.sha256(quarantine_abs.read_bytes()).hexdigest() != fs_snapshot.target_hash:
        raise QuarantineDeleteError("post-commit quarantine hash mismatch")
    if hashlib.sha256(retained_abs.read_bytes()).hexdigest() != fs_snapshot.retained_hash:
        raise QuarantineDeleteError("post-commit retained hash mismatch")

    with open_registry(registry_path) as conn:
        target_state = get_locator_lifecycle_state(
            conn, source_file_id=registry_transition.target_source_file_id
        )
        retained_state = get_locator_lifecycle_state(
            conn, source_file_id=registry_transition.retained_source_file_id
        )
        if target_state is None or target_state["activity_state"] != LOCATOR_INACTIVE:
            raise QuarantineDeleteError("post-commit target locator is not INACTIVE")
        if retained_state is None or retained_state["activity_state"] != LOCATOR_ACTIVE:
            raise QuarantineDeleteError("post-commit retained locator is not ACTIVE")
        for sf in (
            registry_transition.target_source_file_id,
            registry_transition.retained_source_file_id,
        ):
            check = verify_locator_state_event_consistency(conn, source_file_id=sf)
            if not check["consistent"]:
                raise QuarantineDeleteError(
                    f"post-commit locator consistency failed for {sf!r}"
                )

    tracker = read_tracker_json(tracker_file)
    record = tracker[ctx["source_hash"]]
    if list(record.get("paths") or []) != [ctx["retained_path"]]:
        raise QuarantineDeleteError("post-commit tracker path mismatch")

    if ctx["approved_vector_ids"]:
        _verify_bounded_chroma_identity(ctx, expected_source_path=ctx["retained_path"])


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
        raise QuarantineDeleteError(str(exc)) from exc


def _compensate_before_commit(
    ctx: dict[str, Any],
    *,
    fs_snapshot: _FilesystemSnapshot,
    side_snapshot: _SideStoreSnapshot,
    library_root: Path,
    target_abs: Path,
    quarantine_abs: Path,
    tracker_file: Path,
    chroma_file: Path,
    progress: _ExecutionProgress,
    registry_path: Path,
    write_tracker_fn: TrackerWriter,
    update_chroma_fn: ChromaUpdater,
) -> list[str]:
    residual: list[str] = []

    if progress.registry_committed:
        try:
            with open_registry(registry_path) as conn:
                with registry_transaction(conn):
                    restore_locator_quarantine_state_after_failure(
                        conn,
                        document_id=ctx["document_id"],
                        target_source_file_id=ctx["target_source_file_id"],
                        target_activity_state=LOCATOR_ACTIVE,
                        failed_operation_id=ctx["operation_id"],
                        operation_id=f"{ctx['operation_id']}:compensate",
                        source="quarantine_delete_compensation",
                    )
        except Exception as exc:  # noqa: BLE001
            residual.append(f"registry_restore_failed: {exc}")

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

    if progress.filesystem_quarantined:
        try:
            _restore_filesystem_quarantine(
                library_root=library_root,
                target_abs=target_abs,
                quarantine_abs=quarantine_abs,
                expected_hash=fs_snapshot.target_hash,
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
                raise QuarantineDeleteError(
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


def _restore_filesystem_quarantine(
    *,
    library_root: Path,
    target_abs: Path,
    quarantine_abs: Path,
    expected_hash: str,
) -> None:
    root = library_root.resolve()
    _assert_path_under_root(target_abs.resolve(), root)
    _assert_path_under_root(quarantine_abs.resolve(), root)
    if target_abs.is_file():
        return
    if not quarantine_abs.is_file():
        raise QuarantineDeleteError("filesystem rollback requires quarantine file")
    actual = hashlib.sha256(quarantine_abs.read_bytes()).hexdigest()
    if actual != expected_hash:
        raise QuarantineDeleteError("filesystem rollback quarantine hash mismatch")
    os.replace(quarantine_abs, target_abs)


@contextmanager
def _governed_quarantine_delete_lock(persist_dir: str) -> Iterator[Path]:
    persist = Path(persist_dir).resolve()
    if not persist.is_dir():
        raise QuarantineDeleteError("persist_dir must exist before governed quarantine delete")
    lock_path = (persist / QUARANTINE_DELETE_LOCK_NAME).resolve()
    _assert_path_under_root(lock_path, persist)
    fd: int | None = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode())
    except FileExistsError:
        raise QuarantineDeleteError(
            "another governed quarantine delete holds the persist_dir lock"
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
        raise QuarantineDeleteError(
            f"path {path!s} resolves outside library_root {root!s}"
        ) from exc


def _same_resolved_path(left: str, right: str) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def _require_absolute_path(raw: str, field: str) -> str:
    text = _require_non_empty_str(raw, field)
    path = Path(text)
    if not path.is_absolute():
        raise QuarantineDeleteError(f"{field} must be an absolute path")
    return str(path.resolve())


def _require_relative_path(raw: str, field: str) -> str:
    if "\0" in raw:
        raise QuarantineDeleteError(f"{field} must not contain NUL")
    try:
        return normalize_relative_path(raw)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        raise QuarantineDeleteError(f"invalid {field}: {exc}") from exc


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise QuarantineDeleteError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise QuarantineDeleteError(f"{field} must not contain leading/trailing whitespace")
    return value


def _require_operation_id(operation_id: str) -> str:
    op = _require_non_empty_str(operation_id, "operation_id")
    if "/" in op or "\\" in op:
        raise QuarantineDeleteError("operation_id must be a safe opaque identifier")
    return op


def _normalize_vector_ids(raw: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise QuarantineDeleteError("approved_vector_ids must be a sequence of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _require_non_empty_str(item, "approved_vector_ids entry")
        if text in seen:
            raise QuarantineDeleteError("approved_vector_ids must not contain duplicates")
        seen.add(text)
        out.append(text)
    return tuple(out)

"""Caller-owned atomic registry MOVE transition (Phase E2).

Provisions the destination locator and records the V5 MOVE transition inside
the caller's registry_transaction. Never commits, rolls back, or opens its own
connection. Registry-only orchestration for a future executor.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rag_engine.library_state.move_approval import MoveApprovalValidationResult
    from rag_engine.library_state.move_preflight import PreMoveEvidence

from rag_engine.metadata_registry.exceptions import (
    LifecycleTransitionError,
    RegistryIntegrityError,
    RegistryValidationError,
)
from rag_engine.metadata_registry.locator_lifecycle import (
    LOCATOR_ACTIVE,
    get_locator_lifecycle_state,
    record_locator_move_transition,
)
from rag_engine.metadata_registry.move_provisioning import (
    provision_move_destination_locator,
)
from rag_engine.metadata_registry.repository import _validate_registry_timestamp
from rag_engine.stable_identity import (
    IdentityValidationError,
    validate_document_id,
)

MOVE_REGISTRY_TRANSITION_SOURCE = "move_registry_transition"
MOVE_REGISTRY_TRANSITION_REASON = "registry MOVE transition provisioning"


class MoveRegistryTransitionError(RegistryValidationError):
    """Raised when MOVE registry transition preconditions fail."""


@dataclass(frozen=True)
class MoveRegistryTransitionResult:
    """Outcome of provisioning + V5 MOVE inside one caller-owned transaction."""

    old_source_file_id: str
    new_source_file_id: str
    destination_source_file: dict[str, Any]
    destination_v4_event_id: int
    destination_v5_state: dict[str, Any]
    destination_v5_init_event: dict[str, Any]
    old_move_event_id: int
    new_move_event_id: int
    old_final_activity_state: str
    new_final_activity_state: str
    operation_id: str
    affected_source_file_ids: tuple[str, str]


def prepare_move_registry_transition(
    conn: sqlite3.Connection,
    *,
    approval: MoveApprovalValidationResult,
    pre_move_evidence: PreMoveEvidence,
    old_source_file_id: str,
    registry_collection: str,
    operation_id: str,
    actor: str | None = None,
    created_at: str | None = None,
) -> MoveRegistryTransitionResult:
    """Provision destination locator and record V5 MOVE in one transaction."""
    _require_active_transaction(conn)
    _validate_bindings(
        approval=approval,
        pre_move_evidence=pre_move_evidence,
        old_source_file_id=old_source_file_id,
        registry_collection=registry_collection,
        operation_id=operation_id,
        created_at=created_at,
    )
    _validate_conn_registry_path(conn, pre_move_evidence.registry_db)
    _validate_old_locator_active(
        conn,
        old_source_file_id=old_source_file_id,
        document_id=approval.document_id,
        source_path=approval.source_path,
    )

    op = operation_id.strip()
    ts = created_at

    try:
        provisioned = provision_move_destination_locator(
            conn,
            approval=approval,
            document_id=approval.document_id,
            source_hash=approval.source_hash,
            destination_relative_path=approval.destination_path,
            registry_collection=registry_collection,
            operation_id=op,
            actor=actor,
            created_at=ts,
        )
    except (RegistryValidationError, RegistryIntegrityError) as exc:
        if isinstance(exc, MoveRegistryTransitionError):
            raise
        raise MoveRegistryTransitionError(str(exc)) from exc

    destination_sf_id = str(provisioned.source_file["source_file_id"])
    destination_v4_event_id = int(provisioned.v4_event["event_id"])

    try:
        move_result = record_locator_move_transition(
            conn,
            document_id=approval.document_id,
            old_source_file_id=old_source_file_id,
            new_source_file_id=destination_sf_id,
            operation_id=op,
            old_related_v4_event_id=None,
            new_related_v4_event_id=destination_v4_event_id,
            actor=actor,
            source=MOVE_REGISTRY_TRANSITION_SOURCE,
            reason=MOVE_REGISTRY_TRANSITION_REASON,
            created_at=ts,
        )
    except (RegistryValidationError, LifecycleTransitionError) as exc:
        raise MoveRegistryTransitionError(str(exc)) from exc

    old_final = str(move_result["old_state"]["activity_state"])
    new_final = str(move_result["new_state"]["activity_state"])
    old_move_event_id = int(move_result["old_event"]["event_id"])
    new_move_event_id = int(move_result["new_event"]["event_id"])

    return MoveRegistryTransitionResult(
        old_source_file_id=old_source_file_id,
        new_source_file_id=destination_sf_id,
        destination_source_file=provisioned.source_file,
        destination_v4_event_id=destination_v4_event_id,
        destination_v5_state=provisioned.v5_state,
        destination_v5_init_event=provisioned.v5_lifecycle_event,
        old_move_event_id=old_move_event_id,
        new_move_event_id=new_move_event_id,
        old_final_activity_state=old_final,
        new_final_activity_state=new_final,
        operation_id=op,
        affected_source_file_ids=(
            old_source_file_id,
            destination_sf_id,
        ),
    )


def _require_active_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise MoveRegistryTransitionError(
            "prepare_move_registry_transition requires an active registry_transaction"
        )


def _validate_bindings(
    *,
    approval: MoveApprovalValidationResult,
    pre_move_evidence: PreMoveEvidence,
    old_source_file_id: str,
    registry_collection: str,
    operation_id: str,
    created_at: str | None,
) -> None:
    from rag_engine.library_state.move_approval import MoveApprovalValidationResult
    from rag_engine.library_state.move_preflight import PreMoveEvidence

    if not isinstance(approval, MoveApprovalValidationResult):
        raise MoveRegistryTransitionError(
            "approval must be a MoveApprovalValidationResult"
        )
    if not isinstance(pre_move_evidence, PreMoveEvidence):
        raise MoveRegistryTransitionError("pre_move_evidence must be a PreMoveEvidence")

    if approval.source_path != pre_move_evidence.source_path:
        raise MoveRegistryTransitionError("source_path binding mismatch")
    if approval.destination_path != pre_move_evidence.destination_path:
        raise MoveRegistryTransitionError("destination_path binding mismatch")
    if approval.document_id != pre_move_evidence.document_id:
        raise MoveRegistryTransitionError("document_id binding mismatch")
    if approval.source_hash != pre_move_evidence.source_hash:
        raise MoveRegistryTransitionError("source_hash binding mismatch")
    if approval.request_id != pre_move_evidence.request_id:
        raise MoveRegistryTransitionError("request_id binding mismatch")
    if approval.plan_digest != pre_move_evidence.plan_digest:
        raise MoveRegistryTransitionError("plan_digest binding mismatch")

    _validate_registry_db_path(pre_move_evidence.registry_db)

    if not isinstance(old_source_file_id, str) or not old_source_file_id.strip():
        raise MoveRegistryTransitionError("old_source_file_id is required")
    if "\0" in old_source_file_id or old_source_file_id != old_source_file_id.strip():
        raise MoveRegistryTransitionError(
            "old_source_file_id must be a safe non-empty identifier"
        )

    if not isinstance(registry_collection, str) or not registry_collection.strip():
        raise MoveRegistryTransitionError(
            "registry_collection must be a non-empty string"
        )
    if registry_collection != registry_collection.strip():
        raise MoveRegistryTransitionError(
            "registry_collection must not contain leading/trailing whitespace"
        )

    _validate_operation_id(operation_id)
    if created_at is not None:
        _validate_registry_timestamp(created_at)

    try:
        validate_document_id(approval.document_id)
    except IdentityValidationError as exc:
        raise MoveRegistryTransitionError(str(exc)) from exc


def _validate_operation_id(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise MoveRegistryTransitionError("operation_id is required")
    op = operation_id.strip()
    if "\0" in op or "/" in op or "\\" in op:
        raise MoveRegistryTransitionError(
            "operation_id must be a safe opaque identifier"
        )
    return op


def _validate_registry_db_path(registry_db: str) -> None:
    if not isinstance(registry_db, str) or not registry_db.strip():
        raise MoveRegistryTransitionError("registry_db must be a non-empty path")
    path = Path(registry_db)
    if not path.is_absolute():
        raise MoveRegistryTransitionError("registry_db must be an absolute path")


def _validate_conn_registry_path(
    conn: sqlite3.Connection, expected_registry_db: str
) -> None:
    _validate_registry_db_path(expected_registry_db)
    rows = conn.execute("PRAGMA database_list").fetchall()
    main = next((r for r in rows if r["name"] == "main"), None)
    if main is None or not main["file"]:
        raise MoveRegistryTransitionError(
            "connection registry path cannot be verified"
        )
    actual = Path(main["file"]).resolve()
    expected = Path(expected_registry_db).resolve()
    if actual != expected:
        raise MoveRegistryTransitionError(
            "pre_move_evidence registry_db does not match connection database"
        )


def _validate_old_locator_active(
    conn: sqlite3.Connection,
    *,
    old_source_file_id: str,
    document_id: str,
    source_path: str,
) -> None:
    row = conn.execute(
        "SELECT source_file_id, document_id, relative_path FROM source_files "
        "WHERE source_file_id = ?",
        (old_source_file_id,),
    ).fetchone()
    if row is None:
        raise MoveRegistryTransitionError(
            f"old_source_file_id {old_source_file_id!r} is not registered"
        )
    if row["document_id"] != document_id:
        raise MoveRegistryTransitionError(
            "old_source_file_id is not bound to the supplied document_id"
        )
    if row["relative_path"] != source_path:
        raise MoveRegistryTransitionError(
            "old_source_file_id relative_path does not match approval source_path"
        )

    state = get_locator_lifecycle_state(conn, source_file_id=old_source_file_id)
    if state is None:
        raise MoveRegistryTransitionError(
            f"old locator {old_source_file_id!r} has no lifecycle state projection"
        )
    if state["activity_state"] != LOCATOR_ACTIVE:
        raise MoveRegistryTransitionError(
            f"MOVE requires old locator activity_state ACTIVE; "
            f"got {state['activity_state']!r}"
        )

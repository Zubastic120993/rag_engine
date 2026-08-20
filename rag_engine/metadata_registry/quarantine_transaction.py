"""Caller-owned atomic registry quarantine-delete transition (DELETE Phase B).

Records V4 compensation request plus V5 terminal INACTIVE outcome for the target
locator inside the caller's registry_transaction. Never commits on its own.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag_engine.governed_delete.quarantine_approval import (
        QuarantineDeleteApprovalValidationResult,
    )
    from rag_engine.governed_delete.quarantine_preflight import QuarantineDeleteEvidence

from rag_engine.metadata_registry.exceptions import (
    LifecycleTransitionError,
    RegistryValidationError,
)
from rag_engine.metadata_registry.locator_lifecycle import (
    LOCATOR_ACTIVE,
    LOCATOR_INACTIVE,
    get_locator_lifecycle_state,
    record_compensation_request,
    record_terminal_compensation_outcome,
    resolve_alias_registration_v4_event_id,
    verify_locator_state_event_consistency,
)
from rag_engine.metadata_registry.repository import (
    make_source_file_id,
    _validate_registry_timestamp,
)
from rag_engine.stable_identity import IdentityValidationError, validate_document_id

QUARANTINE_REGISTRY_TRANSITION_SOURCE = "quarantine_registry_transition"
QUARANTINE_REGISTRY_TRANSITION_REASON = "registry quarantine-delete transition"


class QuarantineRegistryTransitionError(RegistryValidationError):
    """Raised when quarantine-delete registry transition preconditions fail."""


@dataclass(frozen=True)
class QuarantineRegistryTransitionResult:
    """Outcome of V4 request + V5 terminal transition inside one transaction."""

    target_source_file_id: str
    retained_source_file_id: str
    compensation_request_v4_event_id: int
    compensation_completed_event_id: int
    target_final_activity_state: str
    retained_final_activity_state: str
    operation_id: str
    approval_digest: str


def validate_quarantine_registry_preconditions(
    conn: sqlite3.Connection,
    *,
    approval: QuarantineDeleteApprovalValidationResult,
    preflight_evidence: QuarantineDeleteEvidence,
    target_source_file_id: str,
    retained_source_file_id: str,
    registry_collection: str,
    operation_id: str,
) -> None:
    """Read-only DELETE locator eligibility check before any quarantine-delete writes."""
    _validate_bindings(
        approval=approval,
        preflight_evidence=preflight_evidence,
        target_source_file_id=target_source_file_id,
        retained_source_file_id=retained_source_file_id,
        registry_collection=registry_collection,
        operation_id=operation_id,
        created_at=None,
    )
    _validate_conn_registry_path(conn, preflight_evidence.registry_db)
    _validate_retained_locator_active(
        conn,
        retained_source_file_id=retained_source_file_id,
        document_id=approval.document_id,
        retained_path=approval.retained_path,
    )
    _validate_target_locator_active(
        conn,
        target_source_file_id=target_source_file_id,
        document_id=approval.document_id,
        target_path=approval.target_path,
    )


def prepare_quarantine_registry_transition(
    conn: sqlite3.Connection,
    *,
    approval: QuarantineDeleteApprovalValidationResult,
    preflight_evidence: QuarantineDeleteEvidence,
    target_source_file_id: str,
    retained_source_file_id: str,
    registry_collection: str,
    operation_id: str,
    actor: str | None = None,
    created_at: str | None = None,
) -> QuarantineRegistryTransitionResult:
    """Record compensation request and terminal INACTIVE outcome for target locator."""
    _require_active_transaction(conn)
    validate_quarantine_registry_preconditions(
        conn,
        approval=approval,
        preflight_evidence=preflight_evidence,
        target_source_file_id=target_source_file_id,
        retained_source_file_id=retained_source_file_id,
        registry_collection=registry_collection,
        operation_id=operation_id,
    )
    if created_at is not None:
        _validate_registry_timestamp(created_at)

    op = operation_id.strip()
    ts = created_at
    registration_v4_event_id = resolve_alias_registration_v4_event_id(
        conn, source_file_id=target_source_file_id
    )

    try:
        request_result = record_compensation_request(
            conn,
            source_file_id=target_source_file_id,
            document_id=approval.document_id,
            registration_v4_event_id=registration_v4_event_id,
            compensation_approval_digest=approval.approval_digest,
            operation_id=op,
            actor=actor,
            source=QUARANTINE_REGISTRY_TRANSITION_SOURCE,
            reason=QUARANTINE_REGISTRY_TRANSITION_REASON,
            created_at=ts,
        )
        terminal_result = record_terminal_compensation_outcome(
            conn,
            source_file_id=target_source_file_id,
            document_id=approval.document_id,
            compensation_request_v4_event_id=int(request_result["v4_event"]["event_id"]),
            outcome="COMPLETED",
            operation_id=op,
            actor=actor,
            source=QUARANTINE_REGISTRY_TRANSITION_SOURCE,
            reason=QUARANTINE_REGISTRY_TRANSITION_REASON,
            created_at=ts,
        )
    except (RegistryValidationError, LifecycleTransitionError) as exc:
        raise QuarantineRegistryTransitionError(str(exc)) from exc

    target_final = str(terminal_result["activity_state"])
    if target_final != LOCATOR_INACTIVE:
        raise QuarantineRegistryTransitionError(
            f"quarantine transition must leave target INACTIVE; got {target_final!r}"
        )

    retained_state = get_locator_lifecycle_state(
        conn, source_file_id=retained_source_file_id
    )
    if retained_state is None or retained_state["activity_state"] != LOCATOR_ACTIVE:
        raise QuarantineRegistryTransitionError(
            "retained locator must remain ACTIVE during registry preparation"
        )

    return QuarantineRegistryTransitionResult(
        target_source_file_id=target_source_file_id,
        retained_source_file_id=retained_source_file_id,
        compensation_request_v4_event_id=int(request_result["v4_event"]["event_id"]),
        compensation_completed_event_id=int(
            terminal_result["lifecycle_event"]["event_id"]
        ),
        target_final_activity_state=target_final,
        retained_final_activity_state=LOCATOR_ACTIVE,
        operation_id=op,
        approval_digest=approval.approval_digest,
    )


def _validate_target_locator_active(
    conn: sqlite3.Connection,
    *,
    target_source_file_id: str,
    document_id: str,
    target_path: str,
) -> None:
    row = conn.execute(
        "SELECT source_file_id, document_id, relative_path FROM source_files "
        "WHERE source_file_id = ?",
        (target_source_file_id,),
    ).fetchone()
    if row is None:
        raise QuarantineRegistryTransitionError(
            f"target_source_file_id {target_source_file_id!r} is not registered"
        )
    if row["document_id"] != document_id:
        raise QuarantineRegistryTransitionError(
            "target_source_file_id is not bound to the supplied document_id"
        )
    if row["relative_path"] != target_path:
        raise QuarantineRegistryTransitionError(
            "target_source_file_id relative_path does not match approval target_path"
        )

    consistency = verify_locator_state_event_consistency(
        conn, source_file_id=target_source_file_id
    )
    if not consistency["consistent"]:
        discrepancies = consistency.get("discrepancies") or ()
        raise QuarantineRegistryTransitionError(
            "target locator lifecycle projection is invalid: "
            + "; ".join(discrepancies)
        )

    state = get_locator_lifecycle_state(conn, source_file_id=target_source_file_id)
    if state is None:
        raise QuarantineRegistryTransitionError(
            f"target locator {target_source_file_id!r} has no lifecycle state projection"
        )
    if state["activity_state"] != LOCATOR_ACTIVE:
        raise QuarantineRegistryTransitionError(
            f"quarantine requires target locator activity_state ACTIVE; "
            f"got {state['activity_state']!r}"
        )

    try:
        resolve_alias_registration_v4_event_id(
            conn, source_file_id=target_source_file_id
        )
    except RegistryValidationError as exc:
        raise QuarantineRegistryTransitionError(str(exc)) from exc


def _require_active_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise QuarantineRegistryTransitionError(
            "prepare_quarantine_registry_transition requires an active registry_transaction"
        )


def _validate_bindings(
    *,
    approval: QuarantineDeleteApprovalValidationResult,
    preflight_evidence: QuarantineDeleteEvidence,
    target_source_file_id: str,
    retained_source_file_id: str,
    registry_collection: str,
    operation_id: str,
    created_at: str | None,
) -> None:
    from rag_engine.governed_delete.quarantine_approval import (
        QuarantineDeleteApprovalValidationResult,
    )
    from rag_engine.governed_delete.quarantine_preflight import QuarantineDeleteEvidence

    if not isinstance(approval, QuarantineDeleteApprovalValidationResult):
        raise QuarantineRegistryTransitionError(
            "approval must be a QuarantineDeleteApprovalValidationResult"
        )
    if not isinstance(preflight_evidence, QuarantineDeleteEvidence):
        raise QuarantineRegistryTransitionError(
            "preflight_evidence must be a QuarantineDeleteEvidence"
        )

    if approval.target_path != preflight_evidence.target_path:
        raise QuarantineRegistryTransitionError("target_path binding mismatch")
    if approval.retained_path != preflight_evidence.retained_path:
        raise QuarantineRegistryTransitionError("retained_path binding mismatch")
    if approval.quarantine_path != preflight_evidence.quarantine_path:
        raise QuarantineRegistryTransitionError("quarantine_path binding mismatch")
    if approval.document_id != preflight_evidence.document_id:
        raise QuarantineRegistryTransitionError("document_id binding mismatch")
    if approval.source_hash != preflight_evidence.source_hash:
        raise QuarantineRegistryTransitionError("source_hash binding mismatch")
    if approval.request_id != preflight_evidence.request_id:
        raise QuarantineRegistryTransitionError("request_id binding mismatch")
    if approval.plan_digest != preflight_evidence.plan_digest:
        raise QuarantineRegistryTransitionError("plan_digest binding mismatch")

    expected_target_sf = make_source_file_id(
        document_id=approval.document_id,
        relative_path=approval.target_path,
    )
    if target_source_file_id != expected_target_sf:
        raise QuarantineRegistryTransitionError(
            "target_source_file_id must match deterministic locator id for target_path"
        )

    if target_source_file_id == retained_source_file_id:
        raise QuarantineRegistryTransitionError(
            "target_source_file_id and retained_source_file_id must differ"
        )
    for field_name, value in (
        ("target_source_file_id", target_source_file_id),
        ("retained_source_file_id", retained_source_file_id),
    ):
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise QuarantineRegistryTransitionError(f"{field_name} is required")
        if value != value.strip():
            raise QuarantineRegistryTransitionError(
                f"{field_name} must not contain leading/trailing whitespace"
            )

    if not isinstance(registry_collection, str) or not registry_collection.strip():
        raise QuarantineRegistryTransitionError(
            "registry_collection must be a non-empty string"
        )

    _validate_operation_id(operation_id)
    if created_at is not None:
        _validate_registry_timestamp(created_at)

    try:
        validate_document_id(approval.document_id)
    except IdentityValidationError as exc:
        raise QuarantineRegistryTransitionError(str(exc)) from exc


def _validate_operation_id(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise QuarantineRegistryTransitionError("operation_id is required")
    op = operation_id.strip()
    if "\0" in op or "/" in op or "\\" in op:
        raise QuarantineRegistryTransitionError(
            "operation_id must be a safe opaque identifier"
        )
    return op


def _validate_registry_db_path(registry_db: str) -> None:
    if not isinstance(registry_db, str) or not registry_db.strip():
        raise QuarantineRegistryTransitionError("registry_db must be a non-empty path")
    path = Path(registry_db)
    if not path.is_absolute():
        raise QuarantineRegistryTransitionError("registry_db must be an absolute path")


def _validate_conn_registry_path(
    conn: sqlite3.Connection, expected_registry_db: str
) -> None:
    _validate_registry_db_path(expected_registry_db)
    rows = conn.execute("PRAGMA database_list").fetchall()
    main = next((r for r in rows if r["name"] == "main"), None)
    if main is None or not main["file"]:
        raise QuarantineRegistryTransitionError(
            "connection registry path cannot be verified"
        )
    actual = Path(main["file"]).resolve()
    expected = Path(expected_registry_db).resolve()
    if actual != expected:
        raise QuarantineRegistryTransitionError(
            "preflight_evidence registry_db does not match connection database"
        )


def _validate_retained_locator_active(
    conn: sqlite3.Connection,
    *,
    retained_source_file_id: str,
    document_id: str,
    retained_path: str,
) -> None:
    row = conn.execute(
        "SELECT source_file_id, document_id, relative_path FROM source_files "
        "WHERE source_file_id = ?",
        (retained_source_file_id,),
    ).fetchone()
    if row is None:
        raise QuarantineRegistryTransitionError(
            f"retained_source_file_id {retained_source_file_id!r} is not registered"
        )
    if row["document_id"] != document_id:
        raise QuarantineRegistryTransitionError(
            "retained_source_file_id is not bound to the supplied document_id"
        )
    if row["relative_path"] != retained_path:
        raise QuarantineRegistryTransitionError(
            "retained_source_file_id relative_path does not match approval retained_path"
        )
    state = get_locator_lifecycle_state(conn, source_file_id=retained_source_file_id)
    if state is None:
        raise QuarantineRegistryTransitionError(
            f"retained locator {retained_source_file_id!r} has no lifecycle state projection"
        )
    if state["activity_state"] != LOCATOR_ACTIVE:
        raise QuarantineRegistryTransitionError(
            f"retained locator must remain ACTIVE; got {state['activity_state']!r}"
        )

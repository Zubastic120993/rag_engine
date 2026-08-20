"""Transaction-bound MOVE destination locator provisioning (Phase E1).

Creates one destination source_files row, V4 alias-registration event, and V5
INITIALIZED ACTIVE locator inside the caller's registry_transaction. Never
commits, rolls back, or opens its own connection.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rag_engine.library_state.move_approval import MoveApprovalValidationResult
from rag_engine.metadata_registry.exceptions import RegistryValidationError
from rag_engine.metadata_registry.locator_lifecycle import (
    LOCATOR_ACTIVE,
    LOCATOR_EVENT_INITIALIZED,
    initialize_locator_lifecycle_state,
)
from rag_engine.metadata_registry.migrations import utc_now
from rag_engine.metadata_registry.repository import (
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    _validate_canonical_approval_digest,
    _validate_registry_timestamp,
    append_source_file_event,
    register_source_file,
)
from rag_engine.stable_identity import (
    IdentityValidationError,
    PathNormalizationError,
    normalize_relative_path,
    validate_document_id,
    validate_source_hash,
)

MOVE_DESTINATION_PROVISIONING_SOURCE = "move_destination_provisioning"
MOVE_DESTINATION_PROVISIONING_REASON = "future MOVE destination locator provisioning"


class MoveDestinationProvisioningError(RegistryValidationError):
    """Raised when MOVE destination locator provisioning preconditions fail."""


@dataclass(frozen=True)
class MoveDestinationProvisioningResult:
    """Outcome of provisioning one destination locator inside a transaction."""

    source_file: dict[str, Any]
    v4_event: dict[str, Any]
    v5_state: dict[str, Any]
    v5_lifecycle_event: dict[str, Any]
    idempotent: bool = False


def provision_move_destination_locator(
    conn: sqlite3.Connection,
    *,
    approval: MoveApprovalValidationResult,
    document_id: str,
    source_hash: str,
    destination_relative_path: str,
    registry_collection: str,
    operation_id: str,
    actor: str | None = None,
    created_at: str | None = None,
) -> MoveDestinationProvisioningResult:
    """Provision one destination locator row + V4/V5 evidence for a future MOVE."""
    _require_active_transaction(conn)
    _validate_inputs(
        approval=approval,
        document_id=document_id,
        source_hash=source_hash,
        destination_relative_path=destination_relative_path,
        registry_collection=registry_collection,
        operation_id=operation_id,
        created_at=created_at,
    )

    ts = created_at or utc_now()
    op = _validate_operation_id(operation_id)
    norm_dest = _normalize_destination_path(destination_relative_path)
    _validate_document_binding(conn, document_id=document_id, source_hash=source_hash)
    _validate_destination_available(
        conn,
        document_id=document_id,
        destination_relative_path=norm_dest,
    )

    source_file = register_source_file(
        conn,
        document_id=document_id,
        relative_path=norm_dest,
        source_hash=source_hash,
        collection=registry_collection,
    )
    destination_sf_id = str(source_file["source_file_id"])

    v4_event = append_source_file_event(
        conn,
        source_file_id=destination_sf_id,
        document_id=document_id,
        event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
        operation_id=op,
        approval_digest=approval.approval_digest,
        actor=actor,
        source=MOVE_DESTINATION_PROVISIONING_SOURCE,
        reason=MOVE_DESTINATION_PROVISIONING_REASON,
        created_at=ts,
    )

    init_result = initialize_locator_lifecycle_state(
        conn,
        source_file_id=destination_sf_id,
        document_id=document_id,
        related_v4_event_id=int(v4_event["event_id"]),
        actor=actor,
        source=MOVE_DESTINATION_PROVISIONING_SOURCE,
        reason=MOVE_DESTINATION_PROVISIONING_REASON,
        created_at=ts,
    )
    if init_result.get("idempotent"):
        raise MoveDestinationProvisioningError(
            "destination locator V5 state already exists"
        )
    v5_state = init_result["state"]
    v5_event = init_result["lifecycle_event"]
    if v5_state is None or v5_event is None:
        raise MoveDestinationProvisioningError(
            "destination locator V5 initialization did not produce state/event"
        )
    if v5_state["activity_state"] != LOCATOR_ACTIVE:
        raise MoveDestinationProvisioningError(
            "destination locator must begin ACTIVE"
        )
    if v5_event["event_type"] != LOCATOR_EVENT_INITIALIZED:
        raise MoveDestinationProvisioningError(
            "destination locator must begin with SOURCE_FILE_LOCATOR_INITIALIZED"
        )

    return MoveDestinationProvisioningResult(
        source_file=source_file,
        v4_event=v4_event,
        v5_state=v5_state,
        v5_lifecycle_event=v5_event,
        idempotent=False,
    )


def _require_active_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise MoveDestinationProvisioningError(
            "provision_move_destination_locator requires an active registry_transaction"
        )


def _validate_inputs(
    *,
    approval: MoveApprovalValidationResult,
    document_id: str,
    source_hash: str,
    destination_relative_path: str,
    registry_collection: str,
    operation_id: str,
    created_at: str | None,
) -> None:
    from rag_engine.library_state.move_approval import MoveApprovalValidationResult

    if not isinstance(approval, MoveApprovalValidationResult):
        raise MoveDestinationProvisioningError(
            "approval must be a MoveApprovalValidationResult"
        )
    if approval.document_id != document_id:
        raise MoveDestinationProvisioningError("approval document_id mismatch")
    if approval.source_hash != source_hash:
        raise MoveDestinationProvisioningError("approval source_hash mismatch")
    norm_approval_dest = _normalize_destination_path(approval.destination_path)
    norm_supplied_dest = _normalize_destination_path(destination_relative_path)
    if norm_approval_dest != norm_supplied_dest:
        raise MoveDestinationProvisioningError("approval destination_path mismatch")
    _validate_canonical_approval_digest(approval.approval_digest)
    try:
        validate_document_id(document_id)
        validate_source_hash(source_hash)
    except IdentityValidationError as exc:
        raise MoveDestinationProvisioningError(str(exc)) from exc
    if not isinstance(registry_collection, str) or not registry_collection.strip():
        raise MoveDestinationProvisioningError(
            "registry_collection must be a non-empty string"
        )
    if registry_collection != registry_collection.strip():
        raise MoveDestinationProvisioningError(
            "registry_collection must not contain leading/trailing whitespace"
        )
    _validate_operation_id(operation_id)
    if created_at is not None:
        _validate_registry_timestamp(created_at)


def _validate_operation_id(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise MoveDestinationProvisioningError("operation_id is required")
    op = operation_id.strip()
    if "\0" in op or "/" in op or "\\" in op:
        raise MoveDestinationProvisioningError(
            "operation_id must be a safe opaque identifier"
        )
    return op


def _normalize_destination_path(raw: str) -> str:
    if not isinstance(raw, str):
        raise MoveDestinationProvisioningError(
            "destination_relative_path must be a relative path string"
        )
    if "\0" in raw:
        raise MoveDestinationProvisioningError(
            "destination_relative_path must not contain NUL"
        )
    if not raw.strip():
        raise MoveDestinationProvisioningError(
            "destination_relative_path must be non-empty"
        )
    if raw != raw.strip():
        raise MoveDestinationProvisioningError(
            "destination_relative_path must not contain leading/trailing whitespace"
        )
    if raw.startswith("/") or raw.startswith("\\"):
        raise MoveDestinationProvisioningError(
            "destination_relative_path must not be absolute"
        )
    if ".." in raw.replace("\\", "/").split("/"):
        raise MoveDestinationProvisioningError(
            "destination_relative_path must not contain traversal segments"
        )
    try:
        return normalize_relative_path(raw)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        raise MoveDestinationProvisioningError(
            f"invalid destination_relative_path: {exc}"
        ) from exc


def _validate_document_binding(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    source_hash: str,
) -> None:
    row = conn.execute(
        "SELECT document_id, source_hash FROM document_versions WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        raise MoveDestinationProvisioningError(
            f"document_id {document_id!r} is not registered"
        )
    if row["source_hash"] != source_hash:
        raise MoveDestinationProvisioningError(
            "document source_hash binding mismatch"
        )


def _validate_destination_available(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    destination_relative_path: str,
) -> None:
    same_doc = conn.execute(
        "SELECT source_file_id FROM source_files "
        "WHERE document_id = ? AND relative_path = ?",
        (document_id, destination_relative_path),
    ).fetchone()
    if same_doc is not None:
        raise MoveDestinationProvisioningError(
            "destination relative_path already exists for document_id"
        )

    other_doc = conn.execute(
        "SELECT document_id FROM source_files WHERE relative_path = ?",
        (destination_relative_path,),
    ).fetchone()
    if other_doc is not None and other_doc["document_id"] != document_id:
        raise MoveDestinationProvisioningError(
            "destination relative_path is already registered to another document"
        )

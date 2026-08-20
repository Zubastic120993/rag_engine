"""V5 source-file locator lifecycle - current-state projection + append-only events."""

from __future__ import annotations

import sqlite3
from typing import Any

from rag_engine.metadata_registry.exceptions import (
    LifecycleTransitionError,
    RegistryValidationError,
)
from rag_engine.metadata_registry.migrations import utc_now
from rag_engine.metadata_registry.repository import (
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
    SOURCE_FILE_EVENT_REGISTERED,
    append_source_file_event,
    _row_to_dict,
    _validate_registry_timestamp,
)
from rag_engine.stable_identity import (
    IdentityValidationError,
    validate_document_id,
)

# ---------------------------------------------------------------------------
# Activity states (current-state authority)
# ---------------------------------------------------------------------------

LOCATOR_ACTIVE = "ACTIVE"
LOCATOR_COMPENSATION_PENDING = "COMPENSATION_PENDING"
LOCATOR_INACTIVE = "INACTIVE"
LOCATOR_COMPENSATION_REJECTED = "COMPENSATION_REJECTED"
LOCATOR_COMPENSATION_FAILED = "COMPENSATION_FAILED"

LOCATOR_ACTIVITY_STATES: frozenset[str] = frozenset(
    {
        LOCATOR_ACTIVE,
        LOCATOR_COMPENSATION_PENDING,
        LOCATOR_INACTIVE,
        LOCATOR_COMPENSATION_REJECTED,
        LOCATOR_COMPENSATION_FAILED,
    }
)

# ---------------------------------------------------------------------------
# V5 lifecycle event vocabulary (closed; V4 types remain in source_file_events)
# ---------------------------------------------------------------------------

LOCATOR_EVENT_INITIALIZED = "SOURCE_FILE_LOCATOR_INITIALIZED"
LOCATOR_EVENT_COMPENSATION_COMPLETED = "SOURCE_FILE_COMPENSATION_COMPLETED"
LOCATOR_EVENT_COMPENSATION_REJECTED = "SOURCE_FILE_COMPENSATION_REJECTED"
LOCATOR_EVENT_COMPENSATION_FAILED = "SOURCE_FILE_COMPENSATION_FAILED"
LOCATOR_EVENT_REACTIVATED = "SOURCE_FILE_LOCATOR_REACTIVATED"
LOCATOR_EVENT_MOVED = "SOURCE_FILE_LOCATOR_MOVED"

LOCATOR_LIFECYCLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        LOCATOR_EVENT_INITIALIZED,
        LOCATOR_EVENT_COMPENSATION_COMPLETED,
        LOCATOR_EVENT_COMPENSATION_REJECTED,
        LOCATOR_EVENT_COMPENSATION_FAILED,
        LOCATOR_EVENT_REACTIVATED,
        LOCATOR_EVENT_MOVED,
    }
)

MIGRATION_V5_SOURCE: str = "schema_migration_v4_to_v5"
MIGRATION_V5_REASON: str = "v5_backfill_existing_locator"

_MOVE_NEW_ELIGIBLE_PRIOR: frozenset[str] = frozenset(
    {
        LOCATOR_ACTIVE,
        LOCATOR_INACTIVE,
        LOCATOR_COMPENSATION_REJECTED,
        LOCATOR_COMPENSATION_FAILED,
    }
)

MOVE_COMPENSATION_RESTORABLE_NEW_PRIOR: frozenset[str] = frozenset(
    _MOVE_NEW_ELIGIBLE_PRIOR
)

_RECOVERY_EVENT_BY_TARGET: dict[str, str] = {
    LOCATOR_ACTIVE: LOCATOR_EVENT_REACTIVATED,
    LOCATOR_INACTIVE: LOCATOR_EVENT_COMPENSATION_COMPLETED,
    LOCATOR_COMPENSATION_REJECTED: LOCATOR_EVENT_COMPENSATION_REJECTED,
    LOCATOR_COMPENSATION_FAILED: LOCATOR_EVENT_COMPENSATION_FAILED,
}


def _validate_activity_state(state: str) -> str:
    if state not in LOCATOR_ACTIVITY_STATES:
        raise RegistryValidationError(
            f"activity_state must be one of {sorted(LOCATOR_ACTIVITY_STATES)}; "
            f"got {state!r}"
        )
    return state


def _validate_event_type(event_type: str) -> str:
    if event_type not in LOCATOR_LIFECYCLE_EVENT_TYPES:
        raise RegistryValidationError(
            f"event_type must be one of {sorted(LOCATOR_LIFECYCLE_EVENT_TYPES)}"
        )
    return event_type


def _load_locator_binding(
    conn: sqlite3.Connection, *, source_file_id: str
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT source_file_id, document_id FROM source_files WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchone()
    if row is None:
        raise RegistryValidationError(
            f"source_file_id {source_file_id!r} is not registered"
        )
    return _row_to_dict(row)  # type: ignore[return-value]


def _append_locator_lifecycle_event(
    conn: sqlite3.Connection,
    *,
    source_file_id: str,
    document_id: str,
    event_type: str,
    previous_state: str | None,
    new_state: str,
    related_v4_event_id: int | None = None,
    related_event_id: int | None = None,
    operation_id: str | None = None,
    approval_digest: str | None = None,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    _validate_event_type(event_type)
    _validate_activity_state(new_state)
    if previous_state is not None:
        _validate_activity_state(previous_state)
    ts = created_at or utc_now()
    _validate_registry_timestamp(ts)
    try:
        cur = conn.execute(
            "INSERT INTO source_file_locator_lifecycle_events ("
            "source_file_id, document_id, event_type, previous_state, new_state, "
            "related_v4_event_id, related_event_id, operation_id, approval_digest, "
            "reason, actor, source, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_file_id,
                document_id,
                event_type,
                previous_state,
                new_state,
                related_v4_event_id,
                related_event_id,
                operation_id,
                approval_digest,
                reason,
                actor,
                source,
                ts,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryValidationError(
            f"source_file_locator_lifecycle_events insert failed: {exc}"
        ) from exc
    event_id = int(cur.lastrowid)
    row = conn.execute(
        "SELECT * FROM source_file_locator_lifecycle_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def _upsert_locator_state_projection(
    conn: sqlite3.Connection,
    *,
    source_file_id: str,
    activity_state: str,
    state_updated_at: str,
    last_lifecycle_event_id: int | None,
    last_v4_event_id: int | None,
) -> dict[str, Any]:
    _validate_activity_state(activity_state)
    _validate_registry_timestamp(state_updated_at)
    conn.execute(
        "INSERT INTO source_file_locator_state ("
        "source_file_id, activity_state, state_updated_at, "
        "last_lifecycle_event_id, last_v4_event_id"
        ") VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source_file_id) DO UPDATE SET "
        "activity_state = excluded.activity_state, "
        "state_updated_at = excluded.state_updated_at, "
        "last_lifecycle_event_id = excluded.last_lifecycle_event_id, "
        "last_v4_event_id = excluded.last_v4_event_id",
        (
            source_file_id,
            activity_state,
            state_updated_at,
            last_lifecycle_event_id,
            last_v4_event_id,
        ),
    )
    row = conn.execute(
        "SELECT * FROM source_file_locator_state WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def _validate_move_v4_event_reference(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    source_file_id: str,
    document_id: str,
    role: str,
) -> None:
    """Fail closed: V4 event must exist and match the supplied locator/document."""
    row = conn.execute(
        "SELECT event_id, source_file_id, document_id FROM source_file_events "
        "WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise RegistryValidationError(
            f"{role}_related_v4_event_id {event_id} does not exist"
        )
    if row["source_file_id"] != source_file_id:
        raise RegistryValidationError(
            f"{role}_related_v4_event_id {event_id} is not bound to "
            f"{role} locator {source_file_id!r}"
        )
    if row["document_id"] != document_id:
        raise RegistryValidationError(
            f"{role}_related_v4_event_id {event_id} is not bound to "
            f"document_id {document_id!r}"
        )


def _resolve_latest_v4_registration_event_id(
    conn: sqlite3.Connection, *, source_file_id: str
) -> int | None:
    row = conn.execute(
        "SELECT event_id FROM source_file_events "
        "WHERE source_file_id = ? AND event_type IN (?, ?) "
        "ORDER BY event_id DESC LIMIT 1",
        (
            source_file_id,
            SOURCE_FILE_EVENT_REGISTERED,
            SOURCE_FILE_EVENT_ALIAS_REGISTERED,
        ),
    ).fetchone()
    return int(row["event_id"]) if row is not None else None


def initialize_locator_lifecycle_state(
    conn: sqlite3.Connection,
    *,
    source_file_id: str,
    document_id: str,
    related_v4_event_id: int | None = None,
    actor: str | None = None,
    source: str,
    reason: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Migration/backfill support: one ACTIVE projection + INITIALIZED event per locator."""
    try:
        validate_document_id(document_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc

    binding = _load_locator_binding(conn, source_file_id=source_file_id)
    if binding["document_id"] != document_id:
        raise RegistryValidationError(
            f"document_id {document_id!r} does not match source_file binding"
        )

    existing = conn.execute(
        "SELECT source_file_id FROM source_file_locator_state WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchone()
    if existing is not None:
        state = get_locator_lifecycle_state(conn, source_file_id=source_file_id)
        return {
            "state": state,
            "lifecycle_event": None,
            "idempotent": True,
        }

    v4_id = related_v4_event_id
    if v4_id is None:
        v4_id = _resolve_latest_v4_registration_event_id(
            conn, source_file_id=source_file_id
        )

    ts = created_at or utc_now()
    event = _append_locator_lifecycle_event(
        conn,
        source_file_id=source_file_id,
        document_id=document_id,
        event_type=LOCATOR_EVENT_INITIALIZED,
        previous_state=None,
        new_state=LOCATOR_ACTIVE,
        related_v4_event_id=v4_id,
        actor=actor,
        source=source,
        reason=reason,
        created_at=ts,
    )
    state = _upsert_locator_state_projection(
        conn,
        source_file_id=source_file_id,
        activity_state=LOCATOR_ACTIVE,
        state_updated_at=ts,
        last_lifecycle_event_id=int(event["event_id"]),
        last_v4_event_id=v4_id,
    )
    return {
        "state": state,
        "lifecycle_event": event,
        "idempotent": False,
    }


def get_locator_lifecycle_state(
    conn: sqlite3.Connection, *, source_file_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM source_file_locator_state WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchone()
    return _row_to_dict(row)


def list_locator_lifecycle_states_for_document(
    conn: sqlite3.Connection, *, document_id: str
) -> list[dict[str, Any]]:
    try:
        validate_document_id(document_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc
    rows = conn.execute(
        "SELECT s.* FROM source_file_locator_state s "
        "INNER JOIN source_files f ON f.source_file_id = s.source_file_id "
        "WHERE f.document_id = ? ORDER BY s.source_file_id",
        (document_id,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]  # type: ignore[misc]


def record_locator_move_transition(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    old_source_file_id: str,
    new_source_file_id: str,
    operation_id: str,
    old_related_v4_event_id: int | None = None,
    new_related_v4_event_id: int | None = None,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Explicit locator MOVE: old ACTIVE→INACTIVE, new eligible→ACTIVE; pair via operation_id.

    Optional V4 linkage is locator-specific: ``old_related_v4_event_id`` attaches only
    to the old-locator MOVE event; ``new_related_v4_event_id`` only to the new-locator event.
    """
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise RegistryValidationError(
            "operation_id is required for SOURCE_FILE_LOCATOR_MOVED transitions"
        )
    if old_source_file_id == new_source_file_id:
        raise RegistryValidationError(
            "old_source_file_id and new_source_file_id must differ for MOVE"
        )
    try:
        validate_document_id(document_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc

    old_binding = _load_locator_binding(conn, source_file_id=old_source_file_id)
    new_binding = _load_locator_binding(conn, source_file_id=new_source_file_id)
    if old_binding["document_id"] != document_id:
        raise RegistryValidationError(
            "old_source_file_id is not bound to the supplied document_id"
        )
    if new_binding["document_id"] != document_id:
        raise RegistryValidationError(
            "new_source_file_id is not bound to the supplied document_id"
        )

    old_state_row = get_locator_lifecycle_state(conn, source_file_id=old_source_file_id)
    if old_state_row is None:
        raise RegistryValidationError(
            f"old locator {old_source_file_id!r} has no lifecycle state projection"
        )
    if old_state_row["activity_state"] != LOCATOR_ACTIVE:
        raise LifecycleTransitionError(
            f"MOVE requires old locator activity_state ACTIVE; "
            f"got {old_state_row['activity_state']!r}"
        )

    new_state_row = get_locator_lifecycle_state(conn, source_file_id=new_source_file_id)
    if new_state_row is None:
        raise RegistryValidationError(
            f"new locator {new_source_file_id!r} has no lifecycle state projection"
        )
    new_prior = new_state_row["activity_state"]
    if new_prior not in _MOVE_NEW_ELIGIBLE_PRIOR:
        raise LifecycleTransitionError(
            f"new locator activity_state {new_prior!r} is not eligible for MOVE to ACTIVE"
        )

    if old_related_v4_event_id is not None:
        _validate_move_v4_event_reference(
            conn,
            event_id=old_related_v4_event_id,
            source_file_id=old_source_file_id,
            document_id=document_id,
            role="old",
        )
    if new_related_v4_event_id is not None:
        _validate_move_v4_event_reference(
            conn,
            event_id=new_related_v4_event_id,
            source_file_id=new_source_file_id,
            document_id=document_id,
            role="new",
        )

    ts = created_at or utc_now()
    op = operation_id.strip()
    src = source or "record_locator_move_transition"

    new_event = _append_locator_lifecycle_event(
        conn,
        source_file_id=new_source_file_id,
        document_id=document_id,
        event_type=LOCATOR_EVENT_MOVED,
        previous_state=new_prior,
        new_state=LOCATOR_ACTIVE,
        related_v4_event_id=new_related_v4_event_id,
        operation_id=op,
        reason=reason,
        actor=actor,
        source=src,
        created_at=ts,
    )
    old_event = _append_locator_lifecycle_event(
        conn,
        source_file_id=old_source_file_id,
        document_id=document_id,
        event_type=LOCATOR_EVENT_MOVED,
        previous_state=LOCATOR_ACTIVE,
        new_state=LOCATOR_INACTIVE,
        related_v4_event_id=old_related_v4_event_id,
        related_event_id=int(new_event["event_id"]),
        operation_id=op,
        reason=reason,
        actor=actor,
        source=src,
        created_at=ts,
    )

    new_state = _upsert_locator_state_projection(
        conn,
        source_file_id=new_source_file_id,
        activity_state=LOCATOR_ACTIVE,
        state_updated_at=ts,
        last_lifecycle_event_id=int(new_event["event_id"]),
        last_v4_event_id=new_state_row.get("last_v4_event_id"),
    )
    old_state = _upsert_locator_state_projection(
        conn,
        source_file_id=old_source_file_id,
        activity_state=LOCATOR_INACTIVE,
        state_updated_at=ts,
        last_lifecycle_event_id=int(old_event["event_id"]),
        last_v4_event_id=old_state_row.get("last_v4_event_id"),
    )

    return {
        "document_id": document_id,
        "operation_id": op,
        "old_source_file_id": old_source_file_id,
        "new_source_file_id": new_source_file_id,
        "affected_source_file_ids": [old_source_file_id, new_source_file_id],
        "old_state": old_state,
        "new_state": new_state,
        "old_event": old_event,
        "new_event": new_event,
    }


def validate_move_compensation_restorable(*, new_prior_activity_state: str) -> None:
    """Fail closed when a failed MOVE cannot restore the captured new-locator state."""
    _validate_activity_state(new_prior_activity_state)
    if new_prior_activity_state not in MOVE_COMPENSATION_RESTORABLE_NEW_PRIOR:
        raise RegistryValidationError(
            f"new locator activity_state {new_prior_activity_state!r} cannot be "
            "restored after a failed MOVE using existing V5 vocabulary"
        )


def restore_locator_move_states_after_failure(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    old_source_file_id: str,
    new_source_file_id: str,
    old_target_activity_state: str,
    new_target_activity_state: str,
    failed_move_operation_id: str,
    operation_id: str,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Restore both locator projections after a failed explicit MOVE.

    Appends auditable recovery events and updates projections only. Does not
    mutate source_files, tracker, Chroma, or filesystem state. Preserves the
    forward MOVE event history from ``failed_move_operation_id``.
    """
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise RegistryValidationError(
            "operation_id is required for move-failure recovery"
        )
    if not isinstance(failed_move_operation_id, str) or not failed_move_operation_id.strip():
        raise RegistryValidationError(
            "failed_move_operation_id is required for move-failure recovery"
        )
    if old_source_file_id == new_source_file_id:
        raise RegistryValidationError(
            "old_source_file_id and new_source_file_id must differ for recovery"
        )
    try:
        validate_document_id(document_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc

    _validate_activity_state(old_target_activity_state)
    _validate_activity_state(new_target_activity_state)
    validate_move_compensation_restorable(new_prior_activity_state=new_target_activity_state)
    if old_target_activity_state != LOCATOR_ACTIVE:
        raise RegistryValidationError(
            "move-failure recovery requires old_target_activity_state ACTIVE"
        )

    old_binding = _load_locator_binding(conn, source_file_id=old_source_file_id)
    new_binding = _load_locator_binding(conn, source_file_id=new_source_file_id)
    if old_binding["document_id"] != document_id:
        raise RegistryValidationError(
            "old_source_file_id is not bound to the supplied document_id"
        )
    if new_binding["document_id"] != document_id:
        raise RegistryValidationError(
            "new_source_file_id is not bound to the supplied document_id"
        )

    old_state_row = get_locator_lifecycle_state(conn, source_file_id=old_source_file_id)
    new_state_row = get_locator_lifecycle_state(conn, source_file_id=new_source_file_id)
    if old_state_row is None:
        raise RegistryValidationError(
            f"old locator {old_source_file_id!r} has no lifecycle state projection"
        )
    if new_state_row is None:
        raise RegistryValidationError(
            f"new locator {new_source_file_id!r} has no lifecycle state projection"
        )
    if old_state_row["activity_state"] != LOCATOR_INACTIVE:
        raise LifecycleTransitionError(
            "move-failure recovery requires old locator post-MOVE state INACTIVE; "
            f"got {old_state_row['activity_state']!r}"
        )
    if new_state_row["activity_state"] != LOCATOR_ACTIVE:
        raise LifecycleTransitionError(
            "move-failure recovery requires new locator post-MOVE state ACTIVE; "
            f"got {new_state_row['activity_state']!r}"
        )

    ts = created_at or utc_now()
    op = operation_id.strip()
    src = source or "restore_locator_move_states_after_failure"
    recovery_reason = reason or (
        f"restore locator states after failed MOVE operation_id={failed_move_operation_id.strip()}"
    )

    old_event: dict[str, Any] | None = None
    new_event: dict[str, Any] | None = None

    if old_target_activity_state != LOCATOR_INACTIVE:
        old_event = _append_locator_lifecycle_event(
            conn,
            source_file_id=old_source_file_id,
            document_id=document_id,
            event_type=LOCATOR_EVENT_REACTIVATED,
            previous_state=LOCATOR_INACTIVE,
            new_state=LOCATOR_ACTIVE,
            operation_id=op,
            reason=recovery_reason,
            actor=actor,
            source=src,
            created_at=ts,
        )
        old_state = _upsert_locator_state_projection(
            conn,
            source_file_id=old_source_file_id,
            activity_state=LOCATOR_ACTIVE,
            state_updated_at=ts,
            last_lifecycle_event_id=int(old_event["event_id"]),
            last_v4_event_id=old_state_row.get("last_v4_event_id"),
        )
    else:
        old_state = old_state_row

    if new_target_activity_state != LOCATOR_ACTIVE:
        new_event_type = _RECOVERY_EVENT_BY_TARGET[new_target_activity_state]
        new_event = _append_locator_lifecycle_event(
            conn,
            source_file_id=new_source_file_id,
            document_id=document_id,
            event_type=new_event_type,
            previous_state=LOCATOR_ACTIVE,
            new_state=new_target_activity_state,
            operation_id=op,
            reason=recovery_reason,
            actor=actor,
            source=src,
            created_at=ts,
        )
        new_state = _upsert_locator_state_projection(
            conn,
            source_file_id=new_source_file_id,
            activity_state=new_target_activity_state,
            state_updated_at=ts,
            last_lifecycle_event_id=int(new_event["event_id"]),
            last_v4_event_id=new_state_row.get("last_v4_event_id"),
        )
    else:
        new_state = new_state_row

    if old_state["activity_state"] != old_target_activity_state:
        raise LifecycleTransitionError(
            "old locator recovery did not reach target activity_state"
        )
    if new_state["activity_state"] != new_target_activity_state:
        raise LifecycleTransitionError(
            "new locator recovery did not reach target activity_state"
        )

    return {
        "document_id": document_id,
        "operation_id": op,
        "failed_move_operation_id": failed_move_operation_id.strip(),
        "old_source_file_id": old_source_file_id,
        "new_source_file_id": new_source_file_id,
        "old_target_activity_state": old_target_activity_state,
        "new_target_activity_state": new_target_activity_state,
        "old_state": old_state,
        "new_state": new_state,
        "old_event": old_event,
        "new_event": new_event,
    }


_TERMINAL_COMPENSATION_OUTCOMES: dict[str, tuple[str, str]] = {
    "COMPLETED": (LOCATOR_EVENT_COMPENSATION_COMPLETED, LOCATOR_INACTIVE),
    "REJECTED": (LOCATOR_EVENT_COMPENSATION_REJECTED, LOCATOR_COMPENSATION_REJECTED),
    "FAILED": (LOCATOR_EVENT_COMPENSATION_FAILED, LOCATOR_COMPENSATION_FAILED),
}


def _resolve_alias_registration_v4_event_id(
    conn: sqlite3.Connection, *, source_file_id: str
) -> int:
    row = conn.execute(
        "SELECT event_id FROM source_file_events "
        "WHERE source_file_id = ? AND event_type = ? "
        "ORDER BY event_id DESC LIMIT 1",
        (source_file_id, SOURCE_FILE_EVENT_ALIAS_REGISTERED),
    ).fetchone()
    if row is None:
        raise RegistryValidationError(
            f"locator {source_file_id!r} has no SOURCE_FILE_ALIAS_REGISTERED event"
        )
    return int(row["event_id"])


resolve_alias_registration_v4_event_id = _resolve_alias_registration_v4_event_id


def _assert_no_open_compensation_request(
    conn: sqlite3.Connection, *, source_file_id: str
) -> None:
    orphan = conn.execute(
        "SELECT e.event_id FROM source_file_events e "
        "WHERE e.source_file_id = ? AND e.event_type = ? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM source_file_locator_lifecycle_events le "
        "  WHERE le.related_v4_event_id = e.event_id "
        "  AND le.event_type IN (?, ?, ?)"
        ")",
        (
            source_file_id,
            SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
            LOCATOR_EVENT_COMPENSATION_COMPLETED,
            LOCATOR_EVENT_COMPENSATION_REJECTED,
            LOCATOR_EVENT_COMPENSATION_FAILED,
        ),
    ).fetchall()
    if orphan:
        state = get_locator_lifecycle_state(conn, source_file_id=source_file_id)
        if state is None or state["activity_state"] != LOCATOR_COMPENSATION_PENDING:
            raise RegistryValidationError(
                "open SOURCE_FILE_COMPENSATION_REQUESTED without terminal V5 outcome"
            )


def record_compensation_request(
    conn: sqlite3.Connection,
    *,
    source_file_id: str,
    document_id: str,
    registration_v4_event_id: int,
    compensation_approval_digest: str,
    operation_id: str | None = None,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Append V4 compensation request and set projection to COMPENSATION_PENDING."""
    try:
        validate_document_id(document_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc

    binding = _load_locator_binding(conn, source_file_id=source_file_id)
    if binding["document_id"] != document_id:
        raise RegistryValidationError(
            "source_file_id is not bound to the supplied document_id"
        )

    state_row = get_locator_lifecycle_state(conn, source_file_id=source_file_id)
    if state_row is None:
        raise RegistryValidationError(
            f"locator {source_file_id!r} has no lifecycle state projection"
        )
    if state_row["activity_state"] != LOCATOR_ACTIVE:
        raise LifecycleTransitionError(
            f"compensation request requires activity_state ACTIVE; "
            f"got {state_row['activity_state']!r}"
        )

    _validate_move_v4_event_reference(
        conn,
        event_id=registration_v4_event_id,
        source_file_id=source_file_id,
        document_id=document_id,
        role="registration",
    )
    reg_row = conn.execute(
        "SELECT event_type FROM source_file_events WHERE event_id = ?",
        (registration_v4_event_id,),
    ).fetchone()
    if reg_row is None or reg_row["event_type"] != SOURCE_FILE_EVENT_ALIAS_REGISTERED:
        raise RegistryValidationError(
            "registration_v4_event_id must reference SOURCE_FILE_ALIAS_REGISTERED"
        )

    _assert_no_open_compensation_request(conn, source_file_id=source_file_id)

    ts = created_at or utc_now()
    v4_event = append_source_file_event(
        conn,
        source_file_id=source_file_id,
        document_id=document_id,
        event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
        operation_id=operation_id.strip() if operation_id else None,
        approval_digest=compensation_approval_digest,
        related_event_id=registration_v4_event_id,
        reason=reason,
        actor=actor,
        source=source or "record_compensation_request",
        created_at=ts,
    )
    state = _upsert_locator_state_projection(
        conn,
        source_file_id=source_file_id,
        activity_state=LOCATOR_COMPENSATION_PENDING,
        state_updated_at=ts,
        last_lifecycle_event_id=state_row.get("last_lifecycle_event_id"),
        last_v4_event_id=int(v4_event["event_id"]),
    )
    return {
        "source_file_id": source_file_id,
        "document_id": document_id,
        "activity_state": LOCATOR_COMPENSATION_PENDING,
        "v4_event": v4_event,
        "lifecycle_event": None,
        "state": state,
    }


def record_terminal_compensation_outcome(
    conn: sqlite3.Connection,
    *,
    source_file_id: str,
    document_id: str,
    compensation_request_v4_event_id: int,
    outcome: str,
    operation_id: str | None = None,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Append V5 terminal compensation lifecycle event and update projection."""
    if outcome not in _TERMINAL_COMPENSATION_OUTCOMES:
        raise RegistryValidationError(
            "outcome must be one of COMPLETED, REJECTED, or FAILED"
        )
    event_type, terminal_state = _TERMINAL_COMPENSATION_OUTCOMES[outcome]

    try:
        validate_document_id(document_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc

    binding = _load_locator_binding(conn, source_file_id=source_file_id)
    if binding["document_id"] != document_id:
        raise RegistryValidationError(
            "source_file_id is not bound to the supplied document_id"
        )

    request_row = conn.execute(
        "SELECT event_id, source_file_id, document_id, event_type FROM source_file_events "
        "WHERE event_id = ?",
        (compensation_request_v4_event_id,),
    ).fetchone()
    if request_row is None:
        raise RegistryValidationError(
            f"compensation_request_v4_event_id {compensation_request_v4_event_id} "
            "does not exist"
        )
    if request_row["source_file_id"] != source_file_id:
        raise RegistryValidationError(
            "compensation_request_v4_event_id is not bound to source_file_id"
        )
    if request_row["document_id"] != document_id:
        raise RegistryValidationError(
            "compensation_request_v4_event_id is not bound to document_id"
        )
    if request_row["event_type"] != SOURCE_FILE_EVENT_COMPENSATION_REQUESTED:
        raise RegistryValidationError(
            "compensation_request_v4_event_id must reference "
            "SOURCE_FILE_COMPENSATION_REQUESTED"
        )

    state_row = get_locator_lifecycle_state(conn, source_file_id=source_file_id)
    if state_row is None:
        raise RegistryValidationError(
            f"locator {source_file_id!r} has no lifecycle state projection"
        )
    if state_row["activity_state"] != LOCATOR_COMPENSATION_PENDING:
        raise LifecycleTransitionError(
            f"terminal compensation outcome requires COMPENSATION_PENDING; "
            f"got {state_row['activity_state']!r}"
        )
    if int(state_row.get("last_v4_event_id") or 0) != compensation_request_v4_event_id:
        raise RegistryValidationError(
            "state last_v4_event_id must match compensation_request_v4_event_id"
        )

    prior_terminal = conn.execute(
        "SELECT event_id FROM source_file_locator_lifecycle_events "
        "WHERE related_v4_event_id = ? AND event_type IN (?, ?, ?)",
        (
            compensation_request_v4_event_id,
            LOCATOR_EVENT_COMPENSATION_COMPLETED,
            LOCATOR_EVENT_COMPENSATION_REJECTED,
            LOCATOR_EVENT_COMPENSATION_FAILED,
        ),
    ).fetchone()
    if prior_terminal is not None:
        raise RegistryValidationError(
            "compensation request already has a terminal V5 outcome"
        )

    ts = created_at or utc_now()
    lifecycle_event = _append_locator_lifecycle_event(
        conn,
        source_file_id=source_file_id,
        document_id=document_id,
        event_type=event_type,
        previous_state=LOCATOR_COMPENSATION_PENDING,
        new_state=terminal_state,
        related_v4_event_id=compensation_request_v4_event_id,
        operation_id=operation_id.strip() if operation_id else None,
        reason=reason,
        actor=actor,
        source=source or "record_terminal_compensation_outcome",
        created_at=ts,
    )
    state = _upsert_locator_state_projection(
        conn,
        source_file_id=source_file_id,
        activity_state=terminal_state,
        state_updated_at=ts,
        last_lifecycle_event_id=int(lifecycle_event["event_id"]),
        last_v4_event_id=compensation_request_v4_event_id,
    )
    return {
        "source_file_id": source_file_id,
        "document_id": document_id,
        "outcome": outcome,
        "activity_state": terminal_state,
        "v4_event_id": compensation_request_v4_event_id,
        "lifecycle_event": lifecycle_event,
        "state": state,
    }


def restore_locator_quarantine_state_after_failure(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    target_source_file_id: str,
    target_activity_state: str,
    failed_operation_id: str,
    operation_id: str,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Restore target locator projection after a failed quarantine-delete attempt."""
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise RegistryValidationError(
            "operation_id is required for quarantine-failure recovery"
        )
    if not isinstance(failed_operation_id, str) or not failed_operation_id.strip():
        raise RegistryValidationError(
            "failed_operation_id is required for quarantine-failure recovery"
        )
    try:
        validate_document_id(document_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc

    _validate_activity_state(target_activity_state)
    if target_activity_state != LOCATOR_ACTIVE:
        raise RegistryValidationError(
            "quarantine-failure recovery requires target_activity_state ACTIVE"
        )

    binding = _load_locator_binding(conn, source_file_id=target_source_file_id)
    if binding["document_id"] != document_id:
        raise RegistryValidationError(
            "target_source_file_id is not bound to the supplied document_id"
        )

    state_row = get_locator_lifecycle_state(conn, source_file_id=target_source_file_id)
    if state_row is None:
        raise RegistryValidationError(
            f"target locator {target_source_file_id!r} has no lifecycle state projection"
        )

    current = state_row["activity_state"]
    ts = created_at or utc_now()
    op = operation_id.strip()
    src = source or "restore_locator_quarantine_state_after_failure"
    recovery_reason = reason or (
        "restore target locator after failed quarantine-delete "
        f"operation_id={failed_operation_id.strip()}"
    )

    lifecycle_event: dict[str, Any] | None = None
    if current == LOCATOR_INACTIVE:
        lifecycle_event = _append_locator_lifecycle_event(
            conn,
            source_file_id=target_source_file_id,
            document_id=document_id,
            event_type=LOCATOR_EVENT_REACTIVATED,
            previous_state=LOCATOR_INACTIVE,
            new_state=LOCATOR_ACTIVE,
            operation_id=op,
            reason=recovery_reason,
            actor=actor,
            source=src,
            created_at=ts,
        )
        state = _upsert_locator_state_projection(
            conn,
            source_file_id=target_source_file_id,
            activity_state=LOCATOR_ACTIVE,
            state_updated_at=ts,
            last_lifecycle_event_id=int(lifecycle_event["event_id"]),
            last_v4_event_id=state_row.get("last_v4_event_id"),
        )
    elif current == LOCATOR_COMPENSATION_PENDING:
        raise LifecycleTransitionError(
            "quarantine-failure recovery cannot restore COMPENSATION_PENDING; "
            "registry transaction should have rolled back"
        )
    elif current == LOCATOR_ACTIVE:
        state = state_row
    else:
        raise LifecycleTransitionError(
            f"target locator activity_state {current!r} is not restorable to ACTIVE"
        )

    if state["activity_state"] != LOCATOR_ACTIVE:
        raise LifecycleTransitionError(
            "target locator recovery did not reach ACTIVE activity_state"
        )

    return {
        "document_id": document_id,
        "operation_id": op,
        "failed_operation_id": failed_operation_id.strip(),
        "target_source_file_id": target_source_file_id,
        "target_activity_state": target_activity_state,
        "state": state,
        "lifecycle_event": lifecycle_event,
    }


def verify_locator_state_event_consistency(
    conn: sqlite3.Connection, *, source_file_id: str
) -> dict[str, Any]:
    """Fail-closed read-back verifier for projection/event/V4/MOVE linkage."""
    discrepancies: list[str] = []
    state = get_locator_lifecycle_state(conn, source_file_id=source_file_id)
    if state is None:
        return {
            "consistent": False,
            "activity_state": None,
            "discrepancies": ["missing lifecycle state projection"],
            "requires_manual_review": True,
        }

    activity_state = state["activity_state"]
    if activity_state not in LOCATOR_ACTIVITY_STATES:
        discrepancies.append(f"invalid activity_state {activity_state!r}")

    binding = conn.execute(
        "SELECT document_id FROM source_files WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchone()
    if binding is None:
        discrepancies.append("source_files row missing for state projection")
    else:
        dup = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_state "
            "WHERE source_file_id = ?",
            (source_file_id,),
        ).fetchone()["c"]
        if int(dup) != 1:
            discrepancies.append("multiple state rows for one locator")

    last_lifecycle_id = state.get("last_lifecycle_event_id")
    last_event: dict[str, Any] | None = None
    if last_lifecycle_id is None:
        discrepancies.append("last_lifecycle_event_id is NULL")
    else:
        row = conn.execute(
            "SELECT * FROM source_file_locator_lifecycle_events WHERE event_id = ?",
            (last_lifecycle_id,),
        ).fetchone()
        if row is None:
            discrepancies.append(
                f"last_lifecycle_event_id {last_lifecycle_id} does not exist"
            )
        else:
            last_event = _row_to_dict(row)
            if last_event["source_file_id"] != source_file_id:
                discrepancies.append("last lifecycle event source_file_id mismatch")
            if last_event["new_state"] != activity_state and activity_state != LOCATOR_COMPENSATION_PENDING:
                discrepancies.append(
                    "activity_state disagrees with last lifecycle event new_state"
                )

    last_v4_id = state.get("last_v4_event_id")
    if last_v4_id is not None:
        v4 = conn.execute(
            "SELECT event_id, source_file_id, event_type FROM source_file_events "
            "WHERE event_id = ?",
            (last_v4_id,),
        ).fetchone()
        if v4 is None:
            discrepancies.append(f"last_v4_event_id {last_v4_id} does not exist")
        elif v4["source_file_id"] != source_file_id:
            discrepancies.append("last_v4_event_id source_file_id mismatch")

    if activity_state == LOCATOR_COMPENSATION_PENDING:
        if last_v4_id is None:
            discrepancies.append("COMPENSATION_PENDING requires last_v4_event_id")
        else:
            v4 = conn.execute(
                "SELECT event_type FROM source_file_events WHERE event_id = ?",
                (last_v4_id,),
            ).fetchone()
            if v4 is None or v4["event_type"] != SOURCE_FILE_EVENT_COMPENSATION_REQUESTED:
                discrepancies.append(
                    "COMPENSATION_PENDING requires last_v4_event_id to reference "
                    "SOURCE_FILE_COMPENSATION_REQUESTED"
                )
        if last_event is not None and last_event["new_state"] == LOCATOR_COMPENSATION_PENDING:
            discrepancies.append(
                "COMPENSATION_PENDING must not advance last_lifecycle_event_id to pending"
            )

    # Orphan V4 compensation request: request exists but state is not pending and no terminal outcome
    orphan = conn.execute(
        "SELECT e.event_id FROM source_file_events e "
        "WHERE e.source_file_id = ? AND e.event_type = ? "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM source_file_locator_lifecycle_events le "
        "  WHERE le.related_v4_event_id = e.event_id "
        "  AND le.event_type IN (?, ?, ?)"
        ")",
        (
            source_file_id,
            SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
            LOCATOR_EVENT_COMPENSATION_COMPLETED,
            LOCATOR_EVENT_COMPENSATION_REJECTED,
            LOCATOR_EVENT_COMPENSATION_FAILED,
        ),
    ).fetchall()
    if orphan and activity_state != LOCATOR_COMPENSATION_PENDING:
        discrepancies.append(
            "orphan V4 SOURCE_FILE_COMPENSATION_REQUESTED without terminal V5 outcome"
        )

    # MOVE linkage checks for latest MOVED event on this locator
    if last_event is not None and last_event["event_type"] == LOCATOR_EVENT_MOVED:
        op = last_event.get("operation_id")
        if not op:
            discrepancies.append("SOURCE_FILE_LOCATOR_MOVED missing operation_id")
        else:
            peers = conn.execute(
                "SELECT event_id, source_file_id, previous_state, new_state, "
                "related_event_id FROM source_file_locator_lifecycle_events "
                "WHERE operation_id = ? AND event_type = ?",
                (op, LOCATOR_EVENT_MOVED),
            ).fetchall()
            if len(peers) != 2:
                discrepancies.append(
                    f"MOVE operation_id {op!r} expected exactly 2 events, got {len(peers)}"
                )
            else:
                old_ev = next((p for p in peers if p["new_state"] == LOCATOR_INACTIVE), None)
                new_ev = next((p for p in peers if p["new_state"] == LOCATOR_ACTIVE), None)
                if old_ev is None or new_ev is None:
                    discrepancies.append("MOVE pair missing INACTIVE/ACTIVE outcome events")
                elif old_ev["related_event_id"] != new_ev["event_id"]:
                    discrepancies.append(
                        "MOVE old event related_event_id does not reference new event"
                    )

    requires_manual_review = bool(discrepancies)
    return {
        "consistent": not discrepancies,
        "activity_state": activity_state,
        "discrepancies": discrepancies,
        "requires_manual_review": requires_manual_review,
    }

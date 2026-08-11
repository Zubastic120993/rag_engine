"""Phase 5 document revision / replacement lifecycle.

Operates at subject_id → document_id only.
Does not require stable chunk_id or Chroma UUID rewrite.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from rag_engine.metadata_registry.exceptions import (
    LifecycleError,
    LifecycleTransitionError,
    RegistryConflictError,
    RegistryValidationError,
)
from rag_engine.metadata_registry.migrations import utc_now
from rag_engine.metadata_registry.repository import _row_to_dict, registry_transaction
from rag_engine.stable_identity import (
    IdentityValidationError,
    validate_document_id,
    validate_subject_id,
)

# ---------------------------------------------------------------------------
# Controlled vocabulary (roadmap states)
# ---------------------------------------------------------------------------

LIFECYCLE_ACTIVE = "ACTIVE"
LIFECYCLE_SUPERSEDED = "SUPERSEDED"
LIFECYCLE_ARCHIVED = "ARCHIVED"
LIFECYCLE_REPLACED = "REPLACED"
LIFECYCLE_WITHDRAWN = "WITHDRAWN"
LIFECYCLE_DUPLICATE = "DUPLICATE"

LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        LIFECYCLE_ACTIVE,
        LIFECYCLE_SUPERSEDED,
        LIFECYCLE_ARCHIVED,
        LIFECYCLE_REPLACED,
        LIFECYCLE_WITHDRAWN,
        LIFECYCLE_DUPLICATE,
    }
)

DEFAULT_LIFECYCLE_STATUS = LIFECYCLE_ACTIVE

# Relation edges (source → target)
RELATION_SUPERSEDES = "supersedes"  # source (new) supersedes target (old); same subject
RELATION_REPLACES = "replaces"  # source replaces target; may cross subjects
RELATION_DUPLICATE_OF = "duplicate_of"  # source is duplicate of canonical target

RELATION_TYPES: frozenset[str] = frozenset(
    {
        RELATION_SUPERSEDES,
        RELATION_REPLACES,
        RELATION_DUPLICATE_OF,
    }
)

# Simple status transitions that do not require a related document.
# Compound ops (supersede / replace / duplicate) are handled separately.
_SIMPLE_TRANSITIONS: dict[str, frozenset[str]] = {
    LIFECYCLE_ACTIVE: frozenset(
        {LIFECYCLE_ARCHIVED, LIFECYCLE_WITHDRAWN}
    ),
    LIFECYCLE_SUPERSEDED: frozenset({LIFECYCLE_ARCHIVED}),
    LIFECYCLE_REPLACED: frozenset({LIFECYCLE_ARCHIVED}),
    LIFECYCLE_WITHDRAWN: frozenset({LIFECYCLE_ARCHIVED}),
    LIFECYCLE_DUPLICATE: frozenset({LIFECYCLE_ARCHIVED}),
    LIFECYCLE_ARCHIVED: frozenset(),  # terminal for simple ops
}

# Statuses from which supersede may promote a successor to ACTIVE.
_SUPERSEDE_PROMOTABLE: frozenset[str] = frozenset(
    {
        LIFECYCLE_ACTIVE,  # already active → idempotent path
        LIFECYCLE_ARCHIVED,
        LIFECYCLE_WITHDRAWN,
        LIFECYCLE_SUPERSEDED,
        LIFECYCLE_REPLACED,
        LIFECYCLE_DUPLICATE,
    }
)


def transition_matrix() -> dict[str, list[str]]:
    """Return the simple (non-compound) allowed transition matrix."""
    return {src: sorted(dsts) for src, dsts in sorted(_SIMPLE_TRANSITIONS.items())}


def _validate_doc(document_id: str) -> str:
    try:
        return validate_document_id(document_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc


def _validate_subj(subject_id: str) -> str:
    try:
        return validate_subject_id(subject_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc


def _get_revision(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    _validate_doc(document_id)
    row = conn.execute(
        "SELECT * FROM document_versions WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        raise RegistryValidationError(f"document_id not found: {document_id!r}")
    return _row_to_dict(row)  # type: ignore[return-value]


def _require_columns(conn: sqlite3.Connection) -> None:
    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(document_versions)").fetchall()
    }
    if "lifecycle_status" not in cols:
        raise LifecycleError(
            "document_versions.lifecycle_status missing; migrate registry to schema v2"
        )


def _active_for_subject(
    conn: sqlite3.Connection, subject_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM document_versions "
        "WHERE subject_id = ? AND lifecycle_status = ? "
        "ORDER BY document_id",
        (subject_id, LIFECYCLE_ACTIVE),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def _append_event(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    previous_state: str | None,
    new_state: str,
    reason: str | None,
    related_document_id: str | None = None,
    relation_type: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    ts = created_at or utc_now()
    cur = conn.execute(
        "INSERT INTO document_lifecycle_events ("
        "document_id, previous_state, new_state, relation_type, "
        "related_document_id, reason, actor, source, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            document_id,
            previous_state,
            new_state,
            relation_type,
            related_document_id,
            reason,
            actor,
            source,
            ts,
        ),
    )
    event_id = int(cur.lastrowid)
    row = conn.execute(
        "SELECT * FROM document_lifecycle_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def _upsert_relation(
    conn: sqlite3.Connection,
    *,
    source_document_id: str,
    target_document_id: str,
    relation_type: str,
    reason: str | None,
    actor: str | None,
    source: str | None,
    created_at: str | None = None,
) -> dict[str, Any]:
    if relation_type not in RELATION_TYPES:
        raise RegistryValidationError(f"unknown relation_type: {relation_type!r}")
    if source_document_id == target_document_id:
        raise RegistryValidationError(
            f"relation {relation_type!r} cannot target the same document_id"
        )
    existing = conn.execute(
        "SELECT * FROM document_version_relations "
        "WHERE source_document_id = ? AND target_document_id = ? AND relation_type = ?",
        (source_document_id, target_document_id, relation_type),
    ).fetchone()
    if existing is not None:
        return _row_to_dict(existing)  # type: ignore[return-value]

    # Conflicting same-type edge from source to a different target
    other = conn.execute(
        "SELECT * FROM document_version_relations "
        "WHERE source_document_id = ? AND relation_type = ?",
        (source_document_id, relation_type),
    ).fetchone()
    if other is not None and other["target_document_id"] != target_document_id:
        raise RegistryConflictError(
            f"document {source_document_id!r} already has {relation_type!r} → "
            f"{other['target_document_id']!r}; refusing {target_document_id!r}"
        )

    ts = created_at or utc_now()
    conn.execute(
        "INSERT INTO document_version_relations ("
        "source_document_id, target_document_id, relation_type, reason, "
        "created_at, actor, source"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            source_document_id,
            target_document_id,
            relation_type,
            reason,
            ts,
            actor,
            source,
        ),
    )
    row = conn.execute(
        "SELECT * FROM document_version_relations "
        "WHERE source_document_id = ? AND target_document_id = ? AND relation_type = ?",
        (source_document_id, target_document_id, relation_type),
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def _set_status(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    new_state: str,
    reason: str | None,
    related_document_id: str | None = None,
    relation_type: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    rev = _get_revision(conn, document_id)
    prev = rev["lifecycle_status"]
    if prev == new_state and related_document_id is None:
        # Pure status idempotent no-op (no new event).
        return {
            "document_version": rev,
            "event": None,
            "idempotent": True,
        }
    ts = created_at or utc_now()
    conn.execute(
        "UPDATE document_versions "
        "SET lifecycle_status = ?, lifecycle_updated_at = ? "
        "WHERE document_id = ?",
        (new_state, ts, document_id),
    )
    event = _append_event(
        conn,
        document_id=document_id,
        previous_state=prev,
        new_state=new_state,
        reason=reason,
        related_document_id=related_document_id,
        relation_type=relation_type,
        actor=actor,
        source=source,
        created_at=ts,
    )
    updated = _get_revision(conn, document_id)
    return {"document_version": updated, "event": event, "idempotent": False}


def get_revision_status(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    """Return revision row including lifecycle_status."""
    _require_columns(conn)
    return _get_revision(conn, document_id)


def get_active_revision(
    conn: sqlite3.Connection, subject_id: str
) -> dict[str, Any] | None:
    """Return the ACTIVE revision for a subject, or None."""
    _require_columns(conn)
    _validate_subj(subject_id)
    return _active_for_subject(conn, subject_id)


def get_lifecycle_history(
    conn: sqlite3.Connection, document_id: str
) -> list[dict[str, Any]]:
    """Append-only event history for a revision, oldest first."""
    _require_columns(conn)
    _validate_doc(document_id)
    rows = conn.execute(
        "SELECT * FROM document_lifecycle_events "
        "WHERE document_id = ? "
        "ORDER BY event_id ASC",
        (document_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def get_relations(
    conn: sqlite3.Connection, document_id: str
) -> list[dict[str, Any]]:
    """Relations where document is source or target."""
    _require_columns(conn)
    _validate_doc(document_id)
    rows = conn.execute(
        "SELECT * FROM document_version_relations "
        "WHERE source_document_id = ? OR target_document_id = ? "
        "ORDER BY relation_id ASC",
        (document_id, document_id),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def set_revision_status(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    new_state: str,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Apply a simple status transition (no related document).

    Compound transitions (SUPERSEDED / REPLACED / DUPLICATE) must use the
    dedicated high-level operations.
    """
    _require_columns(conn)
    if new_state not in LIFECYCLE_STATES:
        raise RegistryValidationError(f"unknown lifecycle status: {new_state!r}")
    if new_state in {
        LIFECYCLE_SUPERSEDED,
        LIFECYCLE_REPLACED,
        LIFECYCLE_DUPLICATE,
    }:
        raise LifecycleTransitionError(
            f"status {new_state!r} requires a related document; "
            "use supersede_revision / replace_revision / mark_duplicate"
        )

    with registry_transaction(conn):
        rev = _get_revision(conn, document_id)
        prev = rev["lifecycle_status"]
        if prev == new_state:
            return {
                "document_version": rev,
                "event": None,
                "idempotent": True,
            }
        allowed = _SIMPLE_TRANSITIONS.get(prev, frozenset())
        if new_state not in allowed:
            raise LifecycleTransitionError(
                f"invalid transition {prev!r} → {new_state!r}"
            )
        if new_state == LIFECYCLE_ACTIVE:
            # Defensive: simple matrix never allows promote-to-ACTIVE.
            raise LifecycleTransitionError(
                "promoting to ACTIVE requires supersede_revision / replace_revision"
            )
        return _set_status(
            conn,
            document_id=document_id,
            new_state=new_state,
            reason=reason,
            actor=actor,
            source=source,
        )


def archive_revision(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    return set_revision_status(
        conn,
        document_id=document_id,
        new_state=LIFECYCLE_ARCHIVED,
        reason=reason,
        actor=actor,
        source=source,
    )


def withdraw_revision(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    return set_revision_status(
        conn,
        document_id=document_id,
        new_state=LIFECYCLE_WITHDRAWN,
        reason=reason,
        actor=actor,
        source=source,
    )


def activate_revision(
    conn: sqlite3.Connection,
    document_id: str,
    *,
    reason: str,
    actor: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Explicitly select a WITHDRAWN revision as ACTIVE for its subject.

    Narrow administrative/current-selection path for cases where migration (or
    an equivalent) left a subject with zero ACTIVE revisions. Does **not**
    invent supersedes/replaces/duplicate_of relations.

    Constraints:
    - source status must be WITHDRAWN (rejects SUPERSEDED/DUPLICATE/ARCHIVED/…)
    - ``reason`` is mandatory
    - subject must have no other ACTIVE revision
    - atomic via ``registry_transaction``
    """
    _require_columns(conn)
    if not isinstance(reason, str) or not reason.strip():
        raise RegistryValidationError(
            "activate_revision requires a non-empty explicit reason"
        )

    with registry_transaction(conn):
        rev = _get_revision(conn, document_id)
        prev = rev["lifecycle_status"]
        if prev == LIFECYCLE_ACTIVE:
            return {
                "document_version": rev,
                "event": None,
                "idempotent": True,
            }
        if prev != LIFECYCLE_WITHDRAWN:
            raise LifecycleTransitionError(
                f"activate_revision only allows WITHDRAWN → ACTIVE; "
                f"got {prev!r} (use supersede_revision / replace_revision "
                "for succession, not generic reactivation)"
            )
        active = _active_for_subject(conn, rev["subject_id"])
        if active is not None and active["document_id"] != document_id:
            raise RegistryConflictError(
                f"subject {rev['subject_id']!r} already has ACTIVE revision "
                f"{active['document_id']!r}; demote or supersede it first"
            )
        return _set_status(
            conn,
            document_id=document_id,
            new_state=LIFECYCLE_ACTIVE,
            reason=reason.strip(),
            actor=actor,
            source=source,
        )


def supersede_revision(
    conn: sqlite3.Connection,
    *,
    old_document_id: str,
    new_document_id: str,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Atomically make ``new`` ACTIVE and ``old`` SUPERSEDED (same subject).

    Records relation ``new supersedes old`` and append-only events.
    Idempotent when the same pair is already in the terminal configuration.
    """
    _require_columns(conn)
    if old_document_id == new_document_id:
        raise RegistryValidationError("cannot supersede a revision with itself")

    with registry_transaction(conn):
        old = _get_revision(conn, old_document_id)
        new = _get_revision(conn, new_document_id)
        if old["subject_id"] != new["subject_id"]:
            raise LifecycleTransitionError(
                "SUPERSEDED requires same subject_id; "
                f"{old['subject_id']!r} vs {new['subject_id']!r} "
                "(use replace_revision for cross-subject replacement)"
            )

        # Idempotent completed state
        rel = conn.execute(
            "SELECT * FROM document_version_relations "
            "WHERE source_document_id = ? AND target_document_id = ? "
            "AND relation_type = ?",
            (new_document_id, old_document_id, RELATION_SUPERSEDES),
        ).fetchone()
        if (
            old["lifecycle_status"] == LIFECYCLE_SUPERSEDED
            and new["lifecycle_status"] == LIFECYCLE_ACTIVE
            and rel is not None
        ):
            return {
                "old": old,
                "new": new,
                "relation": _row_to_dict(rel),
                "events": [],
                "idempotent": True,
            }

        if new["lifecycle_status"] not in _SUPERSEDE_PROMOTABLE:
            raise LifecycleTransitionError(
                f"successor status {new['lifecycle_status']!r} cannot be promoted"
            )

        if old["lifecycle_status"] == LIFECYCLE_SUPERSEDED and rel is None:
            # Already superseded by someone else
            raise RegistryConflictError(
                f"{old_document_id!r} is already SUPERSEDED without "
                f"supersedes edge from {new_document_id!r}"
            )

        if old["lifecycle_status"] not in {
            LIFECYCLE_ACTIVE,
            LIFECYCLE_SUPERSEDED,
        }:
            raise LifecycleTransitionError(
                f"cannot supersede revision in status {old['lifecycle_status']!r}"
            )

        # Ensure no third ACTIVE blocks promotion
        active = _active_for_subject(conn, old["subject_id"])
        if (
            active is not None
            and active["document_id"] not in {old_document_id, new_document_id}
        ):
            raise RegistryConflictError(
                f"subject {old['subject_id']!r} already has ACTIVE revision "
                f"{active['document_id']!r}"
            )

        events: list[dict[str, Any]] = []
        # Demote old first so at-most-one ACTIVE holds throughout.
        if old["lifecycle_status"] != LIFECYCLE_SUPERSEDED:
            res_old = _set_status(
                conn,
                document_id=old_document_id,
                new_state=LIFECYCLE_SUPERSEDED,
                reason=reason,
                related_document_id=new_document_id,
                relation_type=RELATION_SUPERSEDES,
                actor=actor,
                source=source,
            )
            if res_old["event"] is not None:
                events.append(res_old["event"])

        if new["lifecycle_status"] != LIFECYCLE_ACTIVE:
            res_new = _set_status(
                conn,
                document_id=new_document_id,
                new_state=LIFECYCLE_ACTIVE,
                reason=reason,
                related_document_id=old_document_id,
                relation_type=RELATION_SUPERSEDES,
                actor=actor,
                source=source,
            )
            if res_new["event"] is not None:
                events.append(res_new["event"])

        relation = _upsert_relation(
            conn,
            source_document_id=new_document_id,
            target_document_id=old_document_id,
            relation_type=RELATION_SUPERSEDES,
            reason=reason,
            actor=actor,
            source=source,
        )
        return {
            "old": _get_revision(conn, old_document_id),
            "new": _get_revision(conn, new_document_id),
            "relation": relation,
            "events": events,
            "idempotent": False,
        }


def replace_revision(
    conn: sqlite3.Connection,
    *,
    old_document_id: str,
    new_document_id: str,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Mark ``old`` as REPLACED by ``new`` (cross-subject allowed).

    Does not automatically change ``new`` status. Records ``new replaces old``.
    """
    _require_columns(conn)
    if old_document_id == new_document_id:
        raise RegistryValidationError("cannot replace a revision with itself")

    with registry_transaction(conn):
        old = _get_revision(conn, old_document_id)
        new = _get_revision(conn, new_document_id)  # must exist

        rel = conn.execute(
            "SELECT * FROM document_version_relations "
            "WHERE source_document_id = ? AND target_document_id = ? "
            "AND relation_type = ?",
            (new_document_id, old_document_id, RELATION_REPLACES),
        ).fetchone()
        if old["lifecycle_status"] == LIFECYCLE_REPLACED and rel is not None:
            return {
                "old": old,
                "new": new,
                "relation": _row_to_dict(rel),
                "event": None,
                "idempotent": True,
            }

        if old["lifecycle_status"] not in {
            LIFECYCLE_ACTIVE,
            LIFECYCLE_WITHDRAWN,
            LIFECYCLE_ARCHIVED,
            LIFECYCLE_REPLACED,
        }:
            raise LifecycleTransitionError(
                f"cannot mark {old['lifecycle_status']!r} as REPLACED"
            )

        res = {"event": None, "idempotent": True}
        if old["lifecycle_status"] != LIFECYCLE_REPLACED:
            res = _set_status(
                conn,
                document_id=old_document_id,
                new_state=LIFECYCLE_REPLACED,
                reason=reason,
                related_document_id=new_document_id,
                relation_type=RELATION_REPLACES,
                actor=actor,
                source=source,
            )
        relation = _upsert_relation(
            conn,
            source_document_id=new_document_id,
            target_document_id=old_document_id,
            relation_type=RELATION_REPLACES,
            reason=reason,
            actor=actor,
            source=source,
        )
        return {
            "old": _get_revision(conn, old_document_id),
            "new": new,
            "relation": relation,
            "event": res.get("event"),
            "idempotent": bool(res.get("idempotent") and rel is not None),
        }


def mark_duplicate(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    canonical_document_id: str,
    reason: str | None = None,
    actor: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Declare ``document_id`` a DUPLICATE of an explicit canonical revision.

    Does not delete rows, locators, or vectors.
    """
    _require_columns(conn)
    if document_id == canonical_document_id:
        raise RegistryValidationError("self-duplicate is not allowed")

    with registry_transaction(conn):
        dup = _get_revision(conn, document_id)
        canonical = _get_revision(conn, canonical_document_id)

        rel = conn.execute(
            "SELECT * FROM document_version_relations "
            "WHERE source_document_id = ? AND target_document_id = ? "
            "AND relation_type = ?",
            (document_id, canonical_document_id, RELATION_DUPLICATE_OF),
        ).fetchone()
        if dup["lifecycle_status"] == LIFECYCLE_DUPLICATE and rel is not None:
            return {
                "document_version": dup,
                "canonical": canonical,
                "relation": _row_to_dict(rel),
                "event": None,
                "idempotent": True,
            }

        if dup["lifecycle_status"] not in {
            LIFECYCLE_ACTIVE,
            LIFECYCLE_WITHDRAWN,
            LIFECYCLE_ARCHIVED,
            LIFECYCLE_DUPLICATE,
        }:
            raise LifecycleTransitionError(
                f"cannot mark {dup['lifecycle_status']!r} as DUPLICATE"
            )

        res = {"event": None}
        if dup["lifecycle_status"] != LIFECYCLE_DUPLICATE:
            res = _set_status(
                conn,
                document_id=document_id,
                new_state=LIFECYCLE_DUPLICATE,
                reason=reason,
                related_document_id=canonical_document_id,
                relation_type=RELATION_DUPLICATE_OF,
                actor=actor,
                source=source,
            )
        relation = _upsert_relation(
            conn,
            source_document_id=document_id,
            target_document_id=canonical_document_id,
            relation_type=RELATION_DUPLICATE_OF,
            reason=reason,
            actor=actor,
            source=source,
        )
        return {
            "document_version": _get_revision(conn, document_id),
            "canonical": canonical,
            "relation": relation,
            "event": res.get("event"),
            "idempotent": False,
        }


def resolve_initial_lifecycle_status(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    requested: str | None,
) -> str:
    """Decide lifecycle_status for a newly inserted revision.

    - requested explicit value → validated and returned (ACTIVE uniqueness checked)
    - None → ACTIVE if subject has no ACTIVE revision; otherwise error
      (caller must pass a non-ACTIVE staging status then use supersede_revision)
    """
    _validate_subj(subject_id)
    if requested is not None:
        if requested not in LIFECYCLE_STATES:
            raise RegistryValidationError(
                f"unknown lifecycle status: {requested!r}"
            )
        if requested == LIFECYCLE_ACTIVE:
            active = _active_for_subject(conn, subject_id)
            if active is not None:
                raise RegistryConflictError(
                    f"subject {subject_id!r} already has ACTIVE revision "
                    f"{active['document_id']!r}; register with a non-ACTIVE "
                    "status then call supersede_revision, or demote the "
                    "current ACTIVE first"
                )
        return requested

    active = _active_for_subject(conn, subject_id)
    if active is None:
        return DEFAULT_LIFECYCLE_STATUS
    raise RegistryConflictError(
        f"subject {subject_id!r} already has ACTIVE revision "
        f"{active['document_id']!r}; pass lifecycle_status=<non-ACTIVE> "
        "to stage the successor, then call supersede_revision"
    )


def assert_at_most_one_active(conn: sqlite3.Connection, subject_id: str) -> None:
    """Fail closed if more than one ACTIVE revision exists for a subject."""
    _validate_subj(subject_id)
    rows = conn.execute(
        "SELECT document_id FROM document_versions "
        "WHERE subject_id = ? AND lifecycle_status = ?",
        (subject_id, LIFECYCLE_ACTIVE),
    ).fetchall()
    if len(rows) > 1:
        ids = [r["document_id"] for r in rows]
        raise RegistryConflictError(
            f"subject {subject_id!r} has multiple ACTIVE revisions: {ids}"
        )

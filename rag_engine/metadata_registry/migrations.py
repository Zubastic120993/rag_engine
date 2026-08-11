"""Forward-only migrations for the Phase 3 metadata registry."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from typing import Callable, Final

from rag_engine.metadata_registry.connection import open_registry, normalize_db_path
from rag_engine.metadata_registry.exceptions import (
    DowngradeNotAllowedError,
    MigrationError,
    UnknownSchemaVersionError,
)
from rag_engine.metadata_registry.schema import (
    CURRENT_SCHEMA_VERSION,
    REQUIRED_TABLES,
    SCHEMA_SQL,
    SCHEMA_SQL_V2_UPGRADE,
    SCHEMA_SQL_V3_UPGRADE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return applied schema version; 0 if uninitialized. Fail closed on newer."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='registry_schema_version'"
    ).fetchone()
    if row is None:
        return 0
    version_row = conn.execute(
        "SELECT MAX(schema_version) AS version FROM registry_schema_version"
    ).fetchone()
    version = int(version_row["version"] or 0) if version_row is not None else 0
    if version > CURRENT_SCHEMA_VERSION:
        raise UnknownSchemaVersionError(
            f"registry schema version {version} is newer than supported "
            f"version {CURRENT_SCHEMA_VERSION}"
        )
    return version


# Scaffold-compatible alias
current_schema_version = get_schema_version


def _apply_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO registry_schema_version "
        "(schema_version, applied_at, status, description, backward_compatible) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            1,
            utc_now(),
            "applied",
            "Phase 3 stable-id-v1 aligned registry (documents/subject_id, "
            "document_versions/document_id, chunks, chunk_vector_map)",
            1,
        ),
    )


# Migration audit markers (distinguishable from runtime lifecycle ops).
MIGRATION_V2_SOURCE: Final = "schema_migration_v1_to_v2"
MIGRATION_V2_MULTI_REASON: Final = "migration_v1_to_v2_multi_revision_current_unknown"


def _apply_v2(conn: sqlite3.Connection) -> None:
    """Phase 5 revision lifecycle — additive columns + event/relation tables.

    Compatibility policy for pre-existing v1 rows (no lifecycle authority):

    * subject with exactly one revision → leave/set ACTIVE (unambiguous)
    * subject with multiple revisions → ALL revisions WITHDRAWN, zero ACTIVE,
      zero inferred relations. WITHDRAWN here means "not declared operationally
      current by migration", not that a historical withdrawal occurred.
    """
    conn.executescript(SCHEMA_SQL_V2_UPGRADE)
    conn.execute(
        "UPDATE document_versions "
        "SET lifecycle_updated_at = created_at "
        "WHERE lifecycle_updated_at IS NULL"
    )
    # ALTER DEFAULT leaves every row ACTIVE. Single-revision subjects stay ACTIVE.
    # Multi-revision subjects: conservatively withdraw all — do not invent
    # which revision is current or fabricate supersedes/replaces/duplicate_of.
    subjects = conn.execute(
        "SELECT subject_id FROM document_versions GROUP BY subject_id "
        "HAVING COUNT(*) > 1"
    ).fetchall()
    ts = utc_now()
    for row in subjects:
        sid = row["subject_id"] if isinstance(row, sqlite3.Row) else row[0]
        revs = conn.execute(
            "SELECT document_id FROM document_versions WHERE subject_id = ?",
            (sid,),
        ).fetchall()
        for rev in revs:
            doc_id = rev["document_id"] if isinstance(rev, sqlite3.Row) else rev[0]
            conn.execute(
                "UPDATE document_versions "
                "SET lifecycle_status = 'WITHDRAWN', lifecycle_updated_at = ? "
                "WHERE document_id = ?",
                (ts, doc_id),
            )
            # previous_state NULL: schema default ACTIVE is not historical truth.
            conn.execute(
                "INSERT INTO document_lifecycle_events ("
                "document_id, previous_state, new_state, relation_type, "
                "related_document_id, reason, actor, source, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    None,
                    "WITHDRAWN",
                    None,
                    None,
                    MIGRATION_V2_MULTI_REASON,
                    None,
                    MIGRATION_V2_SOURCE,
                    ts,
                ),
            )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_document_versions_one_active_per_subject "
        "ON document_versions(subject_id) WHERE lifecycle_status = 'ACTIVE'"
    )
    conn.execute(
        "INSERT OR IGNORE INTO registry_schema_version "
        "(schema_version, applied_at, status, description, backward_compatible) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            2,
            utc_now(),
            "applied",
            "Phase 5 document revision lifecycle "
            "(lifecycle_status, events, relations; at most one ACTIVE per subject)",
            1,
        ),
    )


def _apply_v3(conn: sqlite3.Connection) -> None:
    """Phase 6B embedding-fp-v1 index fingerprint authority table."""
    conn.executescript(SCHEMA_SQL_V3_UPGRADE)
    conn.execute(
        "INSERT OR IGNORE INTO registry_schema_version "
        "(schema_version, applied_at, status, description, backward_compatible) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            3,
            utc_now(),
            "applied",
            "Phase 6B index fingerprint authority "
            "(index_fingerprints; embedding-fp-v1 envelope)",
            1,
        ),
    )


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _apply_v1,
    2: _apply_v2,
    3: _apply_v3,
}


def migrate_connection(
    conn: sqlite3.Connection,
    *,
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> int:
    current = get_schema_version(conn)
    if target_version < current:
        raise DowngradeNotAllowedError(
            f"downgrade not allowed: current={current} target={target_version}"
        )
    if target_version > CURRENT_SCHEMA_VERSION:
        raise UnknownSchemaVersionError(
            f"target schema version {target_version} is newer than supported "
            f"{CURRENT_SCHEMA_VERSION}"
        )
    if current == target_version:
        return current

    try:
        conn.execute("BEGIN IMMEDIATE")
        for version in range(current + 1, target_version + 1):
            migration = _MIGRATIONS.get(version)
            if migration is None:
                raise MigrationError(f"missing migration for schema version {version}")
            migration(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    final = get_schema_version(conn)
    if final != target_version:
        raise MigrationError(f"migration incomplete: expected {target_version}, got {final}")
    return final


def initialize_registry(
    db_path: str | Path,
    *,
    fail_if_exists: bool = False,
) -> Path:
    """Create/migrate registry at explicit path. Does not touch production defaults."""
    path = normalize_db_path(db_path)
    conn = open_registry(path, create=True, fail_if_exists=fail_if_exists)
    try:
        migrate_connection(conn)
        missing = [
            t
            for t in REQUIRED_TABLES
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            is None
        ]
        if missing:
            raise MigrationError(f"required tables missing after init: {missing}")
    finally:
        conn.close()
    return path


def foreign_keys_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    return bool(row[0]) if row is not None else False

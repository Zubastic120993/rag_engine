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
    SCHEMA_SQL_V4_UPGRADE,
    SCHEMA_SQL_V5_UPGRADE,
    V4_REQUIRED_INDEXES,
    V5_LIFECYCLE_EVENTS_TABLE,
    V5_LOCATOR_STATE_TABLE,
    V5_REQUIRED_INDEXES,
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


V4_TABLE_NAME: Final = "source_file_events"
MIGRATION_V4_DESCRIPTION: Final = (
    "Phase 7 source file events audit table (append-only, alias/compensation evidence)"
)


def _v4_table_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (V4_TABLE_NAME,),
    ).fetchone()
    return row is not None


def _v4_index_present(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


def _v4_objects_complete(conn: sqlite3.Connection) -> bool:
    if not _v4_table_present(conn):
        return False
    return all(_v4_index_present(conn, name) for name in V4_REQUIRED_INDEXES)


def _v4_missing_indexes(conn: sqlite3.Connection) -> list[str]:
    if not _v4_table_present(conn):
        return list(V4_REQUIRED_INDEXES)
    return [
        name for name in V4_REQUIRED_INDEXES if not _v4_index_present(conn, name)
    ]


def _find_duplicate_approval_digests(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    if not _v4_table_present(conn):
        return []
    rows = conn.execute(
        "SELECT approval_digest, COUNT(*) AS c "
        "FROM source_file_events "
        "WHERE approval_digest IS NOT NULL "
        "GROUP BY approval_digest "
        "HAVING c > 1"
    ).fetchall()
    return [
        (
            row["approval_digest"] if isinstance(row, sqlite3.Row) else row[0],
            int(row["c"] if isinstance(row, sqlite3.Row) else row[1]),
        )
        for row in rows
    ]


def _assert_no_duplicate_approval_digests(conn: sqlite3.Connection) -> None:
    duplicates = _find_duplicate_approval_digests(conn)
    if not duplicates:
        return
    detail = ", ".join(f"{digest!r} x{count}" for digest, count in duplicates)
    raise MigrationError(
        "duplicate non-null approval_digest values block V4 unique index creation: "
        f"{detail}"
    )


def _record_v4_schema_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO registry_schema_version "
        "(schema_version, applied_at, status, description, backward_compatible) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            4,
            utc_now(),
            "applied",
            MIGRATION_V4_DESCRIPTION,
            0,
        ),
    )


def _verify_v4_postflight(conn: sqlite3.Connection) -> None:
    """Mandatory post-flight verification after V4 DDL or repair."""
    if not _v4_table_present(conn):
        raise MigrationError(
            "V4 post-flight verification failed: source_file_events table missing"
        )
    missing = _v4_missing_indexes(conn)
    if missing:
        raise MigrationError(
            "V4 post-flight verification failed: missing indexes: "
            + ", ".join(missing)
        )
    version = get_schema_version(conn)
    if version < 4:
        raise MigrationError(
            f"V4 post-flight verification failed: schema version is {version}, expected 4"
        )
    if not foreign_keys_enabled(conn):
        raise MigrationError(
            "V4 post-flight verification failed: foreign keys are not enabled"
        )


def _apply_v4_ddl(conn: sqlite3.Connection) -> None:
    """Idempotent V4 DDL. Fails closed on duplicate non-null approval digests."""
    _assert_no_duplicate_approval_digests(conn)
    try:
        conn.executescript(SCHEMA_SQL_V4_UPGRADE)
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "unique" in msg or "duplicate" in msg:
            raise MigrationError(
                "V4 migration failed: duplicate approval_digest blocks unique index "
                f"creation: {exc}"
            ) from exc
        raise


def _apply_v4(conn: sqlite3.Connection) -> None:
    """Phase 7 source file events — additive DDL + version record."""
    _apply_v4_ddl(conn)
    _record_v4_schema_version(conn)
    _verify_v4_postflight(conn)


def _ensure_v4_complete(conn: sqlite3.Connection) -> None:
    """Bounded idempotent repair for recoverable incomplete V4 object states.

    Handles version-4 databases where the normal migration loop would return
    early, and partial states where V4 objects exist without a version record.
    Never mutates event data except inserting the missing schema-version record.
    """
    version = get_schema_version(conn)
    complete = _v4_objects_complete(conn)
    any_v4_object = _v4_table_present(conn) or any(
        _v4_index_present(conn, name) for name in V4_REQUIRED_INDEXES
    )

    if version < 4 and not any_v4_object:
        return

    if version >= 4 and complete:
        _verify_v4_postflight(conn)
        return

    _assert_no_duplicate_approval_digests(conn)

    if not complete:
        _apply_v4_ddl(conn)

    if version < 4 and _v4_objects_complete(conn):
        _record_v4_schema_version(conn)

    _verify_v4_postflight(conn)


def _verify_v4_baseline(conn: sqlite3.Connection) -> None:
    """V5 precondition: complete V4 baseline must exist."""
    version = get_schema_version(conn)
    if version < 4:
        raise MigrationError(
            f"V5 requires schema version >= 4; current version is {version}"
        )
    _verify_v4_postflight(conn)


def _v5_table_present(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _v5_index_present(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()
    return row is not None


def _v5_objects_complete(conn: sqlite3.Connection) -> bool:
    if not _v5_table_present(conn, V5_LIFECYCLE_EVENTS_TABLE):
        return False
    if not _v5_table_present(conn, V5_LOCATOR_STATE_TABLE):
        return False
    return all(_v5_index_present(conn, name) for name in V5_REQUIRED_INDEXES)


def _v5_missing_indexes(conn: sqlite3.Connection) -> list[str]:
    missing: list[str] = []
    if not _v5_table_present(conn, V5_LIFECYCLE_EVENTS_TABLE) or not _v5_table_present(
        conn, V5_LOCATOR_STATE_TABLE
    ):
        return list(V5_REQUIRED_INDEXES)
    for name in V5_REQUIRED_INDEXES:
        if not _v5_index_present(conn, name):
            missing.append(name)
    return missing


def _record_v5_schema_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO registry_schema_version "
        "(schema_version, applied_at, status, description, backward_compatible) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            5,
            utc_now(),
            "applied",
            "Phase 8 source-file locator lifecycle (events + state projection)",
            0,
        ),
    )


def _backfill_v5_locator_state(conn: sqlite3.Connection) -> None:
    from rag_engine.metadata_registry.locator_lifecycle import (
        MIGRATION_V5_REASON,
        MIGRATION_V5_SOURCE,
        initialize_locator_lifecycle_state,
    )

    rows = conn.execute(
        "SELECT source_file_id, document_id FROM source_files ORDER BY source_file_id"
    ).fetchall()
    for row in rows:
        sf = row["source_file_id"]
        doc = row["document_id"]
        exists = conn.execute(
            "SELECT 1 FROM source_file_locator_state WHERE source_file_id = ?",
            (sf,),
        ).fetchone()
        if exists is not None:
            continue
        initialize_locator_lifecycle_state(
            conn,
            source_file_id=sf,
            document_id=doc,
            source=MIGRATION_V5_SOURCE,
            reason=MIGRATION_V5_REASON,
        )


def _verify_v5_postflight(conn: sqlite3.Connection) -> None:
    """Mandatory post-flight verification after V5 DDL, backfill, or repair."""
    _verify_v4_baseline(conn)
    if not _v5_table_present(conn, V5_LIFECYCLE_EVENTS_TABLE):
        raise MigrationError(
            "V5 post-flight verification failed: "
            "source_file_locator_lifecycle_events table missing"
        )
    if not _v5_table_present(conn, V5_LOCATOR_STATE_TABLE):
        raise MigrationError(
            "V5 post-flight verification failed: source_file_locator_state table missing"
        )
    missing = _v5_missing_indexes(conn)
    if missing:
        raise MigrationError(
            "V5 post-flight verification failed: missing indexes: "
            + ", ".join(missing)
        )
    version = get_schema_version(conn)
    if version < 5:
        raise MigrationError(
            f"V5 post-flight verification failed: schema version is {version}, expected 5"
        )
    if not foreign_keys_enabled(conn):
        raise MigrationError(
            "V5 post-flight verification failed: foreign keys are not enabled"
        )

    sf_count = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
    st_count = conn.execute(
        "SELECT COUNT(*) AS c FROM source_file_locator_state"
    ).fetchone()["c"]
    if int(sf_count) != int(st_count):
        raise MigrationError(
            "V5 post-flight verification failed: source_files count "
            f"{sf_count} != locator state count {st_count}"
        )

    invalid = conn.execute(
        "SELECT source_file_id FROM source_file_locator_state "
        "WHERE activity_state NOT IN ("
        "'ACTIVE','COMPENSATION_PENDING','INACTIVE',"
        "'COMPENSATION_REJECTED','COMPENSATION_FAILED'"
        ")"
    ).fetchall()
    if invalid:
        raise MigrationError(
            "V5 post-flight verification failed: invalid activity_state rows present"
        )

    from rag_engine.metadata_registry.locator_lifecycle import (
        LOCATOR_EVENT_INITIALIZED,
        MIGRATION_V5_SOURCE,
    )

    per_locator = conn.execute(
        "SELECT sf.source_file_id, "
        "CASE WHEN s.source_file_id IS NULL THEN 0 ELSE 1 END AS state_rows, "
        "COALESCE(b.baseline_count, 0) AS baseline_count "
        "FROM source_files sf "
        "LEFT JOIN source_file_locator_state s ON s.source_file_id = sf.source_file_id "
        "LEFT JOIN ("
        "  SELECT source_file_id, COUNT(*) AS baseline_count "
        "  FROM source_file_locator_lifecycle_events "
        "  WHERE event_type = ? AND source = ? "
        "  GROUP BY source_file_id"
        ") b ON b.source_file_id = sf.source_file_id "
        "ORDER BY sf.source_file_id",
        (LOCATOR_EVENT_INITIALIZED, MIGRATION_V5_SOURCE),
    ).fetchall()

    baseline_problems: list[str] = []
    for row in per_locator:
        sf_id = row["source_file_id"]
        if int(row["state_rows"]) != 1:
            baseline_problems.append(f"{sf_id!r}: missing locator state row")
        baseline_count = int(row["baseline_count"])
        if baseline_count == 0:
            baseline_problems.append(
                f"{sf_id!r}: zero migration-baseline INITIALIZED events"
            )
        elif baseline_count > 1:
            baseline_problems.append(
                f"{sf_id!r}: {baseline_count} migration-baseline INITIALIZED events "
                "(expected 1)"
            )

    if baseline_problems:
        raise MigrationError(
            "V5 post-flight verification failed: per-locator migration baseline "
            "coverage invalid: " + "; ".join(baseline_problems)
        )


def _apply_v5_ddl(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL_V5_UPGRADE)


def _apply_v5(conn: sqlite3.Connection) -> None:
    """Phase 8 locator lifecycle — requires complete V4; additive DDL + backfill."""
    _verify_v4_baseline(conn)
    _apply_v5_ddl(conn)
    _backfill_v5_locator_state(conn)
    _record_v5_schema_version(conn)
    _verify_v5_postflight(conn)


def _ensure_v5_complete(conn: sqlite3.Connection) -> None:
    """Bounded idempotent repair for incomplete V5 object/backfill states."""
    version = get_schema_version(conn)
    complete = _v5_objects_complete(conn)
    any_v5 = (
        _v5_table_present(conn, V5_LIFECYCLE_EVENTS_TABLE)
        or _v5_table_present(conn, V5_LOCATOR_STATE_TABLE)
        or any(_v5_index_present(conn, name) for name in V5_REQUIRED_INDEXES)
    )

    if version < 5 and not any_v5:
        return

    if version >= 5 and complete:
        sf_count = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        st_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_state"
        ).fetchone()["c"]
        if int(sf_count) == int(st_count):
            try:
                _verify_v5_postflight(conn)
                return
            except MigrationError:
                pass

    _verify_v4_baseline(conn)

    if not complete:
        _apply_v5_ddl(conn)

    _backfill_v5_locator_state(conn)

    if version < 5:
        _record_v5_schema_version(conn)

    _verify_v5_postflight(conn)


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _apply_v1,
    2: _apply_v2,
    3: _apply_v3,
    4: _apply_v4,
    5: _apply_v5,
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
        if target_version >= 5:
            _ensure_v5_complete(conn)
        elif target_version >= 4:
            _ensure_v4_complete(conn)
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

    if target_version >= 5:
        _ensure_v5_complete(conn)
    elif target_version >= 4:
        _ensure_v4_complete(conn)

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

"""Forward-only migrations for the Phase 3 metadata registry."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from typing import Callable

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


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _apply_v1}


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

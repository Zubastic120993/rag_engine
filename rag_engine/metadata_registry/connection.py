"""SQLite connection helpers — explicit path, foreign keys ON, no import side effects."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rag_engine.metadata_registry.exceptions import (
    ExplicitPathRequiredError,
    ExistingDatabaseError,
    InvalidDatabasePathError,
    MissingDatabaseError,
    ParentDirectoryMissingError,
)


def normalize_db_path(db_path: str | Path | None) -> Path:
    """Require an explicit path and return a resolved absolute Path."""
    if db_path is None or str(db_path).strip() == "":
        raise ExplicitPathRequiredError("explicit registry database path is required")
    candidate = Path(db_path).expanduser()
    if not candidate.is_absolute():
        # Resolve relative paths against CWD for temp-test convenience, but
        # still require the resolved result to be absolute.
        candidate = candidate.resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.is_absolute():
        raise InvalidDatabasePathError("registry database path must resolve to an absolute path")
    return candidate


def ensure_parent_exists(path: Path) -> None:
    if not path.parent.exists():
        raise ParentDirectoryMissingError(f"parent directory does not exist: {path.parent}")


def open_registry(
    db_path: str | Path | None,
    *,
    readonly: bool = False,
    create: bool = False,
    fail_if_exists: bool = False,
) -> sqlite3.Connection:
    """Open a registry DB at an explicit path with ``PRAGMA foreign_keys=ON``.

    Does not use production defaults. Callers must pass ``db_path``.
    """
    path = normalize_db_path(db_path)
    if create:
        if fail_if_exists and path.exists():
            raise ExistingDatabaseError(f"database already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        if not path.exists():
            raise MissingDatabaseError(f"database does not exist: {path}")

    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path))

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# Scaffold-compatible alias
connect_registry = open_registry

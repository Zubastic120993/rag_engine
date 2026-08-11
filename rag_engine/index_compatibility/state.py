"""Authoritative fingerprint state load / atomic write (sidecar + registry)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from rag_engine.index_compatibility.constants import (
    DEFAULT_PHYSICAL_COLLECTION,
    FINGERPRINT_SCHEMA_VERSION,
    SIDECAR_V1_NAME,
)
from rag_engine.index_compatibility.exceptions import (
    FingerprintConflictError,
    FingerprintCorruptError,
    FingerprintUnsupportedVersionError,
)
from rag_engine.index_compatibility.specs import StoredIndexFingerprint


def sidecar_v1_path(persist: str | Path) -> Path:
    return Path(persist) / SIDECAR_V1_NAME


def _atomic_write_json(path: Path, payload: MappingLike) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# typing alias without importing Mapping at runtime cycle risk
MappingLike = dict[str, Any]


def read_sidecar_v1(
    persist: str | Path,
    *,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
) -> StoredIndexFingerprint | None:
    """Read interim sidecar authority. Missing → None. Malformed raises."""
    path = sidecar_v1_path(persist)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FingerprintCorruptError(
            f"sidecar v1 unreadable/invalid JSON: {path}",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise FingerprintCorruptError("sidecar v1 root must be object")
    stored = StoredIndexFingerprint.from_mapping(data, source="sidecar_v1")
    if stored.physical_collection_name != physical_collection_name:
        raise FingerprintCorruptError(
            "sidecar v1 collection binding mismatch",
            details={
                "expected": physical_collection_name,
                "stored": stored.physical_collection_name,
            },
        )
    return stored


def write_sidecar_v1(persist: str | Path, envelope: dict[str, Any]) -> Path:
    """Atomic write of interim sidecar authority (new-index / compatible only)."""
    # Validate before write.
    StoredIndexFingerprint.from_mapping(envelope, source="sidecar_v1")
    path = sidecar_v1_path(persist)
    _atomic_write_json(path, envelope)
    return path


def read_registry_fingerprint(
    registry_db: str | Path | None,
    *,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
) -> StoredIndexFingerprint | None:
    """Read registry authority row if DB/table/row exist. Never creates DB."""
    if registry_db is None:
        return None
    path = Path(registry_db)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_fingerprints'"
        ).fetchone()
        if exists is None:
            return None
        row = conn.execute(
            "SELECT payload_json FROM index_fingerprints "
            "WHERE physical_collection_name = ?",
            (physical_collection_name,),
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise FingerprintCorruptError(
                "registry fingerprint payload is not valid JSON",
                details={"path": str(path)},
            ) from exc
        return StoredIndexFingerprint.from_mapping(data, source="registry")
    finally:
        conn.close()


def write_registry_fingerprint(
    registry_db: str | Path,
    envelope: dict[str, Any],
) -> None:
    """Transactional upsert of registry fingerprint authority (test/new DB only)."""
    StoredIndexFingerprint.from_mapping(envelope, source="registry")
    path = Path(registry_db)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        # Ensure table exists (schema v3). Fail if migrations not applied.
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_fingerprints'"
        ).fetchone()
        if exists is None:
            conn.rollback()
            raise FingerprintCorruptError(
                "index_fingerprints table missing; migrate registry to schema v3 first"
            )
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO index_fingerprints (
                physical_collection_name,
                fingerprint_schema_version,
                index_fingerprint,
                embedding_fingerprint,
                corpus_fingerprint,
                payload_json,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(physical_collection_name) DO UPDATE SET
                fingerprint_schema_version=excluded.fingerprint_schema_version,
                index_fingerprint=excluded.index_fingerprint,
                embedding_fingerprint=excluded.embedding_fingerprint,
                corpus_fingerprint=excluded.corpus_fingerprint,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                envelope["physical_collection_name"],
                envelope["fingerprint_schema_version"],
                envelope["index_fingerprint"],
                envelope["embedding_fingerprint"],
                envelope["corpus_fingerprint"],
                payload,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_authoritative_state(
    persist: str | Path,
    *,
    registry_db: str | Path | None = None,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
) -> StoredIndexFingerprint | None:
    """Load authority with conflict detection.

    Rules (Phase 6A §13):
    - Registry row (if present) is authoritative.
    - Sidecar v1 is interim authority when registry has no row.
    - If both present and disagree → CONFLICT (raise).
    - Sidecar v0 is never consulted here.
    - Missing both → None (caller classifies UNKNOWN_LEGACY / EMPTY).
    - Read paths never rewrite mirrors.
    """
    registry_fp: StoredIndexFingerprint | None = None
    sidecar_fp: StoredIndexFingerprint | None = None

    try:
        registry_fp = read_registry_fingerprint(
            registry_db, physical_collection_name=physical_collection_name
        )
    except FingerprintUnsupportedVersionError:
        raise
    except FingerprintCorruptError:
        raise

    try:
        sidecar_fp = read_sidecar_v1(
            persist, physical_collection_name=physical_collection_name
        )
    except FingerprintUnsupportedVersionError:
        raise
    except FingerprintCorruptError:
        raise

    if registry_fp is not None and sidecar_fp is not None:
        if registry_fp.index_fingerprint != sidecar_fp.index_fingerprint:
            raise FingerprintConflictError(
                "registry and sidecar v1 fingerprint disagree",
                details={
                    "registry_ifp": registry_fp.index_fingerprint,
                    "sidecar_ifp": sidecar_fp.index_fingerprint,
                    "registry_source": registry_fp.source,
                    "sidecar_source": sidecar_fp.source,
                },
            )
        # Agree — registry is authority.
        return registry_fp

    if registry_fp is not None:
        return registry_fp
    return sidecar_fp


def initialize_fingerprint_state(
    persist: str | Path,
    envelope: dict[str, Any],
    *,
    registry_db: str | Path | None = None,
    write_registry: bool = False,
) -> StoredIndexFingerprint:
    """Establish authority for a proven-empty new index.

    Writes sidecar v1 atomically. Optionally writes registry when requested
    and a registry DB path is provided. Never called for non-empty legacy.
    """
    stored = StoredIndexFingerprint.from_mapping(envelope, source="sidecar_v1")
    write_sidecar_v1(persist, envelope)
    if write_registry and registry_db is not None:
        write_registry_fingerprint(registry_db, envelope)
        stored = StoredIndexFingerprint.from_mapping(envelope, source="registry")
    return stored

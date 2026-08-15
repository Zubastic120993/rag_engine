"""Append operation journal ? recovery evidence only, not identity authority."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag_engine.certified_generation.exceptions import AppendRecoveryError
from rag_engine.certified_generation.tracker import read_tracker, write_tracker

JOURNAL_DIR_NAME = "certified_append_journal"


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def journal_root(persist_dir: str | Path) -> Path:
    return Path(persist_dir) / JOURNAL_DIR_NAME


def new_operation_id() -> str:
    return f"capp:{_utc()}:{uuid.uuid4().hex[:12]}"


def write_journal(persist_dir: str | Path, record: dict[str, Any]) -> Path:
    root = journal_root(persist_dir)
    root.mkdir(parents=True, exist_ok=True)
    op_id = str(record["operation_id"])
    path = root / f"{op_id}.json"
    tmp = path.with_suffix(".json.tmp")
    data = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_journal(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def snapshot_tracker(persist_dir: str | Path, operation_id: str) -> Path:
    root = journal_root(persist_dir)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{operation_id}.tracker.bak.json"
    tracker = read_tracker(persist_dir)
    data = json.dumps(tracker, indent=2, ensure_ascii=False) + "\n"
    tmp = dest.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
    return dest


def restore_tracker_snapshot(persist_dir: str | Path, snapshot_path: str | Path) -> None:
    data = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AppendRecoveryError("tracker snapshot corrupt")
    write_tracker(persist_dir, data)


def delete_chroma_ids(db: Any, ids: list[str]) -> None:
    """Delete only append-created IDs. Never broad-delete."""
    if not ids:
        return
    db.delete(ids=list(ids))


def compensating_registry_delete(
    conn: Any,
    *,
    chunk_ids: list[str],
    document_ids: list[str],
    alias_rows: list[tuple[str, str]],
) -> None:
    """Best-effort reverse of append-created registry rows.

    Alias rows are (document_id, relative_path). Document versions are removed
    only when they have no remaining aliases and no remaining chunks.
    Deletes dependents first to satisfy ON DELETE RESTRICT foreign keys.
    """
    for cid in chunk_ids:
        conn.execute("DELETE FROM chunk_vector_map WHERE chunk_id = ?", (cid,))
        conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (cid,))
    for document_id, rel in alias_rows:
        conn.execute(
            "DELETE FROM source_files WHERE document_id = ? AND relative_path = ?",
            (document_id, rel),
        )
    for document_id in document_ids:
        # Also clear any chunks/maps for this document not listed (safety).
        leftover = conn.execute(
            "SELECT chunk_id FROM chunks WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        for row in leftover:
            cid = row["chunk_id"] if hasattr(row, "keys") else row[0]
            conn.execute("DELETE FROM chunk_vector_map WHERE chunk_id = ?", (cid,))
            conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (cid,))
        conn.execute(
            "DELETE FROM document_lifecycle_events WHERE document_id = ? "
            "OR related_document_id = ?",
            (document_id, document_id),
        )
        conn.execute(
            "DELETE FROM document_version_relations WHERE source_document_id = ? "
            "OR target_document_id = ?",
            (document_id, document_id),
        )
        remaining_aliases = conn.execute(
            "SELECT COUNT(*) FROM source_files WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
        remaining_chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
        if remaining_aliases == 0 and remaining_chunks == 0:
            row = conn.execute(
                "SELECT subject_id, source_hash FROM document_versions WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            conn.execute(
                "DELETE FROM document_versions WHERE document_id = ?",
                (document_id,),
            )
            if row is not None:
                subject_id = row[0] if not hasattr(row, "keys") else row["subject_id"]
                still = conn.execute(
                    "SELECT COUNT(*) FROM document_versions WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()[0]
                if still == 0:
                    conn.execute(
                        "DELETE FROM documents WHERE subject_id = ?",
                        (subject_id,),
                    )


def copy_file_backup(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)

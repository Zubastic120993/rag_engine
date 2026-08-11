"""Read-only Chroma persistence inspection via SQLite URI mode.

Never opens a writable chromadb client against production. Embeddings
(vector arrays) are not loaded — only embedding_id + selected metadata.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from rag_engine.reconciliation.models import ChromaRecord

# Metadata keys used for identity/locator reconciliation (exclude document text).
_IDENTITY_META_KEYS = ("source", "page", "collection", "probe")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class ChromaReadError(ValueError):
    """Raised when Chroma SQLite cannot be inspected read-only."""


def open_chroma_sqlite_readonly(path: str | Path) -> sqlite3.Connection:
    """Open chroma.sqlite3 with ``mode=ro`` URI — no writable connection."""
    p = Path(path).resolve()
    if not p.is_file():
        raise ChromaReadError(f"chroma sqlite does not exist: {p}")
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ChromaReadError(f"cannot open chroma sqlite read-only: {exc}") from exc
    conn.row_factory = sqlite3.Row
    # Defensive: even on RO connections.
    conn.execute("PRAGMA query_only = ON")
    return conn


def _meta_value(row: sqlite3.Row) -> Any:
    if row["string_value"] is not None:
        return row["string_value"]
    if row["int_value"] is not None:
        return int(row["int_value"])
    if row["float_value"] is not None:
        return float(row["float_value"])
    if row["bool_value"] is not None:
        return bool(row["bool_value"])
    return None


def load_chroma_snapshot_readonly(
    chroma_sqlite_path: str | Path,
    *,
    include_document_text: bool = False,
) -> dict[str, ChromaRecord]:
    """Load all Chroma embedding IDs + locator metadata (read-only).

    Parameters
    ----------
    chroma_sqlite_path:
        Path to ``chroma.sqlite3`` (or a copied sqlite file).
    include_document_text:
        If True, also load ``chroma:document`` (expensive). Default False.
    """
    conn = open_chroma_sqlite_readonly(chroma_sqlite_path)
    try:
        collections = {
            str(r["id"]): str(r["name"])
            for r in conn.execute("SELECT id, name FROM collections")
        }
        # Map segment → collection via segments table
        segment_to_collection: dict[str, str] = {}
        try:
            for r in conn.execute("SELECT id, collection FROM segments"):
                coll_id = str(r["collection"])
                segment_to_collection[str(r["id"])] = collections.get(
                    coll_id, coll_id
                )
        except sqlite3.Error:
            # Fall back: single known collection name if present
            default_name = next(iter(collections.values()), "unknown")
            for r in conn.execute("SELECT DISTINCT segment_id FROM embeddings"):
                segment_to_collection[str(r["segment_id"])] = default_name

        keys = list(_IDENTITY_META_KEYS)
        if include_document_text:
            keys.append("chroma:document")

        # Batch metadata for identity keys only
        placeholders = ",".join("?" * len(keys))
        meta_by_row_id: dict[int, dict[str, Any]] = {}
        for r in conn.execute(
            f"SELECT id, key, string_value, int_value, float_value, bool_value "
            f"FROM embedding_metadata WHERE key IN ({placeholders})",
            keys,
        ):
            rid = int(r["id"])
            meta_by_row_id.setdefault(rid, {})[str(r["key"])] = _meta_value(r)

        out: dict[str, ChromaRecord] = {}
        for r in conn.execute(
            "SELECT id, segment_id, embedding_id FROM embeddings ORDER BY embedding_id"
        ):
            eid = str(r["embedding_id"])
            rid = int(r["id"])
            seg = str(r["segment_id"])
            physical = segment_to_collection.get(seg, "unknown")
            meta = meta_by_row_id.get(rid, {})
            # Drop heavy document text from retained metadata map for identity
            light_meta = {k: v for k, v in meta.items() if k != "chroma:document"}
            out[eid] = ChromaRecord(
                chroma_embedding_id=eid,
                physical_collection_name=physical,
                source_path=str(meta["source"]) if meta.get("source") is not None else None,
                page=meta.get("page"),
                collection_meta=(
                    str(meta["collection"]) if meta.get("collection") is not None else None
                ),
                metadata=light_meta,
            )
        return out
    finally:
        conn.close()


def audit_chroma(records: dict[str, ChromaRecord]) -> dict[str, Any]:
    """Deterministic Chroma snapshot audit."""
    collections: dict[str, int] = {}
    uuid_like = 0
    non_uuid = 0
    missing_source = 0
    missing_page = 0
    probe = 0
    for rec in records.values():
        collections[rec.physical_collection_name] = (
            collections.get(rec.physical_collection_name, 0) + 1
        )
        if _UUID_RE.match(rec.chroma_embedding_id):
            uuid_like += 1
        else:
            non_uuid += 1
        if not rec.source_path:
            missing_source += 1
        if rec.page is None:
            missing_page += 1
        if rec.metadata.get("probe") is not None:
            probe += 1

    meta_fields: set[str] = set()
    for rec in records.values():
        meta_fields.update(rec.metadata.keys())

    return {
        "chroma_records": len(records),
        "collections": dict(sorted(collections.items())),
        "uuid_like_ids": uuid_like,
        "non_uuid_ids": non_uuid,
        "missing_source_metadata": missing_source,
        "missing_page_metadata": missing_page,
        "probe_records": probe,
        "metadata_fields_observed": sorted(meta_fields),
    }


def chroma_ids_set(records: dict[str, ChromaRecord]) -> set[str]:
    return set(records.keys())

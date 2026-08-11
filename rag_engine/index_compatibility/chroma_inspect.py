"""Read-only Chroma / persist-dir inspection helpers (no mutations)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rag_engine.index_compatibility.constants import DEFAULT_PHYSICAL_COLLECTION


def chroma_sqlite_path(persist: str | Path) -> Path:
    return Path(persist) / "chroma.sqlite3"


def count_vectors_readonly(
    persist: str | Path,
    *,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
) -> int:
    """Count embedding rows for a collection via read-only SQLite.

    Never opens a writable chromadb client. Returns 0 if DB/collection missing.
    """
    db = chroma_sqlite_path(persist)
    if not db.is_file():
        return 0
    uri = f"file:{db.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return 0
    try:
        row = conn.execute(
            "SELECT id FROM collections WHERE name = ? LIMIT 1",
            (physical_collection_name,),
        ).fetchone()
        if row is None:
            # Fall back to any single collection if name differs.
            rows = conn.execute("SELECT id, name FROM collections").fetchall()
            if len(rows) == 1:
                collection_id = rows[0][0]
            else:
                return 0
        else:
            collection_id = row[0]
        # chromadb stores embeddings in embeddings table keyed by segment;
        # count via embeddings join segments for this collection.
        count_row = conn.execute(
            """
            SELECT COUNT(*) FROM embeddings e
            JOIN segments s ON e.segment_id = s.id
            WHERE s.collection = ?
            """,
            (collection_id,),
        ).fetchone()
        if count_row is not None:
            return int(count_row[0] or 0)
        return 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def collection_dimension_readonly(
    persist: str | Path,
    *,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
) -> int | None:
    """Best-effort read of stored vector dimension; None if unknown."""
    db = chroma_sqlite_path(persist)
    if not db.is_file():
        return None
    uri = f"file:{db.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        # Prefer embedding_metadata / embeddings shape if available.
        # chromadb 0.5 stores vector length implicitly; try collection metadata.
        row = conn.execute(
            "SELECT id FROM collections WHERE name = ? LIMIT 1",
            (physical_collection_name,),
        ).fetchone()
        if row is None:
            return None
        # Sample one embedding vector blob length via embedding_fulltext / not reliable.
        # Use embeddings table if vector column exists — often not exposed as float array.
        # Fall back to segment metadata JSON if present.
        meta = conn.execute(
            """
            SELECT str_value FROM collection_metadata
            WHERE collection_id = ? AND key = 'hnsw:space'
            """,
            (row[0],),
        ).fetchone()
        _ = meta  # space is not dimension
        # Try reading from embeddings table schema for dimension via a probe.
        try:
            probe = conn.execute(
                """
                SELECT length(vector) FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                WHERE s.collection = ?
                LIMIT 1
                """,
                (row[0],),
            ).fetchone()
            if probe and probe[0]:
                # vector may be float32 blob: 4 bytes per dim
                nbytes = int(probe[0])
                if nbytes > 0 and nbytes % 4 == 0:
                    return nbytes // 4
        except sqlite3.Error:
            pass
        return None
    except sqlite3.Error:
        return None
    finally:
        conn.close()

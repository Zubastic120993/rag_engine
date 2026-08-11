"""Optional read-only registry snapshot loader (temp DBs / explicit paths only)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from rag_engine.reconciliation.models import RegistrySnapshotRecord


class RegistrySnapshotError(ValueError):
    """Raised when a registry snapshot cannot be loaded read-only."""


def load_registry_snapshot_readonly(
    db_path: str | Path,
) -> list[RegistrySnapshotRecord]:
    """Load minimal identity rows from an explicit registry SQLite path.

    Uses ``mode=ro``. Does not create databases. Does not use production defaults.
    """
    p = Path(db_path).resolve()
    if not p.is_file():
        raise RegistrySnapshotError(f"registry database does not exist: {p}")

    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        # Discover available tables
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "document_versions" not in tables:
            return []

        versions = conn.execute(
            "SELECT document_id, subject_id, source_hash FROM document_versions "
            "ORDER BY document_id"
        ).fetchall()

        chunks_by_doc: dict[str, list[str]] = {}
        if "chunks" in tables:
            for r in conn.execute(
                "SELECT document_id, chunk_id FROM chunks ORDER BY chunk_id"
            ):
                chunks_by_doc.setdefault(str(r["document_id"]), []).append(
                    str(r["chunk_id"])
                )

        maps_by_chunk: dict[str, list[str]] = {}
        if "chunk_vector_map" in tables:
            for r in conn.execute(
                "SELECT chunk_id, chroma_embedding_id FROM chunk_vector_map "
                "ORDER BY chroma_embedding_id"
            ):
                maps_by_chunk.setdefault(str(r["chunk_id"]), []).append(
                    str(r["chroma_embedding_id"])
                )

        paths_by_doc: dict[str, list[str]] = {}
        if "source_files" in tables:
            for r in conn.execute(
                "SELECT document_id, relative_path FROM source_files "
                "ORDER BY relative_path"
            ):
                paths_by_doc.setdefault(str(r["document_id"]), []).append(
                    str(r["relative_path"])
                )

        out: list[RegistrySnapshotRecord] = []
        for v in versions:
            doc_id = str(v["document_id"])
            chunk_ids = tuple(chunks_by_doc.get(doc_id, []))
            chroma_ids: list[str] = []
            for cid in chunk_ids:
                chroma_ids.extend(maps_by_chunk.get(cid, []))
            out.append(
                RegistrySnapshotRecord(
                    subject_id=str(v["subject_id"]) if v["subject_id"] else None,
                    document_id=doc_id,
                    source_hash=str(v["source_hash"]) if v["source_hash"] else None,
                    chunk_ids=chunk_ids,
                    chroma_embedding_ids=tuple(sorted(set(chroma_ids))),
                    source_paths=tuple(paths_by_doc.get(doc_id, [])),
                )
            )
        return out
    finally:
        conn.close()


def registry_audit(records: list[RegistrySnapshotRecord]) -> dict[str, Any]:
    return {
        "registry_documents": len(records),
        "registry_chunks": sum(len(r.chunk_ids) for r in records),
        "registry_vector_mappings": sum(len(r.chroma_embedding_ids) for r in records),
    }

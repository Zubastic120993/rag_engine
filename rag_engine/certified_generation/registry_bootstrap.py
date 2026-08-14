"""Explicit-path registry bootstrap for a certified generation (never production)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from rag_engine.certified_generation.exceptions import UnsafeGenerationPathError
from rag_engine.certified_generation.paths import registry_path_is_production
from rag_engine.metadata_registry import (
    initialize_registry,
    open_registry,
    register_chunk,
    register_document_version,
    register_source_file,
    register_subject,
    register_vector_mapping,
    registry_transaction,
)
from rag_engine.stable_identity.constants import MAPPING_STATUS_NATIVE_CHUNK_ID
from rag_engine.stable_identity.ids import subject_id_pending
from rag_engine.index_compatibility.state import write_registry_fingerprint


def assert_temp_registry_path(registry_db: str | Path) -> Path:
    path = Path(registry_db).expanduser().resolve()
    if registry_path_is_production(path):
        raise UnsafeGenerationPathError(
            "refusing production registry path",
            details={"path": str(path)},
        )
    return path


def bootstrap_registry_db(registry_db: str | Path) -> Path:
    path = assert_temp_registry_path(registry_db)
    return initialize_registry(path)


def bootstrap_certified_documents(
    registry_db: str | Path,
    units: Iterable[Mapping[str, Any]],
    *,
    chunks_by_document: Mapping[str, list[Mapping[str, Any]]] | None = None,
    embedding_generation_id: str | None = None,
    v1_envelope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Register pending subjects, versions, aliases, chunks, native vector maps."""
    path = assert_temp_registry_path(registry_db)
    if not path.is_file():
        bootstrap_registry_db(path)
    conn = open_registry(path)
    counts = {"subjects": 0, "versions": 0, "aliases": 0, "chunks": 0, "maps": 0}
    try:
        with registry_transaction(conn):
            for unit in units:
                document_id = str(unit["document_id"])
                source_hash = str(unit["source_hash"])
                subject_id = subject_id_pending(source_hash)
                register_subject(conn, subject_id=subject_id)
                register_document_version(
                    conn,
                    document_id=document_id,
                    subject_id=subject_id,
                    source_hash=source_hash,
                )
                counts["subjects"] += 1
                counts["versions"] += 1
                for alias in unit.get("aliases") or [unit.get("display_source")]:
                    if not alias:
                        continue
                    register_source_file(
                        conn,
                        document_id=document_id,
                        relative_path=str(alias),
                        source_hash=source_hash,
                        collection=unit.get("collection"),
                    )
                    counts["aliases"] += 1
                for spec in (chunks_by_document or {}).get(document_id, []):
                    register_chunk(
                        conn,
                        chunk_id=str(spec["chunk_id"]),
                        document_id=document_id,
                        chunking_fingerprint=str(spec["chunking_fingerprint"]),
                        ordinal=int(spec["ordinal"]),
                        content_hash=spec.get("content_hash"),
                        page=spec.get("page") if isinstance(spec.get("page"), int) else None,
                    )
                    register_vector_mapping(
                        conn,
                        chunk_id=str(spec["chunk_id"]),
                        chroma_embedding_id=str(spec["chunk_id"]),
                        mapping_status=MAPPING_STATUS_NATIVE_CHUNK_ID,
                    )
                    counts["chunks"] += 1
                    counts["maps"] += 1
        if v1_envelope is not None:
            write_registry_fingerprint(path, dict(v1_envelope))
    finally:
        conn.close()
    return {"registry_db": str(path), "counts": counts, "embedding_generation_id": embedding_generation_id}

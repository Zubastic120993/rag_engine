"""Additive chunk / vector-map tables for temp-registry testing (Phase 2).

SUPERSEDED for Phase 3 authoritative schema by
``rag_engine.metadata_registry`` (stable-id-v1 terminology:
``documents.subject_id``, ``document_versions.document_id``).

These helpers remain for Phase 2 unit tests only. New work must use
``metadata_registry``. Production ``.rag_state`` must not be created here.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from rag_engine.stable_identity.collision import (
    IdentityCollisionError,
    reject_conflicting_chunk_reuse,
)
from rag_engine.stable_identity.constants import (
    IDENTITY_SCHEME_VERSION,
    MAPPING_STATUSES,
    MAPPING_STATUS_LEGACY_UUID,
)
from rag_engine.stable_identity.validation import (
    IdentityValidationError,
    validate_chunk_id,
    validate_chunking_fingerprint,
    validate_content_hash,
    validate_document_id,
)

CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY NOT NULL,
    document_id TEXT NOT NULL,
    -- Stores Phase-1 document_id (docrev:…). Not the Phase 3 metadata_registry schema.
    identity_scheme_version TEXT NOT NULL,
    chunking_fingerprint TEXT NOT NULL,
    chunk_ordinal INTEGER NOT NULL,
    content_hash TEXT,
    page INTEGER,
    created_at TEXT,
    UNIQUE (document_id, chunking_fingerprint, chunk_ordinal, identity_scheme_version)
);
"""

CHUNK_VECTOR_MAP_DDL = """
CREATE TABLE IF NOT EXISTS chunk_vector_map (
    chunk_id TEXT NOT NULL,
    physical_collection_name TEXT NOT NULL,
    chroma_embedding_id TEXT NOT NULL,
    vector_store TEXT NOT NULL DEFAULT 'chroma',
    mapping_status TEXT NOT NULL,
    identity_scheme_version TEXT NOT NULL,
    PRIMARY KEY (chunk_id, physical_collection_name),
    UNIQUE (physical_collection_name, chroma_embedding_id),
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id)
);
"""


def apply_stable_identity_tables(conn: sqlite3.Connection) -> None:
    """Create additive ``chunks`` / ``chunk_vector_map`` tables (idempotent)."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(CHUNKS_DDL + CHUNK_VECTOR_MAP_DDL)
    conn.commit()


def insert_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    document_id: str,
    chunking_fingerprint: str,
    ordinal: int,
    content_hash: str | None = None,
    page: int | None = None,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
    created_at: str | None = None,
) -> None:
    """Insert a chunk row; identical reuse is idempotent; conflicts fail hard."""
    validate_chunk_id(chunk_id)
    validate_document_id(document_id)
    validate_chunking_fingerprint(chunking_fingerprint)
    if content_hash is not None:
        validate_content_hash(content_hash)

    row = conn.execute(
        "SELECT chunk_id, document_id, chunking_fingerprint, chunk_ordinal AS ordinal, "
        "identity_scheme_version FROM chunks WHERE chunk_id = ?",
        (chunk_id,),
    ).fetchone()
    existing: Mapping[str, Any] | None
    if row is None:
        existing = None
    else:
        existing = {
            "chunk_id": row[0],
            "document_id": row[1],
            "chunking_fingerprint": row[2],
            "ordinal": row[3],
            "identity_scheme_version": row[4],
        }
    reject_conflicting_chunk_reuse(
        existing,
        chunk_id=chunk_id,
        document_id=document_id,
        chunking_fingerprint=chunking_fingerprint,
        ordinal=ordinal,
        identity_scheme_version=identity_scheme_version,
    )
    if existing is not None:
        return
    conn.execute(
        "INSERT INTO chunks ("
        "chunk_id, document_id, identity_scheme_version, chunking_fingerprint, "
        "chunk_ordinal, content_hash, page, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chunk_id,
            document_id,
            identity_scheme_version,
            chunking_fingerprint,
            ordinal,
            content_hash,
            page,
            created_at,
        ),
    )
    conn.commit()


def insert_chunk_vector_map(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    chroma_embedding_id: str,
    physical_collection_name: str = "langchain",
    vector_store: str = "chroma",
    mapping_status: str = MAPPING_STATUS_LEGACY_UUID,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
) -> None:
    """Map stable chunk_id ↔ legacy/native Chroma embedding id (temp DB only)."""
    validate_chunk_id(chunk_id)
    if not isinstance(chroma_embedding_id, str) or not chroma_embedding_id.strip():
        raise IdentityValidationError("chroma_embedding_id must be non-empty str")
    if mapping_status not in MAPPING_STATUSES:
        raise IdentityValidationError(
            f"mapping_status must be one of {sorted(MAPPING_STATUSES)}"
        )

    existing = conn.execute(
        "SELECT chunk_id, chroma_embedding_id, mapping_status, identity_scheme_version "
        "FROM chunk_vector_map WHERE chunk_id = ? AND physical_collection_name = ?",
        (chunk_id, physical_collection_name),
    ).fetchone()
    if existing is not None:
        if (
            existing[1] == chroma_embedding_id
            and existing[2] == mapping_status
            and existing[3] == identity_scheme_version
        ):
            return
        raise IdentityCollisionError(
            f"conflicting chunk_vector_map for {chunk_id!r} in "
            f"{physical_collection_name!r}: existing chroma_embedding_id="
            f"{existing[1]!r} status={existing[2]!r}"
        )

    # Also reject another chunk claiming the same chroma id in this collection.
    other = conn.execute(
        "SELECT chunk_id FROM chunk_vector_map "
        "WHERE physical_collection_name = ? AND chroma_embedding_id = ?",
        (physical_collection_name, chroma_embedding_id),
    ).fetchone()
    if other is not None and other[0] != chunk_id:
        raise IdentityCollisionError(
            f"chroma_embedding_id {chroma_embedding_id!r} already mapped to {other[0]!r}"
        )

    conn.execute(
        "INSERT INTO chunk_vector_map ("
        "chunk_id, physical_collection_name, chroma_embedding_id, vector_store, "
        "mapping_status, identity_scheme_version"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (
            chunk_id,
            physical_collection_name,
            chroma_embedding_id,
            vector_store,
            mapping_status,
            identity_scheme_version,
        ),
    )
    conn.commit()

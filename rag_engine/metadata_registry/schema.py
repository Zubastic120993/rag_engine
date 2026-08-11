"""Phase 3 metadata registry schema — stable-id-v1 aligned.

Terminology (authoritative):
  documents.subject_id              = logical lineage (Phase-1 subject_id)
  document_versions.document_id     = immutable revision (Phase-1 document_id = docrev:<sha>)
  chunks.chunk_id                   = Phase-1 chunk_id
  chunk_vector_map.chroma_embedding_id = Chroma vector handle (legacy UUID4 or future chunk_id)

Old scaffold (branch) used:
  documents.document_id             ≈ subject_id (logical)
  document_versions.document_version_id ≈ document_id (revision)
That naming is NOT used here — production registry never existed, so Phase 3
aligns to stable-id-v1 vocabulary before authority activation.
"""

from __future__ import annotations

from typing import Final

# Fresh identity-aligned registry. Branch scaffold schema v1 was never production.
CURRENT_SCHEMA_VERSION: Final = 1

REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "registry_schema_version",
    "documents",
    "document_versions",
    "source_files",
    "chunks",
    "chunk_vector_map",
)

SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS registry_schema_version (
    schema_version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    status TEXT NOT NULL,
    description TEXT,
    backward_compatible INTEGER
);

CREATE TABLE IF NOT EXISTS documents (
    subject_id TEXT PRIMARY KEY NOT NULL,
    -- Phase-1 subject_id (logical lineage). Old scaffold called this document_id.
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    document_type TEXT,
    scope TEXT,
    canonical_title TEXT,
    document_number TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(document_type);
CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(scope);

CREATE TABLE IF NOT EXISTS document_versions (
    document_id TEXT PRIMARY KEY NOT NULL,
    -- Phase-1 document_id = docrev:<source_hash>. Old scaffold: document_version_id.
    subject_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    identity_scheme_version TEXT NOT NULL,
    content_hash TEXT,
    created_at TEXT NOT NULL,
    extractor TEXT,
    extractor_version TEXT,
    notes TEXT,
    FOREIGN KEY (subject_id) REFERENCES documents(subject_id) ON DELETE RESTRICT,
    UNIQUE (source_hash),
    CHECK (document_id = 'docrev:' || source_hash)
);
CREATE INDEX IF NOT EXISTS idx_document_versions_subject_id ON document_versions(subject_id);
CREATE INDEX IF NOT EXISTS idx_document_versions_source_hash ON document_versions(source_hash);

CREATE TABLE IF NOT EXISTS source_files (
    source_file_id TEXT PRIMARY KEY NOT NULL,
    document_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    source_hash TEXT,
    storage_root TEXT,
    collection TEXT,
    status TEXT,
    FOREIGN KEY (document_id) REFERENCES document_versions(document_id) ON DELETE RESTRICT,
    UNIQUE (document_id, relative_path)
);
CREATE INDEX IF NOT EXISTS idx_source_files_document_id ON source_files(document_id);
CREATE INDEX IF NOT EXISTS idx_source_files_relative_path ON source_files(relative_path);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY NOT NULL,
    document_id TEXT NOT NULL,
    identity_scheme_version TEXT NOT NULL,
    chunking_fingerprint TEXT NOT NULL,
    chunk_ordinal INTEGER NOT NULL,
    content_hash TEXT,
    page INTEGER,
    created_at TEXT NOT NULL,
    metadata_json TEXT,
    FOREIGN KEY (document_id) REFERENCES document_versions(document_id) ON DELETE RESTRICT,
    UNIQUE (
        document_id,
        chunking_fingerprint,
        chunk_ordinal,
        identity_scheme_version
    )
);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fingerprint ON chunks(chunking_fingerprint);

CREATE TABLE IF NOT EXISTS chunk_vector_map (
    chunk_id TEXT NOT NULL,
    physical_collection_name TEXT NOT NULL,
    chroma_embedding_id TEXT NOT NULL,
    vector_store TEXT NOT NULL DEFAULT 'chroma',
    mapping_status TEXT NOT NULL,
    identity_scheme_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, physical_collection_name),
    UNIQUE (physical_collection_name, chroma_embedding_id),
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE RESTRICT,
    CHECK (
        mapping_status IN ('legacy_uuid', 'native_chunk_id', 'pending')
    )
);
CREATE INDEX IF NOT EXISTS idx_chunk_vector_map_chroma_id
    ON chunk_vector_map(chroma_embedding_id);
"""

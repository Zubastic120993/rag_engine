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
# v2 adds Phase 5 revision lifecycle columns + event/relation tables.
# v3 adds Phase 6B index fingerprint authority table (embedding-fp-v1).
CURRENT_SCHEMA_VERSION: Final = 3

REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "registry_schema_version",
    "documents",
    "document_versions",
    "source_files",
    "chunks",
    "chunk_vector_map",
    "document_lifecycle_events",
    "document_version_relations",
    "index_fingerprints",
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

# Additive SQL applied when upgrading an existing v1 registry to v2.
# Unique ACTIVE index is created in _apply_v2 after multi-revision subjects
# are conservatively set to WITHDRAWN (no invented succession).
SCHEMA_SQL_V2_UPGRADE: Final[str] = """
ALTER TABLE document_versions ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE document_versions ADD COLUMN lifecycle_updated_at TEXT;

CREATE INDEX IF NOT EXISTS idx_document_versions_lifecycle
    ON document_versions(lifecycle_status);

CREATE TABLE IF NOT EXISTS document_lifecycle_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    previous_state TEXT,
    new_state TEXT NOT NULL,
    relation_type TEXT,
    related_document_id TEXT,
    reason TEXT,
    actor TEXT,
    source TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES document_versions(document_id) ON DELETE RESTRICT,
    FOREIGN KEY (related_document_id) REFERENCES document_versions(document_id)
        ON DELETE RESTRICT,
    CHECK (
        new_state IN (
            'ACTIVE', 'SUPERSEDED', 'ARCHIVED', 'REPLACED', 'WITHDRAWN', 'DUPLICATE'
        )
    )
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_document_id
    ON document_lifecycle_events(document_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_created_at
    ON document_lifecycle_events(created_at);

CREATE TABLE IF NOT EXISTS document_version_relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id TEXT NOT NULL,
    target_document_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    actor TEXT,
    source TEXT,
    UNIQUE (source_document_id, target_document_id, relation_type),
    FOREIGN KEY (source_document_id) REFERENCES document_versions(document_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (target_document_id) REFERENCES document_versions(document_id)
        ON DELETE RESTRICT,
    CHECK (
        relation_type IN ('supersedes', 'replaces', 'duplicate_of')
    ),
    CHECK (source_document_id != target_document_id)
);
CREATE INDEX IF NOT EXISTS idx_version_relations_source
    ON document_version_relations(source_document_id);
CREATE INDEX IF NOT EXISTS idx_version_relations_target
    ON document_version_relations(target_document_id);
"""

# Additive SQL applied when upgrading an existing v2 registry to v3.
SCHEMA_SQL_V3_UPGRADE: Final[str] = """
CREATE TABLE IF NOT EXISTS index_fingerprints (
    physical_collection_name TEXT PRIMARY KEY NOT NULL,
    fingerprint_schema_version TEXT NOT NULL,
    index_fingerprint TEXT NOT NULL,
    embedding_fingerprint TEXT NOT NULL,
    corpus_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_index_fingerprints_ifp
    ON index_fingerprints(index_fingerprint);
"""

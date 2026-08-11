"""Transactional registry write API — stable-id-v1 invariants enforced."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from rag_engine.metadata_registry.connection import open_registry
from rag_engine.metadata_registry.exceptions import (
    RegistryConflictError,
    RegistryValidationError,
)
from rag_engine.metadata_registry.migrations import utc_now
from rag_engine.stable_identity import (
    IDENTITY_SCHEME_VERSION,
    DOCUMENT_ID_PREFIX,
    IdentityCollisionError,
    IdentityValidationError,
    normalize_relative_path,
    reject_conflicting_chunk_reuse,
    validate_chunk_id,
    validate_chunking_fingerprint,
    validate_content_hash,
    validate_document_id,
    validate_source_hash,
    validate_subject_id,
)
from rag_engine.stable_identity.constants import (
    MAPPING_STATUSES,
    MAPPING_STATUS_LEGACY_UUID,
)


def make_source_file_id(*, document_id: str, relative_path: str) -> str:
    """Deterministic locator id (not a business document identity)."""
    token = f"{document_id}|{relative_path}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return f"src:{digest}"


@contextmanager
def registry_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction boundary; rollback on any exception."""
    already = conn.in_transaction
    if not already:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if not already:
            conn.commit()
    except Exception:
        if not already:
            conn.rollback()
        raise


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def register_subject(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    document_type: str | None = None,
    scope: str | None = None,
    canonical_title: str | None = None,
    document_number: str | None = None,
    notes: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Insert or idempotently accept an identical subject row."""
    try:
        validate_subject_id(subject_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc

    existing = conn.execute(
        "SELECT * FROM documents WHERE subject_id = ?", (subject_id,)
    ).fetchone()
    if existing is not None:
        # Idempotent if key fields match; conflict if descriptive fields diverge
        # when previously set to a different non-null value.
        for field, value in (
            ("document_type", document_type),
            ("scope", scope),
            ("canonical_title", canonical_title),
            ("document_number", document_number),
        ):
            if value is None:
                continue
            prev = existing[field]
            if prev is not None and prev != value:
                raise RegistryConflictError(
                    f"subject_id {subject_id!r} {field} conflict: {prev!r} vs {value!r}"
                )
        return _row_to_dict(existing)  # type: ignore[return-value]

    ts = created_at or utc_now()
    conn.execute(
        "INSERT INTO documents ("
        "subject_id, created_at, updated_at, document_type, scope, "
        "canonical_title, document_number, notes"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subject_id,
            ts,
            ts,
            document_type,
            scope,
            canonical_title,
            document_number,
            notes,
        ),
    )
    row = conn.execute(
        "SELECT * FROM documents WHERE subject_id = ?", (subject_id,)
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def register_document_version(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    subject_id: str,
    source_hash: str,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
    content_hash: str | None = None,
    extractor: str | None = None,
    extractor_version: str | None = None,
    notes: str | None = None,
    created_at: str | None = None,
    lifecycle_status: str | None = None,
) -> dict[str, Any]:
    """Register an immutable revision. Enforces document_id == docrev: + source_hash.

    Phase 5: ``lifecycle_status`` defaults to ACTIVE when the subject has no
    ACTIVE revision. If an ACTIVE revision already exists, callers must pass an
    explicit non-ACTIVE status (staging) and later call ``supersede_revision``.
    """
    from rag_engine.metadata_registry.lifecycle import (
        DEFAULT_LIFECYCLE_STATUS,
        resolve_initial_lifecycle_status,
    )

    try:
        validate_document_id(document_id)
        validate_subject_id(subject_id)
        validate_source_hash(source_hash)
        if content_hash is not None:
            validate_content_hash(content_hash)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc

    expected = f"{DOCUMENT_ID_PREFIX}{source_hash}"
    if document_id != expected:
        raise RegistryValidationError(
            f"document_id/source_hash invariant failed: {document_id!r} != {expected!r}"
        )
    if identity_scheme_version != IDENTITY_SCHEME_VERSION:
        # Allow only current scheme for Phase 3 writes; future schemes need migration.
        raise RegistryValidationError(
            f"unsupported identity_scheme_version: {identity_scheme_version!r}"
        )

    existing = conn.execute(
        "SELECT * FROM document_versions WHERE document_id = ?", (document_id,)
    ).fetchone()
    if existing is not None:
        if existing["source_hash"] != source_hash:
            raise RegistryConflictError(
                f"document_id {document_id!r} source_hash conflict"
            )
        if existing["subject_id"] != subject_id:
            raise RegistryConflictError(
                f"document_id {document_id!r} subject_id conflict: "
                f"{existing['subject_id']!r} vs {subject_id!r}"
            )
        if existing["identity_scheme_version"] != identity_scheme_version:
            raise RegistryConflictError(
                f"document_id {document_id!r} identity_scheme_version conflict"
            )
        return _row_to_dict(existing)  # type: ignore[return-value]

    # Same source_hash under a different document_id is impossible under the rule,
    # but UNIQUE(source_hash) also guards corruption.
    other = conn.execute(
        "SELECT document_id FROM document_versions WHERE source_hash = ?",
        (source_hash,),
    ).fetchone()
    if other is not None and other["document_id"] != document_id:
        raise RegistryConflictError(
            f"source_hash already bound to {other['document_id']!r}"
        )

    # Detect whether Phase 5 columns exist (schema v2+).
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(document_versions)").fetchall()
    }
    has_lifecycle = "lifecycle_status" in cols

    ts = created_at or utc_now()
    if has_lifecycle:
        status = resolve_initial_lifecycle_status(
            conn, subject_id=subject_id, requested=lifecycle_status
        )
        try:
            conn.execute(
                "INSERT INTO document_versions ("
                "document_id, subject_id, source_hash, identity_scheme_version, "
                "content_hash, created_at, extractor, extractor_version, notes, "
                "lifecycle_status, lifecycle_updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    subject_id,
                    source_hash,
                    identity_scheme_version,
                    content_hash,
                    ts,
                    extractor,
                    extractor_version,
                    notes,
                    status,
                    ts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RegistryValidationError(
                f"document_versions insert failed integrity check: {exc}"
            ) from exc
        # Initial creation event (append-only).
        conn.execute(
            "INSERT INTO document_lifecycle_events ("
            "document_id, previous_state, new_state, relation_type, "
            "related_document_id, reason, actor, source, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                None,
                status,
                None,
                None,
                "initial_registration",
                None,
                "register_document_version",
                ts,
            ),
        )
    else:
        if lifecycle_status is not None and lifecycle_status != DEFAULT_LIFECYCLE_STATUS:
            raise RegistryValidationError(
                "lifecycle_status requires schema v2; migrate the registry first"
            )
        try:
            conn.execute(
                "INSERT INTO document_versions ("
                "document_id, subject_id, source_hash, identity_scheme_version, "
                "content_hash, created_at, extractor, extractor_version, notes"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    subject_id,
                    source_hash,
                    identity_scheme_version,
                    content_hash,
                    ts,
                    extractor,
                    extractor_version,
                    notes,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RegistryValidationError(
                f"document_versions insert failed integrity check: {exc}"
            ) from exc

    row = conn.execute(
        "SELECT * FROM document_versions WHERE document_id = ?", (document_id,)
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def register_source_file(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    relative_path: str,
    library_root: str | Path | None = None,
    source_hash: str | None = None,
    storage_root: str | None = None,
    collection: str | None = None,
    status: str | None = None,
    source_file_id: str | None = None,
    first_seen_at: str | None = None,
    last_seen_at: str | None = None,
) -> dict[str, Any]:
    """Register a mutable path locator for a document revision."""
    try:
        validate_document_id(document_id)
        if source_hash is not None:
            validate_source_hash(source_hash)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc

    try:
        norm = normalize_relative_path(relative_path, library_root=library_root)
    except Exception as exc:
        raise RegistryValidationError(f"invalid source path: {exc}") from exc

    filename = Path(norm).name
    sid = source_file_id or make_source_file_id(
        document_id=document_id, relative_path=norm
    )
    ts = first_seen_at or utc_now()
    last = last_seen_at or ts

    existing = conn.execute(
        "SELECT * FROM source_files WHERE document_id = ? AND relative_path = ?",
        (document_id, norm),
    ).fetchone()
    if existing is not None:
        if existing["source_file_id"] != sid and source_file_id is not None:
            raise RegistryConflictError(
                f"locator id conflict for {norm!r}: "
                f"{existing['source_file_id']!r} vs {sid!r}"
            )
        # Idempotent: bump last_seen_at only
        conn.execute(
            "UPDATE source_files SET last_seen_at = ? WHERE source_file_id = ?",
            (last, existing["source_file_id"]),
        )
        row = conn.execute(
            "SELECT * FROM source_files WHERE source_file_id = ?",
            (existing["source_file_id"],),
        ).fetchone()
        return _row_to_dict(row)  # type: ignore[return-value]

    by_id = conn.execute(
        "SELECT * FROM source_files WHERE source_file_id = ?", (sid,)
    ).fetchone()
    if by_id is not None:
        raise RegistryConflictError(
            f"source_file_id {sid!r} already used for a different locator"
        )

    try:
        conn.execute(
            "INSERT INTO source_files ("
            "source_file_id, document_id, relative_path, filename, "
            "first_seen_at, last_seen_at, source_hash, storage_root, collection, status"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                document_id,
                norm,
                filename,
                ts,
                last,
                source_hash,
                storage_root,
                collection,
                status,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryValidationError(
            f"source_files insert failed integrity check: {exc}"
        ) from exc

    row = conn.execute(
        "SELECT * FROM source_files WHERE source_file_id = ?", (sid,)
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def register_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    document_id: str,
    chunking_fingerprint: str,
    ordinal: int,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
    content_hash: str | None = None,
    page: int | None = None,
    metadata_json: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Register a chunk; verifies chunk_id against constituents."""
    try:
        validate_chunk_id(chunk_id)
        validate_document_id(document_id)
        validate_chunking_fingerprint(chunking_fingerprint)
        if content_hash is not None:
            validate_content_hash(content_hash)
        reject_conflicting_chunk_reuse(
            None,
            chunk_id=chunk_id,
            document_id=document_id,
            chunking_fingerprint=chunking_fingerprint,
            ordinal=ordinal,
            identity_scheme_version=identity_scheme_version,
        )
    except (IdentityValidationError, IdentityCollisionError) as exc:
        raise RegistryValidationError(str(exc)) from exc

    existing = conn.execute(
        "SELECT chunk_id, document_id, chunking_fingerprint, "
        "chunk_ordinal AS ordinal, identity_scheme_version, content_hash, page "
        "FROM chunks WHERE chunk_id = ?",
        (chunk_id,),
    ).fetchone()
    if existing is not None:
        try:
            reject_conflicting_chunk_reuse(
                {
                    "chunk_id": existing["chunk_id"],
                    "document_id": existing["document_id"],
                    "chunking_fingerprint": existing["chunking_fingerprint"],
                    "ordinal": existing["ordinal"],
                    "identity_scheme_version": existing["identity_scheme_version"],
                },
                chunk_id=chunk_id,
                document_id=document_id,
                chunking_fingerprint=chunking_fingerprint,
                ordinal=ordinal,
                identity_scheme_version=identity_scheme_version,
            )
        except IdentityCollisionError as exc:
            raise RegistryConflictError(str(exc)) from exc
        return _row_to_dict(
            conn.execute(
                "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
        )  # type: ignore[return-value]

    ts = created_at or utc_now()
    try:
        conn.execute(
            "INSERT INTO chunks ("
            "chunk_id, document_id, identity_scheme_version, chunking_fingerprint, "
            "chunk_ordinal, content_hash, page, created_at, metadata_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                document_id,
                identity_scheme_version,
                chunking_fingerprint,
                ordinal,
                content_hash,
                page,
                ts,
                metadata_json,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryValidationError(
            f"chunks insert failed integrity check: {exc}"
        ) from exc

    row = conn.execute(
        "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def register_vector_mapping(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    chroma_embedding_id: str,
    physical_collection_name: str = "langchain",
    vector_store: str = "chroma",
    mapping_status: str = MAPPING_STATUS_LEGACY_UUID,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Map stable chunk_id ↔ Chroma embedding id (legacy UUID or native)."""
    try:
        validate_chunk_id(chunk_id)
    except IdentityValidationError as exc:
        raise RegistryValidationError(str(exc)) from exc
    if not isinstance(chroma_embedding_id, str) or not chroma_embedding_id.strip():
        raise RegistryValidationError("chroma_embedding_id must be non-empty str")
    if mapping_status not in MAPPING_STATUSES:
        raise RegistryValidationError(
            f"mapping_status must be one of {sorted(MAPPING_STATUSES)}"
        )

    existing = conn.execute(
        "SELECT * FROM chunk_vector_map "
        "WHERE chunk_id = ? AND physical_collection_name = ?",
        (chunk_id, physical_collection_name),
    ).fetchone()
    if existing is not None:
        if (
            existing["chroma_embedding_id"] == chroma_embedding_id
            and existing["mapping_status"] == mapping_status
            and existing["identity_scheme_version"] == identity_scheme_version
            and existing["vector_store"] == vector_store
        ):
            return _row_to_dict(existing)  # type: ignore[return-value]
        raise RegistryConflictError(
            f"conflicting chunk_vector_map for {chunk_id!r} in "
            f"{physical_collection_name!r}"
        )

    other = conn.execute(
        "SELECT chunk_id FROM chunk_vector_map "
        "WHERE physical_collection_name = ? AND chroma_embedding_id = ?",
        (physical_collection_name, chroma_embedding_id),
    ).fetchone()
    if other is not None and other["chunk_id"] != chunk_id:
        raise RegistryConflictError(
            f"chroma_embedding_id {chroma_embedding_id!r} already mapped to "
            f"{other['chunk_id']!r}"
        )

    ts = created_at or utc_now()
    try:
        conn.execute(
            "INSERT INTO chunk_vector_map ("
            "chunk_id, physical_collection_name, chroma_embedding_id, vector_store, "
            "mapping_status, identity_scheme_version, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                physical_collection_name,
                chroma_embedding_id,
                vector_store,
                mapping_status,
                identity_scheme_version,
                ts,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RegistryValidationError(
            f"chunk_vector_map insert failed integrity check: {exc}"
        ) from exc

    row = conn.execute(
        "SELECT * FROM chunk_vector_map "
        "WHERE chunk_id = ? AND physical_collection_name = ?",
        (chunk_id, physical_collection_name),
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def register_document_lifecycle(
    conn: sqlite3.Connection,
    *,
    subject_id: str,
    document_id: str,
    source_hash: str,
    relative_path: str,
    chunks: list[Mapping[str, Any]],
    vector_mappings: list[Mapping[str, Any]] | None = None,
    library_root: str | Path | None = None,
    content_hash: str | None = None,
    document_type: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Atomic multi-table registration for one revision + locators + chunks.

    Rolls back entirely on any failure.
    """
    with registry_transaction(conn):
        subj = register_subject(
            conn,
            subject_id=subject_id,
            document_type=document_type,
            scope=scope,
        )
        ver = register_document_version(
            conn,
            document_id=document_id,
            subject_id=subject_id,
            source_hash=source_hash,
            content_hash=content_hash,
        )
        loc = register_source_file(
            conn,
            document_id=document_id,
            relative_path=relative_path,
            library_root=library_root,
            source_hash=source_hash,
        )
        chunk_rows = []
        for spec in chunks:
            chunk_rows.append(
                register_chunk(
                    conn,
                    chunk_id=str(spec["chunk_id"]),
                    document_id=document_id,
                    chunking_fingerprint=str(spec["chunking_fingerprint"]),
                    ordinal=int(spec["ordinal"]),
                    identity_scheme_version=str(
                        spec.get("identity_scheme_version", IDENTITY_SCHEME_VERSION)
                    ),
                    content_hash=spec.get("content_hash"),
                    page=spec.get("page"),
                )
            )
        map_rows = []
        for spec in vector_mappings or []:
            map_rows.append(
                register_vector_mapping(
                    conn,
                    chunk_id=str(spec["chunk_id"]),
                    chroma_embedding_id=str(spec["chroma_embedding_id"]),
                    physical_collection_name=str(
                        spec.get("physical_collection_name", "langchain")
                    ),
                    vector_store=str(spec.get("vector_store", "chroma")),
                    mapping_status=str(
                        spec.get("mapping_status", MAPPING_STATUS_LEGACY_UUID)
                    ),
                    identity_scheme_version=str(
                        spec.get("identity_scheme_version", IDENTITY_SCHEME_VERSION)
                    ),
                )
            )
        return {
            "subject": subj,
            "document_version": ver,
            "source_file": loc,
            "chunks": chunk_rows,
            "vector_mappings": map_rows,
        }


class RegistryRepository:
    """Thin wrapper around connection + registration helpers."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, db_path: str | Path, *, readonly: bool = False) -> "RegistryRepository":
        return cls(open_registry(db_path, readonly=readonly))

    def close(self) -> None:
        self.conn.close()

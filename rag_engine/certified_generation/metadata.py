"""Certified vector metadata + stable chunk IDs for NEW generations only.

Chroma vector IDs are native `chunk:<32hex>` values, never random UUIDs.
"""

from __future__ import annotations

from typing import Any

from rag_engine.certified_generation.exceptions import CollisionGuardError
from rag_engine.stable_identity.ids import chunk_id as make_stable_chunk_id
from rag_engine.stable_identity.validation import (
    validate_chunk_id,
    validate_chunking_fingerprint,
    validate_document_id,
    validate_source_hash,
)

REQUIRED_VECTOR_FIELDS = (
    "document_id",
    "source_hash",
    "chunk_id",
    "embedding_generation_id",
    "chunking_fingerprint",
    "source",
    "page",
    "collection",
)

EXCLUDED_DEFAULT_FIELDS = (
    "subject_id",
    "acquisition_id",
    "aliases",
    "canonical_path",
    "maker",
    "model",
    "title",
    "document_number",
    "revision",
    "edition",
    "applicability",
    "supersession",
    "lifecycle_review",
)


def certified_chunk_id(
    *,
    document_id: str,
    chunking_fingerprint: str,
    ordinal: int,
) -> str:
    return make_stable_chunk_id(document_id, chunking_fingerprint, ordinal)


def certified_vector_metadata(
    *,
    document_id: str,
    source_hash: str,
    chunk_id: str,
    embedding_generation_id: str,
    chunking_fingerprint: str,
    source: str,
    page: Any,
    collection: str,
    page_index: Any | None = None,
    machine_transcribed: bool | None = None,
) -> dict[str, Any]:
    validate_document_id(document_id)
    validate_source_hash(source_hash)
    validate_chunk_id(chunk_id)
    validate_chunking_fingerprint(chunking_fingerprint)
    if not document_id.endswith(source_hash) and document_id != f"docrev:{source_hash}":
        raise CollisionGuardError(
            "document_id must equal docrev:<source_hash>",
            details={"document_id": document_id, "source_hash": source_hash},
        )
    if document_id != f"docrev:{source_hash}":
        raise CollisionGuardError(
            "document_id/source_hash mismatch",
            details={"document_id": document_id, "source_hash": source_hash},
        )
    meta: dict[str, Any] = {
        "document_id": document_id,
        "source_hash": source_hash,
        "chunk_id": chunk_id,
        "embedding_generation_id": embedding_generation_id,
        "chunking_fingerprint": chunking_fingerprint,
        "source": source,
        "page": page if page is not None else "?",
        "collection": collection,
    }
    if page_index is not None:
        meta["page_index"] = page_index
    if machine_transcribed is not None:
        meta["machine_transcribed"] = bool(machine_transcribed)
    for forbidden in EXCLUDED_DEFAULT_FIELDS:
        if forbidden in meta:
            raise CollisionGuardError(f"forbidden default field {forbidden}")
    return meta


def assert_no_chunk_id_collision(seen: dict[str, dict[str, Any]], meta: dict[str, Any]) -> None:
    cid = meta["chunk_id"]
    prev = seen.get(cid)
    if prev is None:
        seen[cid] = meta
        return
    keys = ("document_id", "chunking_fingerprint", "source_hash", "embedding_generation_id")
    for k in keys:
        if prev.get(k) != meta.get(k):
            raise CollisionGuardError(
                "duplicate chunk_id with incompatible metadata",
                details={"chunk_id": cid, "key": k, "prev": prev.get(k), "new": meta.get(k)},
            )
    raise CollisionGuardError(
        "duplicate chunk_id within generation",
        details={"chunk_id": cid},
    )

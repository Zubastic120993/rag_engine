"""Certified new-generation build API - explicit target, never production .rag_db."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from rag_engine.authority import is_machine_transcribed_source
from rag_engine.certified_generation.checkpoints import (
    STATE_BUILDING,
    STATE_BUILT_UNVALIDATED,
    STATE_PREPARED,
    read_checkpoint,
    write_checkpoint,
)
from rag_engine.certified_generation.contracts import (
    certified_chunking_fingerprint,
    certified_v1_envelope,
)
from rag_engine.certified_generation.corpus import (
    dedup_by_document_id,
    load_corpus_manifest,
    verify_manifest_against_disk,
)
from rag_engine.certified_generation.exceptions import (
    CollisionGuardError,
    DimensionGuardError,
    EmptyGenerationGuardError,
    ModelGuardError,
    PartialBuildError,
)
from rag_engine.certified_generation.ids import (
    generation_id_to_dirname,
    make_generation_id,
    parse_generation_id,
    validate_generation_binding,
)
from rag_engine.certified_generation.metadata import (
    assert_no_chunk_id_collision,
    certified_chunk_id,
    certified_vector_metadata,
)
from rag_engine.certified_generation.paths import (
    assert_not_production_generations_root,
    assert_safe_generation_path,
    require_explicit_persist_dir,
)
from rag_engine.certified_generation.tracker import tracker_entry, write_tracker
from rag_engine.certified_generation.v1 import initialize_certified_v1
from rag_engine.config import chroma_client_settings, collection_from_relpath, embed_model
from rag_engine.index_compatibility.constants import DEFAULT_EMBEDDING_DIMENSION
from rag_engine.stable_identity.hashing import content_hash_text


def _page_value(meta: dict[str, Any]) -> Any:
    page = meta.get("page", "?")
    return page if page is not None else "?"


def _page_index(meta: dict[str, Any]) -> Any | None:
    if "page_index" in meta:
        return meta.get("page_index")
    page = meta.get("page")
    if isinstance(page, int):
        return page
    return None


def _probe_dimension(embedding_function: Any) -> int:
    vectors = embedding_function.embed_documents(["certified-generation-dimension-probe"])
    if not vectors or not vectors[0]:
        raise DimensionGuardError("embedding function returned empty vector")
    return len(vectors[0])


def init_certified_generation(
    *,
    persist_dir: str | Path | None,
    corpus_manifest: str | Path | dict[str, Any],
    registry_db: str | Path | None = None,
    utcstamp: str | None = None,
    embedding_model_revision: str | None = None,
    allow_resume: bool = False,
    write_v0_compat: bool = True,
) -> dict[str, Any]:
    """Prepare an isolated certified generation: v1 + checkpoint PREPARED.

    ``persist_dir`` is required. Production ``.rag_db`` is refused.
    Resume of partial embedding writes is disabled by default.
    """
    persist = require_explicit_persist_dir(persist_dir)
    persist = assert_safe_generation_path(persist)
    persist = assert_not_production_generations_root(persist)

    if isinstance(corpus_manifest, dict):
        manifest = corpus_manifest
    else:
        manifest = load_corpus_manifest(corpus_manifest)
    verify_manifest_against_disk(manifest)
    units = dedup_by_document_id(manifest)

    cfp = certified_chunking_fingerprint()
    envelope = certified_v1_envelope(embedding_model_revision=embedding_model_revision)
    model = envelope["embedding_contract"]["embedding_model"]
    dim = int(envelope["embedding_contract"]["embedding_dimension"])
    provider = envelope["embedding_contract"]["embedding_provider"]
    generation_id = make_generation_id(
        embedding_provider=provider,
        embedding_model=model,
        embedding_dimension=dim,
        chunking_fingerprint=cfp,
        corpus_manifest_sha256=str(manifest["manifest_sha256"]),
        utcstamp=utcstamp,
    )
    parse_generation_id(generation_id)

    existing_cp = read_checkpoint(persist) if persist.exists() else None
    if existing_cp and existing_cp.get("state") == STATE_BUILDING and not allow_resume:
        raise PartialBuildError(
            "partial BUILDING generation found; resume disabled - use a new target",
            details={"path": str(persist), "generation_id": existing_cp.get("generation_id")},
        )
    if existing_cp and existing_cp.get("generation_id") not in {None, generation_id}:
        if not allow_resume:
            raise PartialBuildError(
                "checkpoint generation_id mismatch; resume disabled",
                details={
                    "stored": existing_cp.get("generation_id"),
                    "expected": generation_id,
                },
            )

    v1 = initialize_certified_v1(
        persist,
        envelope,
        allow_resume=allow_resume,
        registry_db=registry_db,
        write_registry=False,
    )
    if write_v0_compat:
        from rag_engine.fingerprint import write_fingerprint_at

        write_fingerprint_at(
            persist,
            extra={
                "authority": "compatibility_snapshot_only",
                "certified_generation_id": generation_id,
            },
        )

    checkpoint = {
        "state": STATE_PREPARED,
        "generation_id": generation_id,
        "generation_dir_name": generation_id_to_dirname(generation_id),
        "persist_dir": str(persist),
        "corpus_manifest_sha256": manifest["manifest_sha256"],
        "chunking_fingerprint": cfp,
        "embedding_generation_id": generation_id,
        "embedding_model": model,
        "embedding_provider": provider,
        "embedding_dimension": dim,
        "v1_index_fingerprint": envelope["index_fingerprint"],
        "unique_documents": len(units),
        "source_paths": len(manifest["entries"]),
        "accepted": False,
    }
    write_checkpoint(persist, checkpoint)
    return {
        "persist_dir": str(persist),
        "generation_id": generation_id,
        "chunking_fingerprint": cfp,
        "v1": v1,
        "checkpoint": checkpoint,
        "units": units,
        "manifest": manifest,
        "envelope": envelope,
    }


def prepare_certified_chunks(unit: dict[str, Any], *, chunking_fingerprint: str, generation_id: str) -> dict[str, Any]:
    from rag_engine.ingest import chunk_documents, load_source_documents

    path = Path(unit["absolute_path"])
    docs = load_source_documents(path)
    display = unit["display_source"]
    collection = collection_from_relpath(display)
    for d in docs:
        d.metadata["source"] = display
        d.metadata["page"] = d.metadata.get("page", "?")
        d.metadata["collection"] = collection
    valid = chunk_documents(docs)
    document_id = unit["document_id"]
    source_hash = unit["source_hash"]
    mt = is_machine_transcribed_source(display)
    records = []
    seen: dict[str, dict[str, Any]] = {}
    for ordinal, doc in enumerate(valid):
        cid = certified_chunk_id(
            document_id=document_id,
            chunking_fingerprint=chunking_fingerprint,
            ordinal=ordinal,
        )
        meta = certified_vector_metadata(
            document_id=document_id,
            source_hash=source_hash,
            chunk_id=cid,
            embedding_generation_id=generation_id,
            chunking_fingerprint=chunking_fingerprint,
            source=display,
            page=_page_value(doc.metadata),
            collection=collection,
            page_index=_page_index(doc.metadata),
            machine_transcribed=mt,
        )
        assert_no_chunk_id_collision(seen, meta)
        doc.metadata = meta
        records.append(
            {
                "ordinal": ordinal,
                "chunk_id": cid,
                "document": doc,
                "content_hash": content_hash_text(doc.page_content),
                "chunking_fingerprint": chunking_fingerprint,
                "page": meta["page"] if isinstance(meta["page"], int) else None,
            }
        )
    return {
        "collection": collection,
        "records": records,
        "extraction": "ok" if records else "empty",
    }


def build_certified_generation(
    *,
    persist_dir: str | Path | None,
    corpus_manifest: str | Path | dict[str, Any],
    embedding_function: Any | None = None,
    registry_db: str | Path | None = None,
    utcstamp: str | None = None,
    allow_resume: bool = False,
    init_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Embed unique document bytes into an isolated certified generation.

    Requires explicit persist_dir. Does not default to persist_dir().
    Partial embedding resume is disabled unless allow_resume=True (still
    refuses contract mismatch).
    """
    prepared = init_result or init_certified_generation(
        persist_dir=persist_dir,
        corpus_manifest=corpus_manifest,
        registry_db=registry_db,
        utcstamp=utcstamp,
        allow_resume=allow_resume,
    )
    persist = Path(prepared["persist_dir"])
    generation_id = prepared["generation_id"]
    cfp = prepared["chunking_fingerprint"]
    units = prepared["units"]
    envelope = prepared["envelope"]
    expected_dim = int(envelope["embedding_contract"]["embedding_dimension"])
    expected_model = envelope["embedding_contract"]["embedding_model"]

    if embedding_function is None:
        from langchain_ollama import OllamaEmbeddings

        embedding_function = OllamaEmbeddings(model=embed_model())
        live_model = embed_model()
        if live_model != expected_model:
            raise ModelGuardError(
                "live embedding model does not match generation contract",
                details={"live": live_model, "expected": expected_model},
            )

    dim = _probe_dimension(embedding_function)
    if dim != expected_dim:
        raise DimensionGuardError(
            "embedding dimension mismatch",
            details={"actual": dim, "expected": expected_dim},
        )
    if expected_dim != DEFAULT_EMBEDDING_DIMENSION and dim != expected_dim:
        raise DimensionGuardError("dimension contract failed")

    cp = read_checkpoint(persist)
    if cp and cp.get("state") == STATE_BUILDING and not allow_resume:
        raise PartialBuildError("cannot continue BUILDING generation; resume disabled")
    write_checkpoint(
        persist,
        {**prepared["checkpoint"], "state": STATE_BUILDING},
    )

    from langchain_chroma import Chroma

    db = Chroma(
        persist_directory=str(persist),
        embedding_function=embedding_function,
        client_settings=chroma_client_settings(),
    )
    tracker: dict[str, Any] = {}
    chunks_by_document: dict[str, list[dict[str, Any]]] = {}
    global_ids: dict[str, dict[str, Any]] = {}
    vector_count = 0

    for unit in units:
        prepared_doc = prepare_certified_chunks(
            unit, chunking_fingerprint=cfp, generation_id=generation_id
        )
        records = prepared_doc["records"]
        docs = [r["document"] for r in records]
        ids = [r["chunk_id"] for r in records]
        for rec, doc in zip(records, docs):
            assert_no_chunk_id_collision(global_ids, doc.metadata)
            if rec["chunk_id"] != doc.metadata["chunk_id"]:
                raise CollisionGuardError("chunk_id drifted before write")
        if docs:
            added = db.add_documents(docs, ids=ids)
            if isinstance(added, list) and added and list(added) != ids:
                raise CollisionGuardError(
                    "Chroma did not preserve stable chunk_id vector IDs",
                    details={"expected": ids, "actual": added},
                )
            got = db.get(ids=ids)
            got_ids = list(got.get("ids") or [])
            if sorted(got_ids) != sorted(ids):
                raise CollisionGuardError(
                    "Chroma get() did not return native chunk_ids",
                    details={"expected": ids, "actual": got_ids},
                )
            gens = {
                (m or {}).get("embedding_generation_id")
                for m in (got.get("metadatas") or [])
            }
            if gens - {generation_id, None}:
                raise CollisionGuardError(
                    "mixed embedding_generation_id in target collection",
                    details={"found": sorted(str(g) for g in gens)},
                )
            vector_count += len(ids)
        tracker[unit["source_hash"]] = tracker_entry(
            paths=list(unit["aliases"]),
            chunk_ids=ids,
            collection=prepared_doc["collection"],
            extraction=prepared_doc["extraction"],
            document_id=unit["document_id"],
        )
        chunks_by_document[unit["document_id"]] = records
        unit["collection"] = prepared_doc["collection"]

    write_tracker(persist, tracker)
    write_checkpoint(
        persist,
        {
            **prepared["checkpoint"],
            "state": STATE_BUILT_UNVALIDATED,
            "vector_count": vector_count,
        },
    )

    registry_result = None
    if registry_db is not None:
        from rag_engine.certified_generation.registry_bootstrap import (
            bootstrap_certified_documents,
            bootstrap_registry_db,
        )

        bootstrap_registry_db(registry_db)
        registry_result = bootstrap_certified_documents(
            registry_db,
            units,
            chunks_by_document=chunks_by_document,
            embedding_generation_id=generation_id,
            v1_envelope=envelope,
        )

    return {
        "persist_dir": str(persist),
        "generation_id": generation_id,
        "checkpoint_state": STATE_BUILT_UNVALIDATED,
        "vector_count": vector_count,
        "document_count": len(units),
        "tracker_entries": len(tracker),
        "chunking_fingerprint": cfp,
        "accepted": False,
        "registry": registry_result,
        "units": units,
    }

"""Certified incremental append into an existing certified generation.

Distinct from legacy ``rag-engine ingest``. Explicit targets only.
Multi-store commit is TRANSACTION_LIKE / RECOVERABLE_MULTI_STORE_COMMIT 
not a single native atomic transaction across Chroma + SQLite + JSON.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from rag_engine.certified_generation.append_lock import certified_append_lock
from rag_engine.certified_generation.append_manifest import (
    load_append_manifest,
    verify_append_manifest_against_disk,
)
from rag_engine.certified_generation.append_reconcile import reconcile_append_affected
from rag_engine.certified_generation.append_recovery import (
    compensating_registry_delete,
    delete_chroma_ids,
    new_operation_id,
    restore_tracker_snapshot,
    snapshot_tracker,
    write_journal,
)
from rag_engine.certified_generation.build import (
    _probe_dimension,
    prepare_certified_chunks,
)
from rag_engine.certified_generation.checkpoints import read_checkpoint
from rag_engine.certified_generation.exceptions import (
    AppendGenerationGuardError,
    AppendManifestError,
    AppendRecoveryError,
    AppendTargetError,
    CertifiedAppendError,
    CollisionGuardError,
    DimensionGuardError,
    ModelGuardError,
)
from rag_engine.certified_generation.ids import (
    dirname_to_generation_id,
    parse_generation_id,
)
from rag_engine.certified_generation.metadata import assert_no_chunk_id_collision
from rag_engine.certified_generation.paths import (
    PRODUCTION_RAG_DB,
    assert_certified_persist_dir,
    assert_certified_registry_path,
    is_legacy_production_persist,
)
from rag_engine.certified_generation.tracker import (
    read_tracker,
    tracker_entry,
    write_tracker,
)
from rag_engine.config import chroma_client_settings, collection_from_relpath, embed_model
from rag_engine.index_compatibility.chroma_inspect import count_vectors_readonly
from rag_engine.index_compatibility.compatibility import evaluate_compatibility
from rag_engine.index_compatibility.constants import (
    COMPAT_KNOWN_COMPATIBLE,
    DEFAULT_EMBEDDING_DIMENSION,
    SIDECAR_V1_NAME,
)
from rag_engine.index_compatibility.state import read_sidecar_v1, sidecar_v1_path
from rag_engine.metadata_registry import (
    open_registry,
    register_chunk,
    register_document_version,
    register_source_file,
    register_subject,
    register_vector_mapping,
    registry_transaction,
)
from rag_engine.stable_identity.constants import MAPPING_STATUS_NATIVE_CHUNK_ID
from rag_engine.stable_identity.hashing import source_hash_from_file
from rag_engine.stable_identity.ids import document_id_from_source_hash, subject_id_pending

ACTION_NEW = "NEW_REVISION_APPEND"
ACTION_ALIAS = "ALIAS_ONLY"
ACTION_NO_CHANGE = "NO_CHANGE"
ACTION_ZERO = "ZERO_VECTOR_VALID"

COMMIT_MODEL = "RECOVERABLE_MULTI_STORE_COMMIT"


def is_certified_generation_persist(persist: str | Path) -> bool:
    """True if persist looks like a certified generation (legacy ingest must refuse)."""
    path = Path(persist).expanduser().resolve()
    if (path / "certified_generation_checkpoint.json").is_file():
        return True
    if path.name.startswith("raggen_"):
        return True
    # Contained under .rag_db_generations with a generation-shaped name
    if ".rag_db_generations" in path.parts and path.name.startswith("raggen"):
        return True
    return False


def assert_legacy_ingest_allowed(persist: str | Path) -> None:
    """Fail closed: ordinary ingest must not write UUID vectors into certified gens."""
    from rag_engine.certified_generation.exceptions import LegacyIngestForbiddenError

    path = Path(persist).expanduser().resolve()
    if is_certified_generation_persist(path):
        raise LegacyIngestForbiddenError(
            "legacy ingest cannot write to a certified generation; "
            "use `rag-engine generation append` with an explicit approved manifest",
            details={"persist_dir": str(path)},
        )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _read_v1_raw(persist: Path) -> dict[str, Any]:
    path = sidecar_v1_path(persist)
    if not path.is_file():
        raise AppendGenerationGuardError(
            "certified append requires index_embedding_fingerprint_v1.json",
            details={"path": str(path)},
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AppendGenerationGuardError("v1 root must be object")
    return data


def _generation_id_for_persist(persist: Path) -> str:
    cp = read_checkpoint(persist)
    if cp and cp.get("generation_id"):
        gid = str(cp["generation_id"])
        parse_generation_id(gid)
        return gid
    try:
        return dirname_to_generation_id(persist.name)
    except Exception as exc:
        raise AppendGenerationGuardError(
            "cannot derive generation_id from checkpoint or directory name",
            details={"persist_dir": str(persist)},
        ) from exc


def _chunking_fingerprint_for_persist(persist: Path, v1: Mapping[str, Any]) -> str:
    cp = read_checkpoint(persist)
    if cp and cp.get("chunking_fingerprint"):
        return str(cp["chunking_fingerprint"])
    corp = v1.get("corpus_contract") or {}
    # Prefer recomputed from stored corpus contract fields if present
    from rag_engine.stable_identity.canonical import chunking_fingerprint

    if corp.get("chunk_size") is not None:
        return chunking_fingerprint(dict(corp))
    raise AppendGenerationGuardError("chunking_fingerprint missing from generation")


def validate_certified_append_target(
    *,
    persist_dir: str | Path | None,
    registry_db: str | Path | None,
    dry_validate: bool = False,
) -> dict[str, Any]:
    """Structural target validation. Does not mutate."""
    if persist_dir is None or str(persist_dir).strip() == "":
        raise AppendTargetError("certified append requires explicit --persist-dir")
    if registry_db is None or str(registry_db).strip() == "":
        raise AppendTargetError("certified append requires explicit --registry-db")

    persist = assert_certified_persist_dir(persist_dir)
    registry = assert_certified_registry_path(registry_db)

    if is_legacy_production_persist(persist) or persist == PRODUCTION_RAG_DB.resolve():
        raise AppendTargetError("legacy .rag_db is forbidden as certified append target")

    if persist.name == ".rag_db_generations" or persist.name == ".rag_state":
        raise AppendTargetError("generations root / .rag_state cannot be append targets")

    if not persist.is_dir():
        raise AppendTargetError("persist_dir must exist as a certified generation directory")

    v1_path = sidecar_v1_path(persist)
    if not v1_path.is_file():
        raise AppendGenerationGuardError("v1 missing; append does not initialize generations")

    v1_before = _sha256_file(v1_path)
    v1 = _read_v1_raw(persist)
    # Touch read_sidecar for structural validation
    read_sidecar_v1(persist)

    compat = evaluate_compatibility(persist)
    if compat.state != COMPAT_KNOWN_COMPATIBLE:
        raise AppendGenerationGuardError(
            "append requires KNOWN_COMPATIBLE generation",
            details={"state": compat.state},
        )

    generation_id = _generation_id_for_persist(persist)
    cfp = _chunking_fingerprint_for_persist(persist, v1)
    emb = v1.get("embedding_contract") or {}
    dim = int(emb.get("embedding_dimension") or 0)
    if dim != DEFAULT_EMBEDDING_DIMENSION:
        raise AppendGenerationGuardError(
            "embedding dimension contract mismatch",
            details={"dimension": dim, "expected": DEFAULT_EMBEDDING_DIMENSION},
        )

    if not registry.is_file():
        raise AppendTargetError("registry_db file must exist")

    return {
        "ok": True,
        "persist_dir": str(persist),
        "registry_db": str(registry),
        "generation_id": generation_id,
        "chunking_fingerprint": cfp,
        "compatibility_state": compat.state,
        "embedding_dimension": dim,
        "embedding_model": emb.get("embedding_model"),
        "embedding_provider": emb.get("embedding_provider"),
        "v1_sha256": v1_before,
        "vector_count": count_vectors_readonly(persist),
        "dry_validate": dry_validate,
        "v1_path": str(v1_path),
        "accepted_checkpoint": False,  # debt: do not reinterpret
    }


def _lookup_document_by_hash(conn: Any, source_hash: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT document_id, source_hash, subject_id FROM document_versions WHERE source_hash = ?",
        (source_hash,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _alias_exists(conn: Any, document_id: str, relative_path: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM source_files WHERE document_id = ? AND relative_path = ?",
        (document_id, relative_path),
    ).fetchone()
    return row is not None


def _classify_entry(
    conn: Any,
    *,
    source_hash: str,
    relative_path: str,
) -> str:
    doc = _lookup_document_by_hash(conn, source_hash)
    if doc is None:
        return ACTION_NEW
    if _alias_exists(conn, doc["document_id"], relative_path):
        return ACTION_NO_CHANGE
    return ACTION_ALIAS


def dry_validate_certified_append(
    *,
    persist_dir: str | Path | None,
    registry_db: str | Path | None,
    manifest: str | Path | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Non-mutating target (+ optional manifest) validation."""
    target = validate_certified_append_target(
        persist_dir=persist_dir,
        registry_db=registry_db,
        dry_validate=True,
    )
    result = dict(target)
    if manifest is not None:
        m = load_append_manifest(manifest)
        if m.get("generation_id") and m["generation_id"] != target["generation_id"]:
            raise AppendManifestError(
                "manifest generation_id does not match target",
                details={
                    "manifest": m["generation_id"],
                    "target": target["generation_id"],
                },
            )
        verify_append_manifest_against_disk(m)
        result["manifest_sha256"] = m["manifest_sha256"]
        result["manifest_entries"] = m["source_count"]
    return result


def append_certified_sources(
    *,
    persist_dir: str | Path | None,
    registry_db: str | Path | None,
    manifest: str | Path | Mapping[str, Any],
    embedding_function: Any | None = None,
    dry_validate: bool = False,
    lock_timeout_s: float = 0,
    _fail_after: str | None = None,
    _embed_hook: Callable[[list[Any]], None] | None = None,
) -> dict[str, Any]:
    """Append new revisions and/or aliases into a certified generation.

    Explicit ``persist_dir`` and ``registry_db`` required.
    Does not default to ``persist_dir()`` / production ``.rag_db``.
    """
    if dry_validate:
        return dry_validate_certified_append(
            persist_dir=persist_dir,
            registry_db=registry_db,
            manifest=manifest,
        )

    target = validate_certified_append_target(
        persist_dir=persist_dir,
        registry_db=registry_db,
    )
    persist = Path(target["persist_dir"])
    registry = Path(target["registry_db"])
    generation_id = target["generation_id"]
    cfp = target["chunking_fingerprint"]
    expected_dim = int(target["embedding_dimension"])
    expected_model = target["embedding_model"]
    v1_sha_before = target["v1_sha256"]

    m = load_append_manifest(manifest)
    if m.get("generation_id") and m["generation_id"] != generation_id:
        raise AppendManifestError(
            "manifest generation_id does not match target generation",
            details={"manifest": m["generation_id"], "target": generation_id},
        )
    verify_append_manifest_against_disk(m)

    if not m["entries"]:
        return {
            "ok": True,
            "action_summary": {ACTION_NO_CHANGE: 0},
            "results": [],
            "vector_delta": 0,
            "document_delta": 0,
            "alias_delta": 0,
            "generation_id": generation_id,
            "chunking_fingerprint": cfp,
            "commit_model": COMMIT_MODEL,
            "v1_modified": False,
            "empty_manifest": True,
            "reconciliation": {"ok": True, "p0": 0, "p1": 0, "findings": []},
        }

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

    operation_id = new_operation_id()
    with certified_append_lock(persist, timeout_s=lock_timeout_s):
        # Re-verify after lock
        verify_append_manifest_against_disk(m)
        if _sha256_file(sidecar_v1_path(persist)) != v1_sha_before:
            raise AppendGenerationGuardError("v1 changed underfoot; refuse append")

        conn = open_registry(registry)
        try:
            planned: list[dict[str, Any]] = []
            for entry in m["entries"]:
                # Final per-item hash check
                live_hash = source_hash_from_file(entry["absolute_path"])
                if live_hash != entry["sha256"]:
                    raise AppendManifestError(
                        f"source hash drift mid-run: {entry['relative_path']}",
                        details={"expected": entry["sha256"], "actual": live_hash},
                    )
                action = _classify_entry(
                    conn,
                    source_hash=entry["sha256"],
                    relative_path=entry["relative_path"],
                )
                document_id = document_id_from_source_hash(entry["sha256"])
                item: dict[str, Any] = {
                    "action": action,
                    "relative_path": entry["relative_path"],
                    "absolute_path": entry["absolute_path"],
                    "source_hash": entry["sha256"],
                    "document_id": document_id,
                    "chunk_ids": [],
                    "records": [],
                    "collection": collection_from_relpath(entry["relative_path"]),
                }
                if action == ACTION_NEW:
                    unit = {
                        "document_id": document_id,
                        "source_hash": entry["sha256"],
                        "display_source": entry["relative_path"],
                        "absolute_path": entry["absolute_path"],
                        "aliases": [entry["relative_path"]],
                    }
                    prepared = prepare_certified_chunks(
                        unit,
                        chunking_fingerprint=cfp,
                        generation_id=generation_id,
                    )
                    item["records"] = prepared["records"]
                    item["chunk_ids"] = [r["chunk_id"] for r in prepared["records"]]
                    item["collection"] = prepared["collection"]
                    item["extraction"] = prepared["extraction"]
                    if not item["chunk_ids"]:
                        item["action"] = ACTION_ZERO
                elif action == ACTION_ALIAS:
                    # Load existing chunk_ids from tracker if present
                    tracker = read_tracker(persist)
                    te = tracker.get(entry["sha256"]) or {}
                    item["chunk_ids"] = list(te.get("chunk_ids") or [])
                    item["collection"] = te.get("collection") or item["collection"]
                    item["extraction"] = te.get("extraction") or "ok"
                    if not item["chunk_ids"]:
                        rows = conn.execute(
                            "SELECT chunk_id FROM chunks WHERE document_id = ? "
                            "ORDER BY chunk_ordinal",
                            (document_id,),
                        ).fetchall()
                        item["chunk_ids"] = [r["chunk_id"] for r in rows]
                planned.append(item)

            # Collision check against existing chroma for new chunk ids
            from langchain_chroma import Chroma

            db = Chroma(
                persist_directory=str(persist),
                embedding_function=embedding_function,
                client_settings=chroma_client_settings(),
            )
            new_ids: list[str] = []
            for item in planned:
                if item["action"] in {ACTION_NEW, ACTION_ZERO}:
                    for cid in item["chunk_ids"]:
                        new_ids.append(cid)
            if new_ids:
                existing = db.get(ids=new_ids, include=["metadatas"])
                got = list(existing.get("ids") or [])
                if got:
                    # Idempotent only if metadata matches exactly the planned identity
                    metas = existing.get("metadatas") or []
                    for eid, meta in zip(got, metas):
                        meta = meta or {}
                        if meta.get("embedding_generation_id") != generation_id:
                            raise CollisionGuardError(
                                "conflicting existing chunk_id in generation",
                                details={"chunk_id": eid},
                            )
                        # Treat as conflict for new revision path unless full match
                        matching = next(
                            (p for p in planned if eid in p.get("chunk_ids", [])),
                            None,
                        )
                        if matching is None or matching["action"] not in {
                            ACTION_NEW,
                            ACTION_ZERO,
                        }:
                            raise CollisionGuardError(
                                "chunk_id already present",
                                details={"chunk_id": eid},
                            )
                        if (
                            meta.get("document_id") != matching["document_id"]
                            or meta.get("source_hash") != matching["source_hash"]
                            or meta.get("chunking_fingerprint") != cfp
                        ):
                            raise CollisionGuardError(
                                "duplicate chunk_id with incompatible metadata",
                                details={"chunk_id": eid},
                            )
                    # Consistent existing ? convert those to NO_CHANGE-like skip of chroma
                    for item in planned:
                        if item["action"] == ACTION_NEW and item["chunk_ids"]:
                            if all(cid in got for cid in item["chunk_ids"]):
                                # vectors already present; still ensure registry/tracker
                                pass

            if _fail_after == "before_mutation":
                raise CertifiedAppendError("injected failure before mutation")

            tracker_snap = snapshot_tracker(persist, operation_id)
            journal = {
                "operation_id": operation_id,
                "generation_id": generation_id,
                "manifest_sha256": m["manifest_sha256"],
                "state": "PREPARED",
                "commit_model": COMMIT_MODEL,
                "planned": [
                    {
                        "action": p["action"],
                        "document_id": p["document_id"],
                        "source_hash": p["source_hash"],
                        "relative_path": p["relative_path"],
                        "chunk_ids": p["chunk_ids"],
                    }
                    for p in planned
                ],
                "chroma_ids_written": [],
                "registry_document_ids": [],
                "registry_aliases": [],
                "tracker_snapshot": str(tracker_snap),
            }
            write_journal(persist, journal)

            chroma_written: list[str] = []
            registry_docs: list[str] = []
            registry_aliases: list[tuple[str, str]] = []
            embed_calls = {"n": 0}

            def _counting_embed(docs: list[Any]) -> None:
                embed_calls["n"] += 1
                if _embed_hook:
                    _embed_hook(docs)

            try:
                # --- CHROMA (new vectors only) ---
                for item in planned:
                    if item["action"] not in {ACTION_NEW}:
                        continue
                    records = item["records"]
                    docs = [r["document"] for r in records]
                    ids = [r["chunk_id"] for r in records]
                    if not docs:
                        continue
                    seen: dict[str, dict[str, Any]] = {}
                    for doc in docs:
                        assert_no_chunk_id_collision(seen, doc.metadata)
                    if _fail_after == "embedding":
                        raise ModelGuardError("injected embedding failure")
                    _counting_embed(docs)
                    # Check if already present (idempotent replay)
                    already = db.get(ids=ids)
                    already_ids = set(already.get("ids") or [])
                    if already_ids == set(ids):
                        continue
                    if already_ids:
                        raise CollisionGuardError(
                            "partial existing chunk_ids; refuse overwrite",
                            details={"ids": sorted(already_ids)},
                        )
                    added = db.add_documents(docs, ids=ids)
                    if isinstance(added, list) and added and list(added) != ids:
                        raise CollisionGuardError(
                            "Chroma did not preserve stable chunk_id vector IDs",
                            details={"expected": ids, "actual": added},
                        )
                    got = db.get(ids=ids)
                    if sorted(got.get("ids") or []) != sorted(ids):
                        raise CollisionGuardError("Chroma get mismatch after append")
                    for meta in got.get("metadatas") or []:
                        if (meta or {}).get("embedding_generation_id") != generation_id:
                            raise CollisionGuardError("mixed generation after append write")
                    chroma_written.extend(ids)
                    journal["chroma_ids_written"] = list(chroma_written)
                    journal["state"] = "CHROMA_WRITTEN"
                    write_journal(persist, journal)

                if _fail_after == "after_chroma":
                    raise CertifiedAppendError("injected failure after chroma")

                # --- REGISTRY ---
                with registry_transaction(conn):
                    for item in planned:
                        if item["action"] == ACTION_NO_CHANGE:
                            continue
                        document_id = item["document_id"]
                        source_hash = item["source_hash"]
                        rel = item["relative_path"]
                        if item["action"] in {ACTION_NEW, ACTION_ZERO}:
                            subject_id = subject_id_pending(source_hash)
                            register_subject(conn, subject_id=subject_id)
                            register_document_version(
                                conn,
                                document_id=document_id,
                                subject_id=subject_id,
                                source_hash=source_hash,
                            )
                            registry_docs.append(document_id)
                            for rec in item["records"]:
                                register_chunk(
                                    conn,
                                    chunk_id=rec["chunk_id"],
                                    document_id=document_id,
                                    chunking_fingerprint=cfp,
                                    ordinal=int(rec["ordinal"]),
                                    content_hash=rec.get("content_hash"),
                                    page=rec.get("page")
                                    if isinstance(rec.get("page"), int)
                                    else None,
                                )
                                register_vector_mapping(
                                    conn,
                                    chunk_id=rec["chunk_id"],
                                    chroma_embedding_id=rec["chunk_id"],
                                    mapping_status=MAPPING_STATUS_NATIVE_CHUNK_ID,
                                )
                        # Alias for new, zero, and alias-only
                        register_source_file(
                            conn,
                            document_id=document_id,
                            relative_path=rel,
                            source_hash=source_hash,
                            collection=item.get("collection"),
                        )
                        registry_aliases.append((document_id, rel))
                    if _fail_after == "after_registry_prep":
                        raise CertifiedAppendError("injected failure during registry txn")
                journal["registry_document_ids"] = list(registry_docs)
                journal["registry_aliases"] = [list(a) for a in registry_aliases]
                journal["state"] = "REGISTRY_COMMITTED"
                write_journal(persist, journal)

                if _fail_after == "after_registry":
                    raise CertifiedAppendError("injected failure after registry")

                # --- TRACKER ---
                tracker = read_tracker(persist)
                for item in planned:
                    if item["action"] == ACTION_NO_CHANGE:
                        continue
                    source_hash = item["source_hash"]
                    rel = item["relative_path"]
                    if source_hash in tracker:
                        paths = list(tracker[source_hash].get("paths") or [])
                        if rel not in paths:
                            paths.append(rel)
                        tracker[source_hash]["paths"] = paths
                        # Do not duplicate chunk_ids
                        if item["action"] in {ACTION_NEW, ACTION_ZERO}:
                            tracker[source_hash]["chunk_ids"] = list(item["chunk_ids"])
                            tracker[source_hash]["document_id"] = item["document_id"]
                            tracker[source_hash]["collection"] = item["collection"]
                            tracker[source_hash]["extraction"] = item.get(
                                "extraction", "ok" if item["chunk_ids"] else "empty"
                            )
                    else:
                        tracker[source_hash] = tracker_entry(
                            paths=[rel],
                            chunk_ids=list(item["chunk_ids"]),
                            collection=item["collection"],
                            extraction=item.get(
                                "extraction", "ok" if item["chunk_ids"] else "empty"
                            ),
                            document_id=item["document_id"],
                        )
                if _fail_after == "tracker_write":
                    raise CertifiedAppendError("injected tracker write failure")
                write_tracker(persist, tracker)
                journal["state"] = "TRACKER_WRITTEN"
                write_journal(persist, journal)

            except Exception as exc:
                # Recovery: reverse completed stores without deleting pre-existing vectors
                try:
                    if chroma_written:
                        delete_chroma_ids(db, chroma_written)
                    restore_tracker_snapshot(persist, tracker_snap)
                    if registry_docs or registry_aliases:
                        with registry_transaction(conn):
                            compensating_registry_delete(
                                conn,
                                chunk_ids=list(chroma_written),
                                document_ids=list(registry_docs),
                                alias_rows=list(registry_aliases),
                            )
                    journal["state"] = "ROLLED_BACK"
                    journal["error"] = f"{type(exc).__name__}: {exc}"
                    write_journal(persist, journal)
                except Exception as rec_exc:
                    journal["state"] = "RECOVERY_FAILED"
                    journal["error"] = f"{type(exc).__name__}: {exc}"
                    journal["recovery_error"] = f"{type(rec_exc).__name__}: {rec_exc}"
                    write_journal(persist, journal)
                    raise AppendRecoveryError(
                        "append failed and recovery could not fully restore prior state",
                        details={"operation_id": operation_id, "journal": journal},
                    ) from rec_exc
                raise

            # v1 immutability
            if _sha256_file(sidecar_v1_path(persist)) != v1_sha_before:
                raise AppendGenerationGuardError("v1 was modified during append")

            # Summarize
            results = []
            action_summary: dict[str, int] = {}
            vector_delta = 0
            document_delta = 0
            alias_delta = 0
            for item in planned:
                action = item["action"]
                action_summary[action] = action_summary.get(action, 0) + 1
                if action == ACTION_NEW:
                    vector_delta += len(item["chunk_ids"])
                    document_delta += 1
                    alias_delta += 1
                elif action == ACTION_ZERO:
                    document_delta += 1
                    alias_delta += 1
                elif action == ACTION_ALIAS:
                    alias_delta += 1
                results.append(
                    {
                        "action": action,
                        "relative_path": item["relative_path"],
                        "document_id": item["document_id"],
                        "source_hash": item["source_hash"],
                        "chunk_ids": item["chunk_ids"],
                        "vector_count": len(item["chunk_ids"])
                        if action == ACTION_NEW
                        else 0,
                    }
                )

            recon = reconcile_append_affected(
                persist_dir=persist,
                registry_db=registry,
                generation_id=generation_id,
                chunking_fingerprint=cfp,
                affected=planned,
                db=db,
            )
            if not recon["ok"]:
                # Treat reconciliation failure as hard failure + recover
                try:
                    if chroma_written:
                        delete_chroma_ids(db, chroma_written)
                    restore_tracker_snapshot(persist, tracker_snap)
                    with registry_transaction(conn):
                        compensating_registry_delete(
                            conn,
                            chunk_ids=list(chroma_written),
                            document_ids=list(registry_docs),
                            alias_rows=list(registry_aliases),
                        )
                except Exception as rec_exc:
                    raise AppendRecoveryError(
                        "post-append reconciliation failed and recovery incomplete",
                        details={"reconciliation": recon, "operation_id": operation_id},
                    ) from rec_exc
                raise CertifiedAppendError(
                    "post-append reconciliation failed; changes rolled back",
                    details={"reconciliation": recon},
                )

            journal["state"] = "SUCCESS"
            journal["verification"] = recon
            write_journal(persist, journal)

            return {
                "ok": True,
                "operation_id": operation_id,
                "persist_dir": str(persist),
                "registry_db": str(registry),
                "generation_id": generation_id,
                "chunking_fingerprint": cfp,
                "results": results,
                "action_summary": action_summary,
                "vector_delta": vector_delta,
                "document_delta": document_delta,
                "alias_delta": alias_delta,
                "embed_calls": embed_calls["n"],
                "commit_model": COMMIT_MODEL,
                "v1_modified": False,
                "reconciliation": recon,
            }
        finally:
            conn.close()

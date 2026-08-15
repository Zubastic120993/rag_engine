"""Affected-set reconciliation after certified append."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from rag_engine.certified_generation.tracker import read_tracker
from rag_engine.metadata_registry import open_registry
from rag_engine.stable_identity.constants import MAPPING_STATUS_NATIVE_CHUNK_ID


def reconcile_append_affected(
    *,
    persist_dir: str | Path,
    registry_db: str | Path,
    generation_id: str,
    chunking_fingerprint: str,
    affected: Iterable[Mapping[str, Any]],
    db: Any | None = None,
) -> dict[str, Any]:
    """Verify document/chunk/vector/tracker/registry consistency for append set.

    ``affected`` items:
      - action: NEW_REVISION_APPEND | ALIAS_ONLY | NO_CHANGE | ZERO_VECTOR_VALID
      - source_hash, document_id, relative_path
      - chunk_ids (optional)
    """
    persist = Path(persist_dir)
    tracker = read_tracker(persist)
    conn = open_registry(registry_db)
    findings: list[dict[str, Any]] = []
    p0 = 0
    p1 = 0

    try:
        for item in affected:
            action = item.get("action")
            source_hash = str(item["source_hash"])
            document_id = str(item["document_id"])
            rel = str(item["relative_path"])
            chunk_ids = list(item.get("chunk_ids") or [])

            # Registry document
            dv = conn.execute(
                "SELECT document_id, source_hash FROM document_versions WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if dv is None:
                findings.append({"severity": "P0", "code": "REGISTRY_DOCUMENT_MISSING", "path": rel})
                p0 += 1
                continue
            if dv["source_hash"] != source_hash or document_id != f"docrev:{source_hash}":
                findings.append({"severity": "P0", "code": "DOCUMENT_HASH_MISMATCH", "path": rel})
                p0 += 1

            alias = conn.execute(
                "SELECT 1 FROM source_files WHERE document_id = ? AND relative_path = ?",
                (document_id, rel),
            ).fetchone()
            if alias is None:
                findings.append({"severity": "P0", "code": "ALIAS_MISSING", "path": rel})
                p0 += 1

            t_entry = tracker.get(source_hash)
            if t_entry is None:
                findings.append({"severity": "P0", "code": "TRACKER_MISSING", "path": rel})
                p0 += 1
            else:
                paths = list(t_entry.get("paths") or [])
                if rel not in paths:
                    findings.append({"severity": "P0", "code": "TRACKER_PATH_MISSING", "path": rel})
                    p0 += 1
                if t_entry.get("document_id") not in {None, document_id}:
                    findings.append({"severity": "P1", "code": "TRACKER_DOCUMENT_ID_MISMATCH", "path": rel})
                    p1 += 1
                t_chunks = list(t_entry.get("chunk_ids") or [])
                if action == "ZERO_VECTOR_VALID":
                    if t_chunks:
                        findings.append({"severity": "P0", "code": "ZERO_VECTOR_HAS_CHUNKS", "path": rel})
                        p0 += 1
                elif chunk_ids and sorted(t_chunks) != sorted(chunk_ids):
                    findings.append({"severity": "P0", "code": "TRACKER_CHUNK_MISMATCH", "path": rel})
                    p0 += 1

            if action in {"NEW_REVISION_APPEND", "ZERO_VECTOR_VALID"} or chunk_ids:
                for cid in chunk_ids:
                    crow = conn.execute(
                        "SELECT chunk_id, document_id, chunking_fingerprint FROM chunks WHERE chunk_id = ?",
                        (cid,),
                    ).fetchone()
                    if crow is None:
                        findings.append({"severity": "P0", "code": "REGISTRY_CHUNK_MISSING", "chunk_id": cid})
                        p0 += 1
                        continue
                    if crow["document_id"] != document_id:
                        findings.append({"severity": "P0", "code": "CHUNK_DOCUMENT_MISMATCH", "chunk_id": cid})
                        p0 += 1
                    if crow["chunking_fingerprint"] != chunking_fingerprint:
                        findings.append({"severity": "P0", "code": "CHUNK_CFP_MISMATCH", "chunk_id": cid})
                        p0 += 1
                    mrow = conn.execute(
                        "SELECT chroma_embedding_id, mapping_status FROM chunk_vector_map WHERE chunk_id = ?",
                        (cid,),
                    ).fetchone()
                    if mrow is None:
                        findings.append({"severity": "P0", "code": "MAPPING_MISSING", "chunk_id": cid})
                        p0 += 1
                    else:
                        if mrow["chroma_embedding_id"] != cid:
                            findings.append({"severity": "P0", "code": "VECTOR_ID_NE_CHUNK_ID", "chunk_id": cid})
                            p0 += 1
                        if mrow["mapping_status"] != MAPPING_STATUS_NATIVE_CHUNK_ID:
                            findings.append({"severity": "P0", "code": "NON_NATIVE_MAPPING", "chunk_id": cid})
                            p0 += 1

                if db is not None and chunk_ids:
                    got = db.get(ids=chunk_ids, include=["metadatas"])
                    got_ids = list(got.get("ids") or [])
                    if sorted(got_ids) != sorted(chunk_ids):
                        findings.append(
                            {
                                "severity": "P0",
                                "code": "CHROMA_IDS_MISSING",
                                "expected": chunk_ids,
                                "actual": got_ids,
                            }
                        )
                        p0 += 1
                    for meta in got.get("metadatas") or []:
                        meta = meta or {}
                        for field in (
                            "document_id",
                            "source_hash",
                            "chunk_id",
                            "embedding_generation_id",
                            "chunking_fingerprint",
                            "source",
                            "page",
                            "collection",
                        ):
                            if field not in meta:
                                findings.append({"severity": "P0", "code": "META_MISSING", "field": field})
                                p0 += 1
                        if meta.get("embedding_generation_id") != generation_id:
                            findings.append({"severity": "P0", "code": "GENERATION_MISMATCH"})
                            p0 += 1
                        if meta.get("chunking_fingerprint") != chunking_fingerprint:
                            findings.append({"severity": "P0", "code": "META_CFP_MISMATCH"})
                            p0 += 1
                        if meta.get("document_id") != document_id:
                            findings.append({"severity": "P0", "code": "META_DOCUMENT_MISMATCH"})
                            p0 += 1
                        if meta.get("chunk_id") and meta.get("chunk_id") not in got_ids:
                            findings.append({"severity": "P0", "code": "META_CHUNK_ID_DRIFT"})
                            p0 += 1
    finally:
        conn.close()

    return {
        "ok": p0 == 0 and p1 == 0,
        "p0": p0,
        "p1": p1,
        "findings": findings,
    }

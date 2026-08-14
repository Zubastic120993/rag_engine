"""Read-only generation inspect / structural compare. Never mutates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rag_engine.certified_generation.checkpoints import read_checkpoint
from rag_engine.certified_generation.paths import require_explicit_persist_dir
from rag_engine.certified_generation.tracker import read_tracker, tracker_id_kind
from rag_engine.index_compatibility.chroma_inspect import count_vectors_readonly
from rag_engine.index_compatibility.compatibility import evaluate_compatibility
from rag_engine.index_compatibility.constants import SIDECAR_V1_NAME
from rag_engine.index_compatibility.state import sidecar_v1_path


def inspect_generation(
    persist_dir: str | Path | None,
    *,
    registry_db: str | Path | None = None,
) -> dict[str, Any]:
    persist = require_explicit_persist_dir(persist_dir)
    tracker = read_tracker(persist) if persist.exists() else {}
    chunk_ids: list[str] = []
    kinds: dict[str, int] = {}
    for meta in tracker.values():
        if not isinstance(meta, dict):
            continue
        for cid in meta.get("chunk_ids") or []:
            chunk_ids.append(str(cid))
            kinds[tracker_id_kind(str(cid))] = kinds.get(tracker_id_kind(str(cid)), 0) + 1
    vectors = count_vectors_readonly(persist) if persist.exists() else 0
    compat = evaluate_compatibility(persist, registry_db=registry_db, vector_count=vectors)
    checkpoint = read_checkpoint(persist) if persist.exists() else None
    v1_present = sidecar_v1_path(persist).is_file() if persist.exists() else False
    mixed = False
    gens = set()
    # Tracker-level generation mixing is not stored; checkpoint generation is authority.
    if checkpoint:
        gens.add(checkpoint.get("generation_id"))
    return {
        "persist_dir": str(persist),
        "exists": persist.exists(),
        "generation_id": (checkpoint or {}).get("generation_id"),
        "checkpoint_state": (checkpoint or {}).get("state"),
        "accepted": False,
        "v1_present": v1_present,
        "v1_path": str(sidecar_v1_path(persist)),
        "v1_name": SIDECAR_V1_NAME,
        "compatibility_state": compat.state,
        "compatibility_reason": compat.reason,
        "vector_count": vectors,
        "tracker_entries": len(tracker),
        "tracker_chunk_ids": len(chunk_ids),
        "tracker_id_kinds": kinds,
        "document_count": len(tracker),
        "mixed_generation": mixed,
        "registry_db": str(registry_db) if registry_db else None,
        "mutated": False,
    }


def compare_generations(
    old_persist_dir: str | Path | None,
    new_persist_dir: str | Path | None,
) -> dict[str, Any]:
    old = inspect_generation(old_persist_dir)
    new = inspect_generation(new_persist_dir)
    return {
        "old": old,
        "new": new,
        "vector_count_delta": (new["vector_count"] or 0) - (old["vector_count"] or 0),
        "document_count_delta": (new["document_count"] or 0) - (old["document_count"] or 0),
        "old_compatibility": old["compatibility_state"],
        "new_compatibility": new["compatibility_state"],
        "structural_only": True,
        "ranking_unchanged": True,
        "mutated": False,
    }

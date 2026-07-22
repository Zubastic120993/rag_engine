"""Read-only scope / index diagnostics (no writes to Chroma)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from rag_engine.config import (
    chroma_client_settings,
    library_root,
    load_registry,
    persist_dir,
    track_file,
)
from rag_engine.scope_rules import explain_alias, explain_path_assignment


def _load_tracker() -> dict:
    tf = track_file()
    if not tf.exists():
        return {}
    try:
        return json.loads(tf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _normalize_rel(path: str) -> str:
    raw = path.strip().replace("\\", "/")
    root = library_root()
    p = Path(raw)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(root)).replace("\\", "/")
        except ValueError:
            return raw
    # strip leading ./
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def _chroma_metas_readonly() -> list[dict]:
    """Open Chroma and read metadatas only (no embedding calls for get)."""
    import chromadb

    client = chromadb.PersistentClient(
        path=str(persist_dir()), settings=chroma_client_settings()
    )
    cols = client.list_collections()
    if not cols:
        return []
    # LangChain default collection name
    name = cols[0].name
    col = client.get_collection(name)
    raw = col.get(include=["metadatas"])
    return list(raw.get("metadatas") or [])


def indexed_info_for_path(rel: str) -> dict[str, Any]:
    """Whether path is in tracker / Chroma and chunk counts."""
    norm = _normalize_rel(rel)
    tracker = _load_tracker()
    in_tracker = False
    digest = None
    for d, meta in tracker.items():
        paths = [str(p).replace("\\", "/") for p in (meta.get("paths") or [])]
        if norm in paths or any(p.endswith(norm) or norm.endswith(p) for p in paths):
            in_tracker = True
            digest = d
            break

    chunk_count = 0
    pages: set[int] = set()
    try:
        for m in _chroma_metas_readonly():
            if not m:
                continue
            src = str(m.get("source", "")).replace("\\", "/")
            if src == norm or src.endswith(norm) or norm.endswith(src):
                chunk_count += 1
                pg = m.get("page")
                if isinstance(pg, int):
                    pages.add(pg)
    except Exception as e:  # noqa: BLE001 — diagnostics must not crash CLI
        return {
            "indexed": in_tracker,
            "tracker_hash": digest,
            "chunk_count": None,
            "page_count": None,
            "chroma_error": str(e),
        }

    return {
        "indexed": in_tracker or chunk_count > 0,
        "tracker_hash": digest,
        "chunk_count": chunk_count,
        "page_count": len(pages),
        "pages_min": min(pages) if pages else None,
        "pages_max": max(pages) if pages else None,
    }


def explain_scope(path: str) -> dict[str, Any]:
    norm = _normalize_rel(path)
    assignment = explain_path_assignment(norm)
    indexed = indexed_info_for_path(norm)
    return {
        **assignment,
        "normalized_path": norm,
        **indexed,
    }


def explain_alias_with_counts(alias: str) -> dict[str, Any]:
    info = explain_alias(alias)
    scope = info["resolved_scope"]
    stats = scope_stats().get("scopes", {}).get(scope) or {}
    return {
        **info,
        "document_count": stats.get("document_count"),
        "chunk_count": stats.get("chunk_count"),
        "distinct_sources": stats.get("distinct_sources"),
    }


def scope_stats() -> dict[str, Any]:
    reg = load_registry()
    scopes_meta = reg.get("scopes") or {}
    chunk_by: Counter[str] = Counter()
    sources_by: dict[str, set[str]] = defaultdict(set)
    try:
        metas = _chroma_metas_readonly()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "scopes": {}}

    for m in metas:
        if not m:
            continue
        coll = str(m.get("collection") or "other")
        src = str(m.get("source") or "")
        chunk_by[coll] += 1
        if src:
            sources_by[coll].add(src.replace("\\", "/"))

    out: dict[str, Any] = {}
    for name, meta in scopes_meta.items():
        prefixes = list(meta.get("path_prefixes") or [])
        out[name] = {
            "chunk_count": int(chunk_by.get(name, 0)),
            "distinct_sources": len(sources_by.get(name, set())),
            "document_count": len(sources_by.get(name, set())),
            "path_prefixes": prefixes,
            "hermes_aliases": list(meta.get("hermes_aliases") or []),
        }
    return {"scopes": out, "total_chunks": sum(chunk_by.values())}

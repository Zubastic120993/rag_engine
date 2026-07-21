"""Index fingerprint — detect embed/chunk config drift without reindexing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rag_engine.config import (
    chunk_overlap,
    chunk_size,
    embed_model,
    llm_model,
    persist_dir,
)

FINGERPRINT_NAME = "index_fingerprint.json"
NORMALIZATION_SCHEME = "nfkc"


def fingerprint_path() -> Path:
    return persist_dir() / FINGERPRINT_NAME


def live_fingerprint() -> dict[str, Any]:
    return {
        "embed_model": embed_model(),
        "llm_model": llm_model(),
        "chunk_size": chunk_size(),
        "chunk_overlap": chunk_overlap(),
        "normalization": NORMALIZATION_SCHEME,
    }


def read_fingerprint() -> dict[str, Any] | None:
    path = fingerprint_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_fingerprint(extra: dict[str, Any] | None = None) -> Path:
    """Persist live fingerprint. Call from ingest only — not from doctor."""
    payload = live_fingerprint()
    if extra:
        payload.update(extra)
    path = fingerprint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def compare_fingerprint(
    stored: dict[str, Any] | None = None,
    live: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return match status and differing fields (stored vs live)."""
    stored = stored if stored is not None else read_fingerprint()
    live = live if live is not None else live_fingerprint()
    if stored is None:
        return {
            "status": "MISSING",
            "match": False,
            "diffs": ["fingerprint_file"],
            "stored": None,
            "live": live,
            "message": (
                f"No {FINGERPRINT_NAME} beside the index. "
                "It is written on the next successful sync/ingest; "
                "until then embed-model drift cannot be detected."
            ),
        }
    keys = ("embed_model", "chunk_size", "chunk_overlap", "normalization")
    # llm_model is informational at build time; mismatch is WARN not FAIL for retrieval
    diffs = [k for k in keys if stored.get(k) != live.get(k)]
    llm_diff = stored.get("llm_model") != live.get("llm_model")
    return {
        "status": "MATCH" if not diffs else "MISMATCH",
        "match": not diffs,
        "diffs": diffs,
        "llm_model_differs": llm_diff,
        "stored": {k: stored.get(k) for k in (*keys, "llm_model")},
        "live": live,
        "message": (
            "Index fingerprint matches live config"
            if not diffs
            else f"Fingerprint mismatch: {', '.join(diffs)}"
        ),
    }

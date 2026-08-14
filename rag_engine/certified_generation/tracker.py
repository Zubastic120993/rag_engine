"""Fresh generation tracker (embedded.json) - certified chunk IDs, not a copy of production."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRACKER_NAME = "embedded.json"


def tracker_path(persist: str | Path) -> Path:
    return Path(persist) / TRACKER_NAME


def empty_tracker() -> dict[str, Any]:
    return {}


def tracker_entry(
    *,
    paths: list[str],
    chunk_ids: list[str],
    collection: str,
    extraction: str,
    document_id: str,
) -> dict[str, Any]:
    """Reuse existing tracker shape; chunk_ids are stable chunk:<hex> values."""
    return {
        "paths": list(paths),
        "chunk_ids": list(chunk_ids),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "collection": collection,
        "extraction": extraction,
        "document_id": document_id,
    }


def write_tracker(persist: str | Path, tracker: dict[str, Any]) -> Path:
    path = tracker_path(persist)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    data = json.dumps(tracker, indent=2, ensure_ascii=False) + "\n"
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_tracker(persist: str | Path) -> dict[str, Any]:
    path = tracker_path(persist)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("tracker root must be object")
    return data


def looks_like_uuid(value: str) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split("-")
    return len(parts) == 5 and len(value) == 36


def looks_like_chunk_id(value: str) -> bool:
    return isinstance(value, str) and value.startswith("chunk:") and len(value) == 38


def tracker_id_kind(value: str) -> str:
    if looks_like_chunk_id(value):
        return "certified_chunk_id"
    if looks_like_uuid(value):
        return "legacy_uuid"
    return "unknown"

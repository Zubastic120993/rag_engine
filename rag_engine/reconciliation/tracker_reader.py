"""Read-only tracker (embedded.json) loader — never rewrites the file."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterator, Mapping

from rag_engine.reconciliation.models import TrackerRecord

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class TrackerReadError(ValueError):
    """Raised when tracker JSON cannot be parsed safely."""


def is_uuid_like(value: str) -> bool:
    return bool(isinstance(value, str) and _UUID_RE.match(value))


def load_tracker_readonly(path: str | Path) -> dict[str, TrackerRecord]:
    """Load tracker digests from ``embedded.json`` using a read-only open.

    Does not rewrite formatting. Does not normalize or save anything.
    """
    p = Path(path)
    if not p.is_file():
        raise TrackerReadError(f"tracker file does not exist: {p}")

    # O_RDONLY — never open for write.
    fd = os.open(str(p), os.O_RDONLY)
    try:
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as exc:
                raise TrackerReadError(f"malformed tracker JSON: {exc}") from exc
    except TrackerReadError:
        raise
    except OSError as exc:
        raise TrackerReadError(f"cannot read tracker: {exc}") from exc

    if not isinstance(data, dict):
        raise TrackerReadError("tracker root must be a JSON object keyed by digest")

    out: dict[str, TrackerRecord] = {}
    for digest, raw in data.items():
        if not isinstance(digest, str) or not digest.strip():
            raise TrackerReadError("tracker digest keys must be non-empty strings")
        if not isinstance(raw, dict):
            # Preserve as empty/malformed-capable record for classification
            out[digest] = TrackerRecord(
                source_hash=digest,
                source_paths=(),
                chunk_ids=(),
                collection=None,
                raw_record={"_malformed": True, "value_type": type(raw).__name__},
            )
            continue

        paths_raw = raw.get("paths") or []
        if isinstance(paths_raw, str):
            paths = (paths_raw,)
        elif isinstance(paths_raw, list):
            paths = tuple(str(x) for x in paths_raw if x is not None and str(x) != "")
        else:
            paths = ()

        ids_raw = raw.get("chunk_ids") or []
        if isinstance(ids_raw, list):
            chunk_ids = tuple(str(x) for x in ids_raw)
        else:
            chunk_ids = ()

        collection = raw.get("collection")
        if collection is not None:
            collection = str(collection)

        out[digest] = TrackerRecord(
            source_hash=digest,
            source_paths=paths,
            chunk_ids=chunk_ids,
            collection=collection,
            ingested_at=str(raw["ingested_at"]) if raw.get("ingested_at") is not None else None,
            status=str(raw["status"]) if raw.get("status") is not None else None,
            raw_record=dict(raw),
        )
    return out


def iter_tracker_chunk_ids(
    records: Mapping[str, TrackerRecord],
) -> Iterator[tuple[str, str]]:
    """Yield (digest, chunk_id) pairs."""
    for digest, rec in records.items():
        for cid in rec.chunk_ids:
            yield digest, cid


def audit_tracker(records: Mapping[str, TrackerRecord]) -> dict[str, Any]:
    """Deterministic audit summary of a loaded tracker."""
    zero_chunk = 0
    multi_path = 0
    missing_path = 0
    missing_collection = 0
    malformed = 0
    non_uuid = 0
    total_ids = 0
    owners: dict[str, list[str]] = {}

    for digest, rec in sorted(records.items()):
        if rec.raw_record.get("_malformed"):
            malformed += 1
        if not rec.chunk_ids:
            zero_chunk += 1
        if len(rec.source_paths) > 1:
            multi_path += 1
        if not rec.source_paths:
            missing_path += 1
        if not rec.collection:
            missing_collection += 1
        for cid in rec.chunk_ids:
            total_ids += 1
            owners.setdefault(cid, []).append(digest)
            if not is_uuid_like(cid):
                non_uuid += 1

    dup_ids = sorted(cid for cid, ds in owners.items() if len(ds) > 1)
    return {
        "tracker_digests": len(records),
        "tracker_chunk_ids": total_ids,
        "zero_chunk_records": zero_chunk,
        "multi_path_digests": multi_path,
        "missing_path_records": missing_path,
        "missing_collection_records": missing_collection,
        "malformed_entries": malformed,
        "non_uuid_chunk_ids": non_uuid,
        "duplicate_chunk_id_count": len(dup_ids),
        "duplicate_chunk_ids_sample": dup_ids[:20],
    }

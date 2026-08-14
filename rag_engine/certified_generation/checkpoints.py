"""Generation checkpoint: PREPARED / BUILDING / BUILT_UNVALIDATED only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from rag_engine.certified_generation.exceptions import CheckpointError
from rag_engine.certified_generation.ids import parse_generation_id

CHECKPOINT_NAME = "certified_generation_checkpoint.json"
SCHEMA = "certified-generation-checkpoint-v1"

STATE_PREPARED = "PREPARED"
STATE_BUILDING = "BUILDING"
STATE_BUILT_UNVALIDATED = "BUILT_UNVALIDATED"
ALLOWED_STATES = frozenset({STATE_PREPARED, STATE_BUILDING, STATE_BUILT_UNVALIDATED})
FORBIDDEN_STATES = frozenset({"VALIDATED", "ACCEPTED", "CUTOVER"})


def checkpoint_path(persist: str | Path) -> Path:
    return Path(persist) / CHECKPOINT_NAME


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_checkpoint(persist: str | Path, payload: dict[str, Any]) -> Path:
    state = payload.get("state")
    if state in FORBIDDEN_STATES:
        raise CheckpointError(f"B5I must not mark checkpoint {state}")
    if state not in ALLOWED_STATES:
        raise CheckpointError(f"unsupported checkpoint state: {state!r}")
    if payload.get("accepted") is True:
        raise CheckpointError("checkpoint must not set accepted=true")
    parse_generation_id(str(payload.get("generation_id") or ""))
    body = dict(payload)
    body["schema"] = SCHEMA
    body["accepted"] = False
    path = checkpoint_path(persist)
    _atomic_write(path, body)
    return path


def read_checkpoint(persist: str | Path) -> dict[str, Any] | None:
    path = checkpoint_path(persist)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"checkpoint unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckpointError("checkpoint root must be object")
    if data.get("accepted") is True:
        raise CheckpointError("corrupt checkpoint: accepted=true is forbidden in B5I")
    state = data.get("state")
    if state in FORBIDDEN_STATES:
        raise CheckpointError(f"corrupt checkpoint state {state}")
    if state not in ALLOWED_STATES:
        raise CheckpointError(f"unsupported checkpoint state: {state!r}")
    return data

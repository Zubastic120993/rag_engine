"""Append-only ask event log (esp. exit-2 / no_coverage) beside the index."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag_engine.config import persist_dir


def events_path() -> Path:
    return persist_dir() / "ask_events.jsonl"


def log_ask_event(
    *,
    question: str,
    scope: str | None,
    status: str,
    exit_code: int,
    sources: list[dict] | None = None,
) -> None:
    """Best-effort append. Never raises into the ask path."""
    try:
        persist_dir().mkdir(parents=True, exist_ok=True)
        row: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "question": question,
            "scope": scope,
            "status": status,
            "exit_code": exit_code,
            "n_sources": len(sources or []),
        }
        with events_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(
    *,
    status: str | None = "no_coverage",
    limit: int = 50,
) -> list[dict[str, Any]]:
    path = events_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if status is None or row.get("status") == status:
                rows.append(row)
    return rows[-limit:]

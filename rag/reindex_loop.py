#!/usr/bin/env python3
"""Resume-safe reindex loop. Survives native crashes by restarting ingest."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = Path("/tmp/rag_reindex.log")
MAX_ROUNDS = 120


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    for round_no in range(1, MAX_ROUNDS + 1):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"=== ROUND {round_no} {stamp} ===\n")
        proc = subprocess.run(
            [sys.executable, "-u", "-m", "rag.ingest"],
            cwd=str(ROOT),
            env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONUNBUFFERED": "1"},
            stdout=LOG.open("a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"=== exit {proc.returncode} ===\n")
        # Completed cleanly?
        tail = LOG.read_text(encoding="utf-8", errors="ignore")[-2000:]
        if "\nDone." in tail or tail.strip().endswith("Done.") or "\nDone. " in tail:
            with LOG.open("a", encoding="utf-8") as f:
                f.write("=== COMPLETE ===\n")
            return 0
        time.sleep(3)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

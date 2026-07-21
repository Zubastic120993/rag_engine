#!/usr/bin/env python3
"""Resume-safe reindex loop using short --max-new batches."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = Path("/tmp/rag_reindex.log")
MAX_ROUNDS = 500
BATCH = 5


def main() -> int:
    py = sys.executable
    for round_no in range(1, MAX_ROUNDS + 1):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"=== ROUND {round_no} {stamp} batch={BATCH} ===\n")
            f.flush()
            proc = subprocess.run(
                [py, "-u", "-m", "rag.ingest", "--max-new", str(BATCH)],
                cwd=str(ROOT),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                stdout=f,
                stderr=subprocess.STDOUT,
            )
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"=== exit {proc.returncode} ===\n")

        tail = LOG.read_text(encoding="utf-8", errors="ignore")[-3000:]
        if "\nMORE" in tail or tail.rstrip().endswith("MORE"):
            time.sleep(1)
            continue
        if "\nDone." in tail:
            with LOG.open("a", encoding="utf-8") as f:
                f.write("=== COMPLETE ===\n")
            return 0
        # Unexpected exit (crash) — resume anyway
        time.sleep(2)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

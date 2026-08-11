"""CLI for Phase 4 read-only reconciliation.

Requires explicit paths. Never writes to .rag_db / .rag_state.
Does not create production registries.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rag_engine.reconciliation.engine import reconcile_paths
from rag_engine.reconciliation.report import (
    sample_by_state,
    write_json_report,
    write_markdown_summary,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 4 read-only tracker↔Chroma↔registry reconciliation"
    )
    parser.add_argument("--tracker", required=True, help="Path to embedded.json")
    parser.add_argument(
        "--chroma-sqlite", required=True, help="Path to chroma.sqlite3"
    )
    parser.add_argument(
        "--library-root", default=None, help="Library root for source hash checks"
    )
    parser.add_argument(
        "--registry-db",
        default=None,
        help="Optional explicit registry sqlite (temp/non-production)",
    )
    parser.add_argument(
        "--index-fingerprint",
        default=None,
        help="Optional index_fingerprint.json (evidence only; not stable-id fingerprint)",
    )
    parser.add_argument(
        "--historical-chunking-fingerprint",
        default=None,
        help="Explicit historical Spec §7.1 fingerprint if known (never guessed)",
    )
    parser.add_argument(
        "--no-hash-sources",
        action="store_true",
        help="Skip hashing source files",
    )
    parser.add_argument(
        "--json-out",
        required=True,
        help="Explicit JSON report output path (not under .rag_db/.rag_state)",
    )
    parser.add_argument("--md-out", default=None, help="Optional Markdown summary path")
    parser.add_argument(
        "--include-all-results",
        action="store_true",
        help="Include full results array in JSON (can be large)",
    )
    args = parser.parse_args(argv)

    results, summary, audits = reconcile_paths(
        tracker_path=args.tracker,
        chroma_sqlite_path=args.chroma_sqlite,
        library_root=args.library_root,
        registry_db_path=args.registry_db,
        index_fingerprint_path=args.index_fingerprint,
        historical_chunking_fingerprint=args.historical_chunking_fingerprint,
        hash_existing_sources=not args.no_hash_sources,
    )
    samples = sample_by_state(results, limit=10)
    write_json_report(
        args.json_out,
        results,
        summary,
        audits=audits,
        meta={"cli": True},
        include_all_results=args.include_all_results,
    )
    # Always attach samples when not dumping all results
    if not args.include_all_results:
        path = Path(args.json_out)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["samples"] = samples
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.md_out:
        write_markdown_summary(
            args.md_out,
            summary,
            samples=samples,
            audits=audits,
            meta={"cli": True},
        )

    print(json.dumps(summary.to_dict()["by_state"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

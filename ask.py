#!/usr/bin/env python3
"""CLI for the local scoped RAG."""

from __future__ import annotations

import argparse
import sys

from rag.config import DEFAULT_K, KNOWN_SCOPES
from rag.query import answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask the local CE_Library + manuals RAG",
    )
    parser.add_argument(
        "--scope",
        choices=KNOWN_SCOPES,
        default=None,
        help="Filter retrieval to one collection (e.g. me-c, sms, wiki)",
    )
    parser.add_argument(
        "-k",
        type=int,
        default=DEFAULT_K,
        help=f"Number of chunks to retrieve (default {DEFAULT_K})",
    )
    parser.add_argument(
        "question",
        nargs=argparse.REMAINDER,
        help="Question text",
    )
    args = parser.parse_args(argv)
    question = " ".join(args.question).strip()
    if not question:
        parser.error("provide a question, e.g. ask.py --scope me-c \"lube oil temperature\"")

    text, sources = answer(question, scope=args.scope, k=args.k)
    print(text)
    if sources:
        print("\nSources:")
        for s in sources:
            print(f"  [{s['collection']}] {s['source']}  p.{s['page']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

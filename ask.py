#!/usr/bin/env python3
"""CLI for the local scoped RAG."""

from __future__ import annotations

import argparse

from rag.config import DEFAULT_K, HERMES_SCOPE_ALIASES, KNOWN_SCOPES, resolve_scope
from rag.query import answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask the local CE_Library + manuals RAG",
    )
    parser.add_argument(
        "--scope",
        default=None,
        metavar="NAME",
        help=(
            "Collection filter, or Hermes alias "
            f"(scopes: {', '.join(KNOWN_SCOPES)}; "
            f"aliases: {', '.join(sorted(HERMES_SCOPE_ALIASES))})"
        ),
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
        parser.error('provide a question, e.g. ask.py --scope me-c "lube oil temperature"')

    try:
        scope = resolve_scope(args.scope)
    except ValueError as e:
        parser.error(str(e))

    text, sources = answer(question, scope=scope, k=args.k)
    print(text)
    if sources:
        print("\nSources:")
        for s in sources:
            print(f"  [{s['collection']}] {s['source']}  p.{s['page']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

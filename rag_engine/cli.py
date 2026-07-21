"""Console entrypoints: rag-engine ask|ingest|list-scopes|…"""

from __future__ import annotations

import argparse
import json
import sys

from rag_engine.config import (
    default_k,
    hermes_aliases,
    known_scopes,
    library_root,
    list_scopes,
    persist_dir,
    resolve_scope,
)
from rag_engine.query import EXIT_ERROR, EXIT_NO_COVERAGE, EXIT_OK, answer


def cmd_list_scopes(as_json: bool) -> int:
    rows = list_scopes()
    if as_json:
        print(json.dumps({"scopes": rows}, indent=2))
    else:
        for r in rows:
            aliases = ", ".join(r["hermes_aliases"]) or "—"
            print(f"{r['name']:16s}  {r['description']}")
            print(f"{'':16s}  hermes: {aliases}")
    return EXIT_OK


def cmd_ask(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rag-engine ask")
    parser.add_argument("--scope", default=None, metavar="NAME")
    parser.add_argument("-k", type=int, default=None)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable Hermes contract on stdout",
    )
    parser.add_argument("question", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    question = " ".join(args.question).strip()
    if not question:
        payload = {
            "error": "missing question",
            "answer": None,
            "sources": [],
            "scope": None,
            "status": "error",
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print("provide a question", file=sys.stderr)
        return EXIT_ERROR

    try:
        scope = resolve_scope(args.scope)
    except ValueError as e:
        if args.json:
            print(
                json.dumps(
                    {
                        "error": str(e),
                        "answer": None,
                        "sources": [],
                        "scope": args.scope,
                        "status": "error",
                    }
                )
            )
        else:
            print(str(e), file=sys.stderr)
        return EXIT_ERROR

    try:
        text, sources, status = answer(
            question, scope=scope, k=args.k or default_k()
        )
    except Exception as e:
        if args.json:
            print(
                json.dumps(
                    {
                        "error": str(e),
                        "answer": None,
                        "sources": [],
                        "scope": scope,
                        "status": "error",
                    }
                )
            )
        else:
            print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(
            json.dumps(
                {
                    "answer": text,
                    "sources": sources,
                    "scope": scope,
                    "status": status,
                }
            )
        )
    else:
        print(text)
        if sources:
            print("\nSources:")
            for s in sources:
                print(
                    f"  [{s.get('collection')}] {s.get('path')}  "
                    f"p.{s.get('page')}  score={s.get('score')}"
                )

    if status in ("no_coverage", "empty_question"):
        return EXIT_NO_COVERAGE
    return EXIT_OK


def cmd_paths() -> int:
    print(
        json.dumps(
            {
                "library_root": str(library_root()),
                "db_path": str(persist_dir()),
                "scopes": known_scopes(),
                "hermes_aliases": hermes_aliases(),
            },
            indent=2,
        )
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: rag-engine <ask|ingest|list-scopes|paths|backfill|eval> …\n"
            "  ask [--scope NAME] [--json] QUESTION\n"
            "  ingest [--force] [--max-new N]\n"
            "  list-scopes [--json]\n"
            "  paths\n"
            "  backfill\n"
            "  eval [--retrieval-only]\n"
        )
        return EXIT_OK

    cmd, rest = argv[0], argv[1:]
    if cmd == "ask":
        return cmd_ask(rest)
    if cmd == "list-scopes":
        as_json = "--json" in rest
        return cmd_list_scopes(as_json)
    if cmd == "paths":
        return cmd_paths()
    if cmd == "ingest":
        from rag_engine.ingest import main as ingest_main

        sys.argv = ["rag-engine-ingest", *rest]
        ingest_main()
        return EXIT_OK
    if cmd == "backfill":
        from rag_engine.backfill_collections import main as backfill_main

        sys.argv = ["rag-engine-backfill", *rest]
        backfill_main()
        return EXIT_OK
    if cmd == "eval":
        from rag_engine.eval_run import main as eval_main

        return eval_main(rest)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

"""Console entrypoints: rag-engine ask|ingest|list-scopes|doctor|…"""

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
from rag_engine.events import events_path, log_ask_event, read_events
from rag_engine.query import EXIT_ERROR, EXIT_NO_COVERAGE, EXIT_OK, AskResult, answer


def _print_json(payload: dict) -> None:
    """Exactly one JSON document on stdout (Hermes contract)."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def cmd_list_scopes(as_json: bool) -> int:
    rows = list_scopes()
    if as_json:
        _print_json({"scopes": rows})
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
    parser.add_argument(
        "--suggest-scopes",
        action="store_true",
        help="On no_coverage, name other scopes with verified hits (opt-in)",
    )
    parser.add_argument("question", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    question = " ".join(args.question).strip()
    requested = args.scope

    if not question:
        result = AskResult(
            status="error",
            query="",
            requested_scope=requested,
            resolved_scope=None,
            answer=None,
            error="missing question",
        )
        if args.json:
            _print_json(result.to_json())
        else:
            print("provide a question", file=sys.stderr)
        return EXIT_ERROR

    try:
        resolved = resolve_scope(args.scope)
    except ValueError as e:
        result = AskResult(
            status="error",
            query=question,
            requested_scope=requested,
            resolved_scope=None,
            answer=None,
            error=str(e),
        )
        if args.json:
            _print_json(result.to_json())
        else:
            print(str(e), file=sys.stderr)
        return EXIT_ERROR

    try:
        result = answer(
            question,
            scope=resolved,
            k=args.k or default_k(),
            requested_scope=requested,
            suggest_scopes=args.suggest_scopes,
        )
    except Exception as e:  # noqa: BLE001
        result = AskResult(
            status="error",
            query=question,
            requested_scope=requested,
            resolved_scope=resolved,
            answer=None,
            error=str(e),
        )

    if result.status == "error":
        if args.json:
            _print_json(result.to_json())
        else:
            print(f"error: {result.error}", file=sys.stderr)
        log_ask_event(
            question=question,
            scope=resolved,
            status="error",
            exit_code=EXIT_ERROR,
            sources=[],
        )
        return EXIT_ERROR

    if args.json:
        _print_json(result.to_json())
    else:
        if result.status == "ok":
            print(result.answer or "")
            if result.sources:
                print("\nSources:")
                for s in result.sources:
                    print(
                        f"  [{s.get('collection')}] {s.get('path')}  "
                        f"p.{s.get('page')}  score={s.get('score')}"
                    )
        else:
            print(
                result.answer
                or "I do not know — not specified in the retrieved documents."
            )
            if result.hint:
                print(f"\nHint: {result.hint}", file=sys.stderr)

    code = (
        EXIT_NO_COVERAGE
        if result.status in ("no_coverage", "empty_question")
        else EXIT_OK
    )
    log_ask_event(
        question=question,
        scope=resolved,
        status=result.status,
        exit_code=code,
        sources=result.sources if result.status == "ok" else [],
    )
    return code


def cmd_paths() -> int:
    _print_json(
        {
            "library_root": str(library_root()),
            "db_path": str(persist_dir()),
            "events_log": str(events_path()),
            "scopes": known_scopes(),
            "hermes_aliases": hermes_aliases(),
        }
    )
    return EXIT_OK


def cmd_gaps(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rag-engine gaps")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include all statuses, not only no_coverage",
    )
    args = parser.parse_args(argv)
    status = None if args.all else "no_coverage"
    rows = read_events(status=status, limit=args.limit)
    if args.json:
        _print_json({"events": rows, "path": str(events_path())})
    elif not rows:
        print(f"No events yet ({events_path()})", file=sys.stderr)
    else:
        print(f"Recent coverage gaps ({events_path()}):")
        for r in rows:
            scope = r.get("scope") or "(all)"
            print(f"  {r.get('ts')}  [{scope}]  {r.get('question')}")
    return EXIT_OK


def cmd_explain_scope(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rag-engine explain-scope")
    parser.add_argument("path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    from rag_engine.diagnostics import explain_scope

    info = explain_scope(args.path)
    if args.json:
        _print_json(info)
    else:
        print(f"path:       {info.get('normalized_path')}")
        print(f"scope:      {info.get('scope')}")
        print(f"rule:       {info.get('rule')} → {info.get('matched')}")
        print(f"aliases:    {', '.join(info.get('aliases') or []) or '—'}")
        print(f"indexed:    {info.get('indexed')}")
        print(f"chunks:     {info.get('chunk_count')}")
        print(f"pages:      {info.get('page_count')} "
              f"(min={info.get('pages_min')} max={info.get('pages_max')})")
    return EXIT_OK


def cmd_explain_alias(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rag-engine explain-alias")
    parser.add_argument("alias")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    from rag_engine.diagnostics import explain_alias_with_counts

    try:
        info = explain_alias_with_counts(args.alias)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        _print_json(info)
    else:
        print(f"alias:            {info.get('alias')}")
        print(f"resolved_scope:   {info.get('resolved_scope')}")
        print(f"rule:             {info.get('rule')}")
        print(f"document_count:   {info.get('document_count')}")
        print(f"chunk_count:      {info.get('chunk_count')}")
    return EXIT_OK


def cmd_scope_stats(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rag-engine scope-stats")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    from rag_engine.diagnostics import scope_stats

    stats = scope_stats()
    if args.json:
        _print_json(stats)
        return EXIT_OK
    if stats.get("error"):
        print(stats["error"], file=sys.stderr)
        return EXIT_ERROR
    print(f"total_chunks: {stats.get('total_chunks')}")
    for name, row in (stats.get("scopes") or {}).items():
        print(
            f"{name:16s}  docs={row['document_count']:5d}  "
            f"chunks={row['chunk_count']:6d}  "
            f"prefixes={row['path_prefixes'] or '—'}"
        )
    return EXIT_OK


def cmd_doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rag-engine doctor")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--skip-ollama",
        action="store_true",
        help="Skip live Ollama reachability (for offline unit tests)",
    )
    args = parser.parse_args(argv)
    from rag_engine.doctor import run_doctor

    report = run_doctor(skip_ollama=args.skip_ollama)
    if args.json:
        _print_json(report)
    else:
        print(f"doctor: {report['status']}")
        for c in report["checks"]:
            mark = "OK " if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['name']}: {c['detail']}")
        fp = report.get("fingerprint") or {}
        print(f"fingerprint: {fp.get('status')} — {fp.get('message')}")
    return EXIT_OK if report["status"] == "PASS" else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: rag-engine <ask|sync|ingest|gaps|doctor|explain-scope|"
            "explain-alias|scope-stats|list-scopes|paths|backfill|eval> …\n"
            "  ask [--scope NAME] [--json] [--suggest-scopes] QUESTION\n"
            "  sync | ingest [--force] [--max-new N]\n"
            "  gaps [--limit N] [--json]\n"
            "  doctor [--json] [--skip-ollama]\n"
            "  explain-scope PATH [--json]\n"
            "  explain-alias ALIAS [--json]\n"
            "  scope-stats [--json]\n"
            "  list-scopes [--json]\n"
            "  paths\n"
            "  backfill\n"
            "  eval [--retrieval-only]\n",
            file=sys.stderr,
        )
        return EXIT_OK

    cmd, rest = argv[0], argv[1:]
    if cmd == "ask":
        return cmd_ask(rest)
    if cmd == "list-scopes":
        return cmd_list_scopes("--json" in rest)
    if cmd == "paths":
        return cmd_paths()
    if cmd in ("ingest", "sync"):
        from rag_engine.ingest import main as ingest_main

        sys.argv = ["rag-engine-ingest", *rest]
        ingest_main()
        return EXIT_OK
    if cmd == "gaps":
        return cmd_gaps(rest)
    if cmd == "doctor":
        return cmd_doctor(rest)
    if cmd == "explain-scope":
        return cmd_explain_scope(rest)
    if cmd == "explain-alias":
        return cmd_explain_alias(rest)
    if cmd == "scope-stats":
        return cmd_scope_stats(rest)
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

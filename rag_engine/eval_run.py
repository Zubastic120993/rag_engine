"""Run scoped RAG eval cases (retrieval-only under schema v4).

Legacy NL answer-scoring without --retrieval-only is deprecated: rag_engine
no longer generates answers (Hermes owns generation / refuse judgment).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rag_engine.query import retrieve

EVAL_PATH = Path(__file__).resolve().parent.parent / "eval" / "questions.json"


def _sources_ok(sources: list[dict], substrs: list[str]) -> bool:
    if not substrs:
        return True
    blob = " ".join(
        f"{s.get('path', s.get('source', ''))} {s.get('collection', '')}"
        for s in sources
    )
    return any(sub.lower() in blob.lower() for sub in substrs)


def _scope_ok(sources: list[dict], scope: str | None) -> bool:
    if not scope or not sources:
        return True
    return all(s.get("collection") == scope for s in sources)


def run_case(case: dict, *, retrieval_only: bool) -> dict:
    from rag_engine.pdf_links import citation_page_fields

    q = case["question"]
    scope = case.get("scope")
    expect_refuse = bool(case.get("expect_refuse"))
    substrs = case.get("source_substr") or []

    docs = retrieve(q, scope=scope, k=5)
    sources = []
    for d in docs:
        path = d.metadata.get("source")
        stored = d.metadata.get("page")
        fields = citation_page_fields(stored, source=str(path) if path else None)
        entry = {
            "path": path,
            "page": fields.get("page", stored),
            "collection": d.metadata.get("collection"),
        }
        if "page_index" in fields:
            entry["page_index"] = fields["page_index"]
        sources.append(entry)

    result = {
        "id": case["id"],
        "scope": scope,
        "expect_refuse": expect_refuse,
        "n_docs": len(docs),
        "scope_filter_ok": _scope_ok(sources, scope),
        "source_hint_ok": _sources_ok(sources, substrs) if not expect_refuse else True,
        "sources": sources[:5],
    }

    if not retrieval_only:
        raise SystemExit(
            "eval_run: schema v4 retrieval-only mode — engine answer generation "
            "is removed. Re-run with --retrieval-only (Hermes owns NL generation / "
            "refuse judgment). The legacy answer-scoring path is deprecated."
        )

    if expect_refuse:
        result["pass"] = True
        result["note"] = "retrieval-only: LLM refuse not checked"
    else:
        result["pass"] = (
            bool(docs) and result["scope_filter_ok"] and result["source_hint_ok"]
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local RAG eval set")
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="Evaluate retrieval hits only (required under schema v4).",
    )
    parser.add_argument("--ids", nargs="*", default=None)
    args = parser.parse_args(argv)

    if not args.retrieval_only:
        print(
            "ERROR: schema v4 retrieval-only mode requires --retrieval-only. "
            "Engine NL answer generation is removed; Hermes owns generation.",
            flush=True,
        )
        return 2

    data = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.ids:
        want = set(args.ids)
        cases = [c for c in cases if c["id"] in want]

    results = []
    for case in cases:
        print(f"→ {case['id']} (scope={case.get('scope')}) …", flush=True)
        r = run_case(case, retrieval_only=True)
        results.append(r)
        mark = "PASS" if r["pass"] else "FAIL"
        print(
            f"  {mark}  docs={r['n_docs']} refuse_expect={r['expect_refuse']}",
            flush=True,
        )

    out = Path("eval/last_results.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    passed = sum(1 for r in results if r["pass"])
    print(f"\n{passed}/{len(results)} passed")
    print(f"Wrote {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

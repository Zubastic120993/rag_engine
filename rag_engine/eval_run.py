"""Run scoped RAG eval cases (positives + refuse negatives)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from rag_engine.query import answer, retrieve

EVAL_PATH = Path(__file__).resolve().parent.parent / "eval" / "questions.json"

REFUSE_PATTERNS = [
    r"\bi do not know\b",
    r"\bi don't know\b",
    r"\bdo not know\b",
    r"\bdon't know\b",
    r"\bnot (?:found|specified|available|in (?:the )?(?:context|documents|library|manuals))\b",
    r"\bno (?:relevant|sufficient) (?:information|documents|context)\b",
    r"\bcannot (?:determine|answer|find)\b",
    r"\bto be confirmed\b",
    r"\bnot specified\b",
]


def _looks_like_refuse(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in REFUSE_PATTERNS)


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
    q = case["question"]
    scope = case.get("scope")
    expect_refuse = bool(case.get("expect_refuse"))
    substrs = case.get("source_substr") or []

    docs = retrieve(q, scope=scope, k=5)
    sources = [
        {
            "path": d.metadata.get("source"),
            "page": d.metadata.get("page"),
            "collection": d.metadata.get("collection"),
        }
        for d in docs
    ]

    result = {
        "id": case["id"],
        "scope": scope,
        "expect_refuse": expect_refuse,
        "n_docs": len(docs),
        "scope_filter_ok": _scope_ok(sources, scope),
        "source_hint_ok": _sources_ok(sources, substrs) if not expect_refuse else True,
        "sources": sources[:5],
    }

    if retrieval_only:
        if expect_refuse:
            result["pass"] = True
            result["note"] = "retrieval-only: LLM refuse not checked"
        else:
            result["pass"] = (
                bool(docs) and result["scope_filter_ok"] and result["source_hint_ok"]
            )
        return result

    text, ans_sources, status = answer(q, scope=scope, k=5)
    result["answer_preview"] = text[:400]
    result["sources"] = ans_sources[:5]
    result["status"] = status
    result["scope_filter_ok"] = _scope_ok(ans_sources, scope)
    result["refused"] = _looks_like_refuse(text) or status == "no_coverage"

    if expect_refuse:
        result["pass"] = result["refused"] and result["scope_filter_ok"]
    else:
        result["pass"] = (
            (not result["refused"])
            and result["scope_filter_ok"]
            and _sources_ok(ans_sources, substrs)
            and bool(ans_sources)
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local RAG eval set")
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument("--ids", nargs="*", default=None)
    args = parser.parse_args(argv)

    data = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    if args.ids:
        want = set(args.ids)
        cases = [c for c in cases if c["id"] in want]

    results = []
    for case in cases:
        print(f"→ {case['id']} (scope={case.get('scope')}) …", flush=True)
        r = run_case(case, retrieval_only=args.retrieval_only)
        results.append(r)
        mark = "PASS" if r["pass"] else "FAIL"
        print(f"  {mark}  docs={r['n_docs']} refuse_expect={r['expect_refuse']}", flush=True)

    passed = sum(1 for r in results if r["pass"])
    print(f"\n{passed}/{len(results)} passed")
    out = Path("eval/last_results.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

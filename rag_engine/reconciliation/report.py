"""Deterministic reconciliation report writers (explicit output paths only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rag_engine.reconciliation.models import ReconciliationResult, ReconciliationSummary


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def results_to_jsonable(
    results: Sequence[ReconciliationResult],
    summary: ReconciliationSummary,
    *,
    audits: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "meta": {
            "generated_at": _utc_now(),
            "phase": "phase4_reconciliation",
            "mutates_production": False,
            **dict(meta or {}),
        },
        "summary": summary.to_dict(),
        "audits": dict(audits or {}),
        "results": [r.to_dict() for r in results],
    }


def summarize_reconciliation(
    results: Sequence[ReconciliationResult],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.state.value] = counts.get(r.state.value, 0) + 1
    return dict(sorted(counts.items()))


def sample_by_state(
    results: Sequence[ReconciliationResult],
    *,
    limit: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        if r.state.value == "MATCH":
            continue
        bucket = buckets.setdefault(r.state.value, [])
        if len(bucket) < limit:
            bucket.append(r.to_dict())
    return {k: buckets[k] for k in sorted(buckets)}


def write_json_report(
    path: str | Path,
    results: Sequence[ReconciliationResult],
    summary: ReconciliationSummary,
    *,
    audits: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
    include_all_results: bool = True,
) -> Path:
    """Write JSON report to an explicit path (must not be under .rag_db/.rag_state)."""
    out = Path(path)
    _reject_production_output(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = results_to_jsonable(
        results if include_all_results else [],
        summary,
        audits=audits,
        meta=meta,
    )
    if not include_all_results:
        payload["samples"] = sample_by_state(results)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out


def write_markdown_summary(
    path: str | Path,
    summary: ReconciliationSummary,
    *,
    samples: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    audits: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> Path:
    out = Path(path)
    _reject_production_output(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Phase 4 Reconciliation Summary",
        "",
        f"Generated: `{_utc_now()}`",
        "",
        "Phase 4 classifies only. It does not repair, backfill, re-index, or rewrite UUIDs.",
        "",
        "## Counts by state",
        "",
    ]
    for state, n in summary.by_state.items():
        lines.append(f"- **{state}**: {n}")
    lines.extend(
        [
            "",
            "## Join",
            "",
            f"- tracker digests: {summary.tracker_records}",
            f"- tracker chunk IDs: {summary.tracker_chunk_ids}",
            f"- chroma records: {summary.chroma_records}",
        ]
    )
    for k, v in summary.join.items():
        lines.append(f"- {k}: {v}")
    lines.extend(["", "## Hash", ""])
    for k, v in summary.hash_stats.items():
        lines.append(f"- {k}: {v}")
    lines.extend(
        [
            "",
            f"## Historical fingerprint status: `{summary.historical_fingerprint_status}`",
            "",
            f"## Extractor version status: `{summary.extractor_version_status}`",
            "",
            f"- stable chunk IDs proven: {summary.stable_chunk_ids_proven}",
            f"- stable chunk IDs unresolved: {summary.stable_chunk_ids_unresolved}",
            "",
        ]
    )
    if audits:
        lines.extend(["## Audits", "", "```json", json.dumps(audits, indent=2, sort_keys=True), "```", ""])
    if samples:
        lines.extend(["## Sample non-MATCH findings", ""])
        for state, items in samples.items():
            lines.append(f"### {state} (up to {len(items)})")
            lines.append("")
            for item in items:
                lines.append(
                    f"- `{item.get('unit_kind')}` `{item.get('unit_id')}` "
                    f"reasons={item.get('reason_codes')}"
                )
            lines.append("")
    if meta:
        lines.extend(["## Meta", "", "```json", json.dumps(dict(meta), indent=2, sort_keys=True), "```", ""])
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out


def _reject_production_output(path: Path) -> None:
    parts = {p.lower() for p in path.resolve().parts}
    if ".rag_db" in parts or ".rag_state" in parts:
        raise ValueError(
            "refusing to write reconciliation report under .rag_db or .rag_state"
        )

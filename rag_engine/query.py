"""Shared retrieval + answer synthesis for CLI and Gradio."""

from __future__ import annotations

import os
import re
import time
from math import isfinite
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

from rag_engine.authority import enrich_metadata
from rag_engine.config import (
    chroma_client_settings,
    default_k,
    embed_model,
    known_scopes,
    llm_model,
    persist_dir,
    retrieval_score_max,
)
from rag_engine.openai_generation import clear_caches as clear_generation_caches
from rag_engine.openai_generation import invoke_openai_response
from rag_engine.scope_rules import scope_allows_candidate
from rag_engine.text import normalize_text

# Exit semantics for CLI / Hermes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_COVERAGE = 2

# F-18 added retrieval_evidence + gate, both populated regardless of status.
# No SCHEMA_VERSION bump: purely additive -- every v3 field keeps its exact
# existing name, meaning, and population rule (including sources[], still
# emptied on any non-"ok" status). A consumer that ignores unknown keys
# needs no version signal for this kind of change. Reverted from v4 after
# closeout found ~/.hermes/plugins/ce-rag's own test suite pinned a
# schema_version fixture to 3 and broke on the bump -- its production
# code path never reads schema_version at all (fails open, not a live
# break), but the test regression was real. v3: sources[].score renamed
# to sources[].distance. v2: plain-text generation, no partial_coverage.
SCHEMA_VERSION = 3

DEFAULT_NO_COVERAGE_HINT = (
    "No supporting chunks were found in this scope. "
    "The requested document may belong to another scope; "
    "use scope diagnostics rather than answering from model memory."
)

_STATUSES_WITH_SOURCES = frozenset({"ok"})


def ollama_timeout_s() -> float:
    # Default 300s: retrieval (embedding) calls under load exceed 120s and
    # must still surface as exit 1 rather than hang forever for Hermes.
    # Retrieval only. OpenAI generation uses rag_engine.openai_generation.
    return float(os.environ.get("RAG_OLLAMA_TIMEOUT", "300"))


def suggest_score_max() -> float:
    """Chroma distance threshold: lower is better; keep hits at or below this."""
    return float(os.environ.get("RAG_SUGGEST_SCORE_MAX", "1.2"))


def conservative_result_score_max() -> float:
    """Stricter threshold for converting retrieval into source-preserving success.

    This must be tighter than alternate-scope suggestion: preserving sources as
    an `ok` result is stronger than merely hinting that another scope may help.
    """
    return float(os.environ.get("RAG_CONSERVATIVE_RESULT_SCORE_MAX", "0.7"))


def retrieval_search_width() -> int:
    """How many candidates to actually query Chroma for (F-17).

    hnswlib's search_ef is fixed at index-construction time (10, this store's
    default) and is never revisited per query; requesting n_results > ef forces
    the underlying search to widen regardless (confirmed against installed
    chromadb source, round 7). Recall@5 measured against exact brute-force
    ground truth over the full corpus plateaus at this width (round 7 §3,
    round 8). Querying at this width and truncating to the caller's k — done
    in retrieve_with_scores, never here alone — is what actually changes
    ranking quality; this constant only sets how wide that internal query is.
    """
    return int(os.environ.get("RAG_RETRIEVAL_SEARCH_WIDTH", "400"))


def _round_s(seconds: float | None) -> float | None:
    if seconds is None:
        return None
    return round(float(seconds), 3)


def _empty_timings(
    *,
    scope_resolution: float | None = None,
    retrieval: float | None = None,
    generation_primary: float | None = None,
    generation_repair: float | None = None,
    generation_fallback: float | None = None,
    generation: float | None = None,
    total: float | None = None,
) -> dict[str, float | None]:
    return {
        "scope_resolution": _round_s(scope_resolution),
        "retrieval": _round_s(retrieval),
        "generation_primary": _round_s(generation_primary),
        "generation_repair": _round_s(generation_repair),
        "generation_fallback": _round_s(generation_fallback),
        "generation": _round_s(generation),
        "total": _round_s(total),
    }


@dataclass
class AskResult:
    status: str  # ok | no_coverage | clarification_required | empty_question | error
    query: str
    requested_scope: str | None
    resolved_scope: str | None
    answer: str | None = None
    sources: list[dict] = field(default_factory=list)
    hint: str | None = None
    error: str | None = None
    coverage: str | None = None  # full | none (partial is no longer produced)
    missing_information: str | None = None
    timings: dict[str, float | None] = field(default_factory=_empty_timings)
    model: str | None = None
    best_distance: float | None = None
    score_floor: float | None = None
    # F-18: what retrieval actually found, independent of `sources` and its
    # status-gated emptying below. Always reflects retrieve_with_scores()'s
    # real output -- [] only when retrieval genuinely found nothing (or
    # never ran, e.g. empty_question / a retrieval-stage error), never
    # emptied just because the final status ended up non-"ok". This is what
    # tells "retrieval found nothing" apart from "retrieval found weak
    # matches and the model declined" -- indistinguishable via `sources`
    # alone before this field existed.
    retrieval_evidence: list[dict] = field(default_factory=list)
    retrieval_diagnostics: dict[str, Any] = field(default_factory=dict)
    # F-18: which internal gate produced a non-"ok" status. None for "ok"
    # (nothing to explain) and left unset only where no gate exists yet to
    # attribute a status to (there is currently no such case, but the type
    # stays optional rather than assuming every future non-"ok" path names
    # one). See the gate name -> meaning table in
    # F18_cli_hides_retrieval_evidence_20260727.md.
    gate: str | None = None

    def to_json(self) -> dict[str, Any]:
        keep_sources = self.status in _STATUSES_WITH_SOURCES
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "query": self.query,
            "requested_scope": self.requested_scope,
            "resolved_scope": self.resolved_scope,
            "coverage": self.coverage,
            "answer": self.answer,
            "missing_information": self.missing_information,
            "sources": self.sources if keep_sources else [],
            "retrieval_evidence": self.retrieval_evidence,
            "retrieval_diagnostics": self.retrieval_diagnostics,
            "gate": self.gate,
            "timings": self.timings or _empty_timings(),
            "model": self.model,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.error is not None or self.status == "error":
            payload["error"] = self.error or "unknown error"
        # legacy convenience for older callers
        payload["scope"] = self.resolved_scope
        return payload


@lru_cache(maxsize=1)
def _get_db() -> Chroma:
    embeddings = OllamaEmbeddings(model=embed_model())
    return Chroma(
        persist_directory=str(persist_dir()),
        embedding_function=embeddings,
        client_settings=chroma_client_settings(),
    )


def clear_caches() -> None:
    _get_db.cache_clear()
    clear_generation_caches()


def resolve_answer_model(model: str | None = None) -> str:
    """Explicit model override, else configured default OpenAI model."""
    if model:
        return model
    return llm_model()


def _run_with_timeout(fn, timeout: float | None = None):
    timeout = ollama_timeout_s() if timeout is None else timeout
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeout as e:
            raise TimeoutError(
                f"Ollama call timed out after {timeout}s "
                f"(RAG_OLLAMA_TIMEOUT)"
            ) from e


def authority_preference_distance_window() -> float:
    """Within this distance band, prefer the stronger authority source.

    The score floor already discards weak hits entirely. Inside the surviving
    set, authority should decide only when distances are close enough to be
    materially comparable; it must not override a much closer hit just because
    that hit comes from a lower-ranked source.
    """
    return 0.05


def _candidate_sort_key(
    item: tuple[Any, float],
    *,
    family_support: dict[str, int] | None = None,
    source_support: dict[str, int] | None = None,
) -> tuple[int, int, int, int, int, int, float, str, Any]:
    doc, distance = item
    meta = enrich_metadata(doc.metadata)
    doc.metadata = meta
    band = int(float(distance) / authority_preference_distance_window())
    family = str(meta.get("authority_family", ""))
    support = 0 if family_support is None else int(family_support.get(family, 0))
    source = str(meta.get("source", ""))
    source_coherence = 0 if source_support is None else int(source_support.get(source, 0))
    return (
        band,
        int(meta.get("canonical_authority_rank", meta.get("authority_rank", 5))),
        -source_coherence,
        int(meta.get("document_type_rank", 5)),
        -support,
        int(meta.get("authority_rank", 5)),
        float(distance),
        source,
        meta.get("page", "?"),
    )


def _empty_retrieval_diagnostics() -> dict[str, Any]:
    return {
        "score_floor": retrieval_score_max(),
        "best_raw_distance": None,
        "raw_count": 0,
        "post_admissibility_count": 0,
        "post_scope_count": 0,
        "post_rerank_count": 0,
        "post_dedupe_count": 0,
        "final_retained_count": 0,
        "final_confidence_passed": False,
        "gate": None,
    }


def _is_broadly_admissible(doc: Any, distance: float) -> bool:
    try:
        value = float(distance)
    except (TypeError, ValueError):
        return False
    if not isfinite(value):
        return False
    meta = enrich_metadata(getattr(doc, "metadata", {}) or {})
    doc.metadata = meta
    return bool(str(meta.get("source", "")).strip())


def _apply_broad_admissibility(
    pairs: list[tuple[Any, float]],
) -> list[tuple[Any, float]]:
    admissible: list[tuple[Any, float]] = []
    for doc, distance in pairs:
        if _is_broadly_admissible(doc, distance):
            admissible.append((doc, float(distance)))
    return admissible


def _support_maps(pairs: list[tuple[Any, float]]) -> tuple[dict[str, int], dict[str, int]]:
    family_support: dict[str, int] = {}
    source_support: dict[str, int] = {}
    for doc, _distance in pairs:
        meta = enrich_metadata(doc.metadata)
        doc.metadata = meta
        family = str(meta.get("authority_family", ""))
        if family:
            family_support[family] = family_support.get(family, 0) + 1
        source = str(meta.get("source", ""))
        if source:
            source_support[source] = source_support.get(source, 0) + 1
    return family_support, source_support


def _dedupe_ranked_pairs(ranked: list[tuple[Any, float]]) -> list[tuple[Any, float]]:
    deduped: list[tuple[Any, float]] = []
    seen: set[tuple[str, Any, str]] = set()
    for doc, distance in ranked:
        meta = enrich_metadata(doc.metadata)
        doc.metadata = meta
        key = (
            str(meta.get("source", "")),
            meta.get("page", "?"),
            str(meta.get("collection", "other")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append((doc, distance))
    return deduped


def _apply_final_confidence_gate(
    pairs: list[tuple[Any, float]],
    *,
    diagnostics: dict[str, Any],
) -> tuple[list[tuple[Any, float]], dict[str, Any]]:
    diag = dict(diagnostics)
    if not pairs:
        diag["final_retained_count"] = 0
        diag["final_confidence_passed"] = False
        return [], diag

    family_support, source_support = _support_maps(pairs)
    top_doc, top_distance = pairs[0]
    top_meta = enrich_metadata(top_doc.metadata)
    top_doc.metadata = top_meta

    top_source = str(top_meta.get("source", ""))
    top_family = str(top_meta.get("authority_family", ""))
    top_document_type = str(top_meta.get("document_type", ""))
    top_canonical_authority_rank = int(
        top_meta.get("canonical_authority_rank", top_meta.get("authority_rank", 5))
    )
    top_source_support = int(source_support.get(top_source, 0))
    top_family_support = int(family_support.get(top_family, 0))
    strong_distance = (
        diag.get("best_raw_distance") is not None
        and float(diag["best_raw_distance"]) <= float(diag.get("score_floor", retrieval_score_max()))
    )
    allowed_document_types = {"operation_manual", "spare_parts_catalogue", "maker_manual"}
    top_is_authoritative = (
        top_canonical_authority_rank <= 2
        or (top_canonical_authority_rank <= 3 and top_document_type in allowed_document_types)
    )
    coherent_support = (
        top_source_support >= 2
        or top_family_support >= 2
        or (len(pairs) >= 2 and single_source_consensus(pairs, top_n=min(3, len(pairs))))
    )
    final_pass = bool(
        top_is_authoritative
        and (strong_distance or coherent_support or top_canonical_authority_rank <= 2)
    )

    diag.update(
        {
            "top_distance": float(top_distance),
            "top_source": top_source,
            "top_authority_family": top_family,
            "top_document_type": top_document_type,
            "top_authority_rank": int(top_meta.get("authority_rank", 5)),
            "top_canonical_authority_rank": top_canonical_authority_rank,
            "top_source_support": top_source_support,
            "top_family_support": top_family_support,
            "strong_distance": strong_distance,
            "coherent_support": coherent_support,
            "final_confidence_passed": final_pass,
            "final_retained_count": len(pairs) if final_pass else 0,
        }
    )
    if not final_pass:
        diag["gate"] = "final_confidence_failed"
        return [], diag
    return pairs, diag


def _apply_retrieval_controls(
    pairs: list[tuple[Any, float]],
    *,
    scope: str | None,
    k: int,
) -> tuple[list[tuple[Any, float]], dict[str, Any]]:
    floor = retrieval_score_max()
    best_raw_distance = best_distance(pairs)
    admissible = _apply_broad_admissibility(pairs)
    scope_filtered: list[tuple[Any, float]] = []
    for doc, distance in admissible:
        meta = enrich_metadata(doc.metadata)
        doc.metadata = meta
        if scope_allows_candidate(scope, meta):
            scope_filtered.append((doc, float(distance)))
    family_support, source_support = _support_maps(scope_filtered)
    ranked = sorted(
        scope_filtered,
        key=lambda item: _candidate_sort_key(
            item,
            family_support=family_support,
            source_support=source_support,
        ),
    )
    deduped = _dedupe_ranked_pairs(ranked)

    gate = None
    if pairs and not admissible:
        gate = "broad_admissibility_failed"
    diagnostics = {
        "score_floor": floor,
        "best_raw_distance": best_raw_distance,
        "raw_count": len(pairs),
        "post_admissibility_count": len(admissible),
        "post_scope_count": len(scope_filtered),
        "post_rerank_count": len(ranked),
        "post_dedupe_count": len(deduped),
        "final_retained_count": 0,
        "final_confidence_passed": False,
        "gate": gate,
    }
    return deduped[:k], diagnostics


def retrieve_with_scores(
    question: str,
    scope: str | None = None,
    k: int | None = None,
) -> list[tuple[Any, float]]:
    db = _get_db()
    k = default_k() if k is None else k
    # Query wide (F-17: hnswlib recall is width-bound, not just ef-bound — see
    # retrieval_search_width()), but truncate to k before returning. Callers
    # (answer()'s sources, LLM context, conservative-success check) must never
    # see more than k candidates — only ranking quality should change, not
    # how many chunks reach the context.
    search_k = max(k, retrieval_search_width())
    q = normalize_text(question)

    def _call():
        kwargs: dict[str, Any] = {"k": search_k}
        if scope:
            kwargs["filter"] = {"collection": scope}
        return db.similarity_search_with_score(q, **kwargs)

    raw_results = _run_with_timeout(_call)
    results, _diagnostics = _apply_retrieval_controls(raw_results, scope=scope, k=k)
    return results


def retrieve_with_scores_and_diagnostics(
    question: str,
    scope: str | None = None,
    k: int | None = None,
) -> tuple[list[tuple[Any, float]], dict[str, Any]]:
    db = _get_db()
    k = default_k() if k is None else k
    search_k = max(k, retrieval_search_width())
    q = normalize_text(question)

    def _call():
        kwargs: dict[str, Any] = {"k": search_k}
        if scope:
            kwargs["filter"] = {"collection": scope}
        return db.similarity_search_with_score(q, **kwargs)

    raw_results = _run_with_timeout(_call)
    return _apply_retrieval_controls(raw_results, scope=scope, k=k)


def retrieve(question: str, scope: str | None = None, k: int | None = None):
    return [doc for doc, _ in retrieve_with_scores(question, scope=scope, k=k)]


def _sources_from_pairs(pairs: list[tuple[Any, float]]) -> list[dict]:
    sources: list[dict] = []
    seen: set[tuple] = set()
    for doc, distance in pairs:
        meta = enrich_metadata(doc.metadata)
        doc.metadata = meta
        src = meta.get("source", "unknown")
        page = meta.get("page", "?")
        coll = meta.get("collection", "other")
        key = (src, page, coll)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "path": src,
                "page": page,
                "collection": coll,
                # Chroma L2 distance: lower = closer/more relevant.
                "distance": float(distance),
                "authority_rank": int(meta.get("authority_rank", 5)),
                "machine_transcribed": bool(meta.get("machine_transcribed", False)),
            }
        )
    return sources


_SOURCE_ONLY_PATTERNS = (
    re.compile(r"\bfind the manual\b"),
    re.compile(r"\bfind the document\b"),
    re.compile(r"\blocate the document\b"),
    re.compile(r"\bshow the source\b"),
    re.compile(r"\bshow source details\b"),
    re.compile(r"\breturn source details only\b"),
    re.compile(r"\bsource details only\b"),
    re.compile(r"\bsource only\b"),
    re.compile(r"\bwhich document\b"),
    re.compile(r"\bwhere is this manual\b"),
)


def is_source_only_query(question: str) -> bool:
    """Narrow intent detector for document-locator / source-only queries."""
    q = normalize_text(question or "").lower()
    return any(p.search(q) for p in _SOURCE_ONLY_PATTERNS)


def best_distance(pairs: list[tuple[Any, float]]) -> float | None:
    if not pairs:
        return None
    return min(float(score) for _, score in pairs)


def single_source_consensus(pairs: list[tuple[Any, float]], top_n: int = 3) -> bool:
    """Top retrieved hits all point at the same concrete source path."""
    top = pairs[: max(1, top_n)]
    if not top:
        return False
    paths = [str((doc.metadata or {}).get("source", "")).strip() for doc, _ in top]
    return bool(paths[0]) and len(set(paths)) == 1


def retrieval_is_conservative_success(pairs: list[tuple[Any, float]]) -> bool:
    """Strong enough retrieval evidence to preserve sources without detail claims."""
    distance = best_distance(pairs)
    if distance is None:
        return False
    return distance <= conservative_result_score_max() and single_source_consensus(
        pairs, top_n=min(3, len(pairs))
    )


def build_source_only_answer(sources: list[dict], resolved: str | None) -> str | None:
    """Deterministic source-only response built from retrieval metadata only."""
    if not sources:
        return None
    if resolved:
        return f"Relevant document found in scope {resolved}. See listed source pages."
    return "Relevant document found. See listed source pages."


def suggest_other_scopes(
    question: str,
    *,
    exclude: str | None,
    k: int = 3,
) -> list[dict[str, Any]]:
    """Opt-in: find other scopes with real hits under the distance threshold."""
    threshold = suggest_score_max()
    found: list[dict[str, Any]] = []
    for scope in known_scopes():
        if exclude and scope == exclude:
            continue
        try:
            pairs = retrieve_with_scores(question, scope=scope, k=k)
        except Exception:  # noqa: BLE001
            continue
        good = [(d, s) for d, s in pairs if float(s) <= threshold]
        if not good:
            continue
        best = min(float(s) for _, s in good)
        paths = sorted(
            {
                str((d.metadata or {}).get("source", ""))
                for d, _ in good
                if (d.metadata or {}).get("source")
            }
        )[:3]
        found.append(
            {
                "scope": scope,
                "best_score": best,
                "n_hits": len(good),
                "sample_paths": paths,
            }
        )
    found.sort(key=lambda r: r["best_score"])
    return found


NOT_IN_CONTEXT_TOKEN = "NOT_IN_CONTEXT"

_NOT_IN_CONTEXT_RE = re.compile(r"^\s*NOT_IN_CONTEXT\b")


def model_declared_not_in_context(raw: str) -> bool:
    """Deterministic sentinel check: first non-empty line starts NOT_IN_CONTEXT.

    This is the only signal parsed out of model output — never prose heuristics.
    """
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return bool(_NOT_IN_CONTEXT_RE.match(stripped))
    return False


def clean_answer_text(raw: str) -> str:
    """Deterministic cleanup only: trim, and unwrap a whole-answer code fence."""
    text = (raw or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return text


def _build_prompt(question: str, context: str, resolved: str | None) -> str:
    scope_line = (
        f"Search scope: {resolved}\n" if resolved else "Search scope: entire corpus\n"
    )
    return (
        "You are a grounded document assistant. Use ONLY the context below.\n"
        "Write the answer as plain prose sentences.\n"
        "Rules:\n"
        "- Plain text only: no JSON, no code fences, no markdown formatting.\n"
        "- Preserve every useful fact directly supported by the retrieved context.\n"
        "- A numeric value described as the point where an alarm activates, an "
        "indicator changes, or a limit is exceeded is a threshold/setpoint — "
        "state that value in the answer.\n"
        "- Do not invent values, part numbers, crew data, or procedures "
        "from adjacent or unrelated manuals.\n"
        f"- If the context does not contain the information needed to answer "
        f"the question, reply with the single token {NOT_IN_CONTEXT_TOKEN} on "
        "the first line and nothing else.\n"
        f"{scope_line}\n"
        f"Context:\n{context}\n\n"
        f"Question: {question}\n\n"
        "Answer:"
    )


def _invoke_generation(
    model: str,
    prompt: str,
    *,
    timeout: float | None = None,
) -> str:
    result = invoke_openai_response(model, prompt, timeout=timeout)
    return result.text.strip()


def _no_coverage_hint(question: str, resolved: str | None, suggest_scopes: bool) -> str:
    hint = DEFAULT_NO_COVERAGE_HINT
    if suggest_scopes:
        alts = suggest_other_scopes(question, exclude=resolved)
        if alts:
            names = ", ".join(
                f"{a['scope']} (score={a['best_score']:.3f})" for a in alts[:5]
            )
            hint = (
                f"{DEFAULT_NO_COVERAGE_HINT} "
                f"Verified hits exist in other scopes: {names}."
            )
    return hint


_TECHNICAL_QUERY_PATTERNS = (
    re.compile(r"\btorque\b"),
    re.compile(r"\bclearance\b"),
    re.compile(r"\bsetpoint\b"),
    re.compile(r"\blimit\b"),
    re.compile(r"\btemperature\b"),
    re.compile(r"\bpressure\b"),
    re.compile(r"\bdimension\b"),
    re.compile(r"\binterval\b"),
    re.compile(r"\bquantity\b"),
    re.compile(r"\bprocedure step\b"),
    re.compile(r"\bsetting\b"),
)

_ALARM_QUERY_PATTERN = re.compile(r"\balarm\b|\bsetpoint\b")

_EXPLICIT_SCOPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bm\s*1\.3\b|\bman\b|\bg50me\b|\bmain engine\b|\bturbocharger\b"), "me-c"),
    (
        re.compile(
            r"\byanmar\b|\b6ey22\b|\by22scr\b|\bscr\b|\bauxiliary engine\b|\baux engine\b"
        ),
        "maker-manuals",
    ),
)

_AMBIGUOUS_CONFIRMATION_PROMPTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^main engine$"), "Which main engine component do you mean?"),
    (re.compile(r"^auxiliary engine$|^aux engine$"), "Which auxiliary engine component or system do you mean?"),
)


def _is_technical_query(question: str) -> bool:
    return any(pattern.search(question) for pattern in _TECHNICAL_QUERY_PATTERNS)


def _clarification_prompt(question: str) -> str:
    if _ALARM_QUERY_PATTERN.search(question):
        return "Which equipment or alarm system do you mean?"
    return "Which equipment/component do you mean?"


def _confirmation_prompt(confirmation_text: str) -> str:
    normalized = normalize_text(confirmation_text).lower()
    for pattern, prompt in _AMBIGUOUS_CONFIRMATION_PROMPTS:
        if pattern.search(normalized):
            return prompt
    return "Which equipment/component do you mean?"


def _inferred_scope_from_text(text: str, verified_context: dict[str, Any] | None = None) -> str | None:
    normalized = normalize_text(text).lower()
    for pattern, scope_name in _EXPLICIT_SCOPE_PATTERNS:
        if pattern.search(normalized):
            return scope_name
    if verified_context and verified_context.get("scope"):
        return str(verified_context["scope"])
    return None


def _question_is_sufficient(question: str, verified_context: dict[str, Any] | None = None) -> bool:
    inferred_scope = _inferred_scope_from_text(question, verified_context)
    if inferred_scope:
        return True
    if not verified_context:
        return False
    equipment = normalize_text(str(verified_context.get("equipment") or "")).lower()
    normalized = normalize_text(question).lower()
    if not equipment:
        return False
    component_terms = ["turbocharger", "bolt", "bolts", "valve", "injector", "pump", "filter"]
    return any(term in equipment and term in normalized for term in component_terms)


def _confirmation_is_sufficient(confirmation_text: str) -> bool:
    normalized = normalize_text(confirmation_text).lower()
    if not normalized:
        return False
    for pattern, _prompt in _AMBIGUOUS_CONFIRMATION_PROMPTS:
        if pattern.search(normalized):
            return False
    return _inferred_scope_from_text(normalized) is not None


def _clarification_result(
    *,
    query: str,
    requested_scope: str | None,
    resolved_scope: str | None,
    prompt: str,
    scope_resolution_s: float | None,
    model: str | None,
    technical_state: str,
) -> AskResult:
    diagnostics = _empty_retrieval_diagnostics()
    diagnostics["gate"] = "clarification_required"
    diagnostics["clarification"] = {
        "technical_state": technical_state,
        "prompt": prompt,
        "fresh_retrieval_required": technical_state == "USER_CONFIRMATION",
        "preconfirmation_reuse_allowed": False,
    }
    return AskResult(
        status="clarification_required",
        query=query,
        requested_scope=requested_scope,
        resolved_scope=resolved_scope,
        answer=prompt,
        sources=[],
        coverage="none",
        timings=_empty_timings(scope_resolution=scope_resolution_s),
        model=model,
        retrieval_evidence=[],
        retrieval_diagnostics=diagnostics,
        gate="clarification_required",
    )


def answer(
    question: str,
    scope: str | None = None,
    k: int | None = None,
    *,
    confirmation_text: str | None = None,
    verified_context: dict[str, Any] | None = None,
    requested_scope: str | None = None,
    suggest_scopes: bool = False,
    scope_resolution_s: float | None = None,
    model: str | None = None,
) -> AskResult:
    """Grounded ask. Returns AskResult (ok | no_coverage | clarification_required | empty_question | error)."""
    t0 = time.perf_counter()
    raw_q = question or ""
    q = normalize_text(raw_q)
    req = requested_scope if requested_scope is not None else scope
    resolved = scope
    chosen_model = resolve_answer_model(model)

    def _timings(
        retrieval: float | None = None,
        generation_primary: float | None = None,
        generation_repair: float | None = None,
        generation_fallback: float | None = None,
        generation: float | None = None,
    ) -> dict[str, float | None]:
        return _empty_timings(
            scope_resolution=scope_resolution_s,
            retrieval=retrieval,
            generation_primary=generation_primary,
            generation_repair=generation_repair,
            generation_fallback=generation_fallback,
            generation=generation,
            total=time.perf_counter() - t0,
        )

    if not q:
        return AskResult(
            status="empty_question",
            query=raw_q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=[],
            coverage="none",
            hint=DEFAULT_NO_COVERAGE_HINT,
            timings=_timings(),
            model=chosen_model,
            retrieval_evidence=[],
            retrieval_diagnostics=_empty_retrieval_diagnostics(),
            gate="empty_question",
        )

    active_query = q
    if confirmation_text is not None:
        confirmation = normalize_text(confirmation_text)
        if not _confirmation_is_sufficient(confirmation):
            return _clarification_result(
                query=q,
                requested_scope=req,
                resolved_scope=resolved,
                prompt=_confirmation_prompt(confirmation),
                scope_resolution_s=scope_resolution_s,
                model=chosen_model,
                technical_state="USER_CONFIRMATION_STILL_AMBIGUOUS",
            )
        resolved = scope or _inferred_scope_from_text(confirmation, verified_context)
        active_query = normalize_text(f"{confirmation}. {q}")
    elif _is_technical_query(q) and not _question_is_sufficient(q, verified_context):
        return _clarification_result(
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            prompt=_clarification_prompt(q),
            scope_resolution_s=scope_resolution_s,
            model=chosen_model,
            technical_state="QUERY_UNDERSPECIFIED",
        )
    elif resolved is None:
        resolved = _inferred_scope_from_text(q, verified_context)

    retrieval_s: float | None = None
    retrieval_diag: dict[str, Any] = {}
    try:
        t_ret = time.perf_counter()
        pairs, retrieval_diag = retrieve_with_scores_and_diagnostics(active_query, scope=resolved, k=k)
        retrieval_s = time.perf_counter() - t_ret
        retrieval_diag = {**_empty_retrieval_diagnostics(), **(retrieval_diag or {})}
        if confirmation_text is not None:
            retrieval_diag["clarification"] = {
                "technical_state": "USER_CONFIRMATION",
                "confirmation_text": normalize_text(confirmation_text),
                "fresh_retrieval_required": True,
                "preconfirmation_reuse_allowed": False,
            }
    except TimeoutError as e:
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            error=str(e),
            timings=_timings(retrieval=retrieval_s),
            model=chosen_model,
            retrieval_evidence=[],
            retrieval_diagnostics=_empty_retrieval_diagnostics(),
            gate="retrieval_timeout",
        )
    except Exception as e:  # noqa: BLE001
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            error=str(e),
            timings=_timings(retrieval=retrieval_s),
            model=chosen_model,
            retrieval_evidence=[],
            retrieval_diagnostics=_empty_retrieval_diagnostics(),
            gate="retrieval_error",
        )

    if not pairs:
        gate = str(retrieval_diag.get("gate") or "no_retrieval")
        if gate == "broad_admissibility_failed":
            resolved_gate = "broad_admissibility_failed"
        elif retrieval_diag.get("raw_count"):
            resolved_gate = "final_confidence_failed"
        else:
            resolved_gate = "no_retrieval"
        retrieval_diag["gate"] = resolved_gate
        return AskResult(
            status="no_coverage",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=[],
            coverage="none",
            hint=_no_coverage_hint(q, resolved, suggest_scopes),
            timings=_timings(retrieval=retrieval_s),
            model=chosen_model,
            best_distance=retrieval_diag.get("best_raw_distance"),
            score_floor=retrieval_diag.get("score_floor"),
            retrieval_evidence=[],
            retrieval_diagnostics=retrieval_diag,
            gate=resolved_gate,
        )

    retrieval_evidence = _sources_from_pairs(pairs)
    pairs, retrieval_diag = _apply_final_confidence_gate(pairs, diagnostics=retrieval_diag)
    if not pairs:
        return AskResult(
            status="no_coverage",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=[],
            coverage="none",
            hint=_no_coverage_hint(q, resolved, suggest_scopes),
            timings=_timings(retrieval=retrieval_s),
            model=chosen_model,
            best_distance=retrieval_diag.get("best_raw_distance"),
            score_floor=retrieval_diag.get("score_floor"),
            retrieval_evidence=retrieval_evidence,
            retrieval_diagnostics=retrieval_diag,
            gate="final_confidence_failed",
        )

    sources = _sources_from_pairs(pairs)
    conservative_success = retrieval_is_conservative_success(pairs)

    if is_source_only_query(q) and conservative_success:
        return AskResult(
            status="ok",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=build_source_only_answer(sources, resolved),
            sources=sources,
            coverage="full",
            missing_information=None,
            timings=_timings(retrieval=retrieval_s),
            model=chosen_model,
            best_distance=retrieval_diag.get("best_raw_distance"),
            score_floor=retrieval_diag.get("score_floor"),
            retrieval_evidence=sources,
            retrieval_diagnostics=retrieval_diag,
            gate="ok",
        )

    context_parts = []
    for doc, _score in pairs:
        meta = doc.metadata or {}
        context_parts.append(
            f"[source={meta.get('source')} page={meta.get('page')} "
            f"collection={meta.get('collection')}]\n{doc.page_content.strip()}"
        )
    context = "\n\n".join(context_parts)

    # Single plain-text generation attempt. The model's only structured duty
    # is the NOT_IN_CONTEXT sentinel; every structured field in the payload
    # comes from retrieval metadata the engine already holds. No repair pass,
    # no salvage: a failed generation is an honest error, not partial_coverage.
    prompt = _build_prompt(q, context, resolved)
    t_gen = time.perf_counter()
    try:
        raw = _invoke_generation(chosen_model, prompt)
    except TimeoutError as e:
        generation_s = time.perf_counter() - t_gen
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=sources,
            error=str(e),
            timings=_timings(
                retrieval=retrieval_s,
                generation_primary=generation_s,
                generation=generation_s,
            ),
            model=chosen_model,
            retrieval_evidence=sources,
            retrieval_diagnostics=retrieval_diag,
            gate="generation_timeout",
        )
    except Exception as e:  # noqa: BLE001
        generation_s = time.perf_counter() - t_gen
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=sources,
            error=str(e),
            timings=_timings(
                retrieval=retrieval_s,
                generation_primary=generation_s,
                generation=generation_s,
            ),
            model=chosen_model,
            retrieval_evidence=sources,
            retrieval_diagnostics=retrieval_diag,
            gate="generation_error",
        )
    generation_s = time.perf_counter() - t_gen

    if model_declared_not_in_context(raw):
        if conservative_success:
            return AskResult(
                status="ok",
                query=q,
                requested_scope=req,
                resolved_scope=resolved,
                answer=(
                    "Relevant source found; answer generation could not extract "
                    "the requested detail. See the listed sources."
                ),
                sources=sources,
                coverage="full",
                missing_information=None,
                timings=_timings(
                    retrieval=retrieval_s,
                    generation_primary=generation_s,
                    generation=generation_s,
                ),
                model=chosen_model,
                retrieval_evidence=sources,
                retrieval_diagnostics=retrieval_diag,
                gate="ok",
            )
        # Chunks were retrieved but do not answer the question. This is the
        # ambiguous case F-18 exists for: `sources` empties per the status
        # rule below, exactly like the zero-retrieval no_coverage above --
        # `retrieval_evidence` + `gate="not_in_context_weak_evidence"` are
        # what let a caller tell the two apart.
        return AskResult(
            status="no_coverage",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=[],
            coverage="none",
            hint=_no_coverage_hint(q, resolved, suggest_scopes),
            timings=_timings(
                retrieval=retrieval_s,
                generation_primary=generation_s,
                generation=generation_s,
            ),
            model=chosen_model,
            retrieval_evidence=sources,
            retrieval_diagnostics=retrieval_diag,
            gate="refusal_or_weak_evidence",
        )

    ans = clean_answer_text(raw)
    if not ans:
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=sources,
            error="empty model response",
            timings=_timings(
                retrieval=retrieval_s,
                generation_primary=generation_s,
                generation=generation_s,
            ),
            model=chosen_model,
            retrieval_evidence=sources,
            retrieval_diagnostics=retrieval_diag,
            gate="empty_model_response",
        )

    return AskResult(
        status="ok",
        query=q,
        requested_scope=req,
        resolved_scope=resolved,
        answer=ans,
        sources=sources,
        coverage="full",
        missing_information=None,
        timings=_timings(
            retrieval=retrieval_s,
            generation_primary=generation_s,
            generation=generation_s,
        ),
        model=chosen_model,
        retrieval_evidence=sources,
        retrieval_diagnostics=retrieval_diag,
        gate="ok",
    )

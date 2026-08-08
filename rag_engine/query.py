"""Shared retrieval + answer synthesis for CLI and Gradio."""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import Counter
from math import isfinite
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings, OllamaLLM

from rag_engine.authority import enrich_metadata
from rag_engine.config import (
    chroma_client_settings,
    default_k,
    embed_model,
    known_scopes,
    llm_fallback_model,
    llm_model,
    llm_num_ctx,
    llm_num_predict,
    persist_dir,
    retrieval_score_max,
)
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

_GENERIC_WEAK_TERMS = frozenset(
    {
        "alarm",
        "temperature",
        "tank",
        "bearing",
        "torque",
        "turbocharger",
    }
)

_KNOWN_CLASS_ABBREVIATIONS = frozenset({"lr", "dnv", "dnv-gl", "ccs", "cr", "rina", "abs", "bv"})

_LEXICAL_WORD_RE = re.compile(r"[a-z0-9][a-z0-9./:+\-]*")
_SECTION_TOKEN_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")
_THREAD_TOKEN_RE = re.compile(r"^(?:m|ø|phi)?\d+(?:x\d+(?:\.\d+)?)?$", re.IGNORECASE)
_MIXED_ALNUM_RE = re.compile(r"^(?=.*[a-zA-Z])(?=.*\d)[a-zA-Z0-9./:+\-]+$")


def _tokenize_lexical(text: str) -> list[str]:
    norm = normalize_text(text or "").lower()
    return [tok for tok in _LEXICAL_WORD_RE.findall(norm) if len(tok) >= 2]


def _extract_heading_lines(text: str, *, max_lines: int = 3, max_chars: int = 120) -> list[str]:
    lines: list[str] = []
    for raw in normalize_text(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if len(line) <= max_chars:
            lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def _is_exact_technical_token(token: str) -> bool:
    tok = (token or "").strip().lower()
    if not tok:
        return False
    return bool(
        tok in _KNOWN_CLASS_ABBREVIATIONS
        or _SECTION_TOKEN_RE.match(tok)
        or _THREAD_TOKEN_RE.match(tok)
        or _MIXED_ALNUM_RE.match(tok)
    )


def _query_signals(question: str) -> dict[str, Any]:
    normalized = normalize_text(question or "")
    tokens = _tokenize_lexical(normalized)
    technical_tokens = [tok for tok in tokens if _is_exact_technical_token(tok)]
    generic_tokens = [tok for tok in tokens if tok in _GENERIC_WEAK_TERMS]
    non_generic_tokens = [tok for tok in tokens if tok not in _GENERIC_WEAK_TERMS]
    phrases: list[str] = []
    phrase_source = [tok for tok in tokens if tok not in _GENERIC_WEAK_TERMS]
    for size in range(2, min(5, len(phrase_source)) + 1):
        for i in range(0, len(phrase_source) - size + 1):
            phrase = " ".join(phrase_source[i : i + size]).strip()
            if phrase:
                phrases.append(phrase)
    return {
        "normalized": normalized,
        "tokens": tokens,
        "technical_tokens": list(dict.fromkeys(technical_tokens)),
        "generic_tokens": list(dict.fromkeys(generic_tokens)),
        "non_generic_tokens": list(dict.fromkeys(non_generic_tokens)),
        "phrases": list(dict.fromkeys(phrases)),
    }


def _candidate_identity(doc: Any) -> tuple[str, Any, str, str]:
    meta = enrich_metadata(getattr(doc, "metadata", {}) or {})
    doc.metadata = meta
    chunk_id = str(meta.get("chunk_id") or "").strip()
    if chunk_id:
        return ("chunk", chunk_id, "", "")
    digest = hashlib.sha256(normalize_text(getattr(doc, "page_content", "") or "").encode("utf-8")).hexdigest()
    return (
        str(meta.get("source", "")),
        meta.get("page", "?"),
        str(meta.get("collection", "other")),
        digest,
    )


@lru_cache(maxsize=1)
def _lexical_cache() -> dict[str, Any]:
    db = _get_db()

    def _call():
        return db.get(include=["documents", "metadatas"])

    raw = _run_with_timeout(_call)
    ids = list(raw.get("ids") or [])
    documents = list(raw.get("documents") or [])
    metadatas = list(raw.get("metadatas") or [])
    records: dict[str, dict[str, Any]] = {}
    postings: dict[str, set[str]] = {}
    for idx, chunk_id in enumerate(ids):
        meta = enrich_metadata(metadatas[idx] or {})
        meta.setdefault("chunk_id", chunk_id)
        content = normalize_text(documents[idx] or "")
        tokens = set(_tokenize_lexical(content))
        heading_lines = _extract_heading_lines(content)
        heading_text = "\n".join(heading_lines).lower()
        heading_tokens = set(_tokenize_lexical(" ".join(heading_lines)))
        record = {
            "id": chunk_id,
            "metadata": meta,
            "page_content": content,
            "tokens": tokens,
            "heading_text": heading_text,
            "heading_tokens": heading_tokens,
        }
        records[chunk_id] = record
        for token in tokens | heading_tokens:
            postings.setdefault(token, set()).add(chunk_id)
    return {"records": records, "postings": postings}


def _lexical_retrieve(
    question: str,
    scope: str | None = None,
    k: int | None = None,
) -> list[tuple[Any, dict[str, Any]]]:
    signals = _query_signals(question)
    tokens = list(dict.fromkeys(signals["technical_tokens"] + signals["non_generic_tokens"]))
    if not tokens:
        return []
    cache = _lexical_cache()
    records = cache["records"]
    postings = cache["postings"]
    candidate_ids: set[str] = set()
    for token in tokens:
        candidate_ids.update(postings.get(token, set()))
    if not candidate_ids:
        return []

    scored: list[tuple[Any, dict[str, Any]]] = []
    for chunk_id in candidate_ids:
        record = records.get(chunk_id)
        if not record:
            continue
        doc = type("LexicalDoc", (), {})()
        doc.metadata = dict(record["metadata"])
        doc.page_content = record["page_content"]
        doc.metadata["chunk_id"] = chunk_id
        if scope:
            meta = enrich_metadata(doc.metadata)
            doc.metadata = meta
            if not scope_allows_candidate(scope, meta):
                continue
        exact_hits = [tok for tok in signals["technical_tokens"] if tok in record["tokens"] or tok in record["heading_tokens"]]
        phrase_hits = [phrase for phrase in signals["phrases"] if phrase and phrase in record["page_content"].lower()]
        heading_match = any(tok in record["heading_tokens"] for tok in signals["technical_tokens"])
        token_hits = [tok for tok in signals["non_generic_tokens"] if tok in record["tokens"]]
        if not exact_hits and not phrase_hits and not heading_match and not token_hits:
            continue
        lexical_score = (
            len(exact_hits) * 100
            + len(phrase_hits) * 40
            + (25 if heading_match else 0)
            + len(token_hits) * 5
        )
        if lexical_score <= 0:
            continue
        scored.append(
            (
                doc,
                {
                    "lexical_score": lexical_score,
                    "lexical_exact_hits": sorted(set(exact_hits)),
                    "lexical_phrase_hits": sorted(set(phrase_hits)),
                    "heading_match": heading_match,
                },
            )
        )
    scored.sort(
        key=lambda item: (
            -int(bool(item[1]["lexical_exact_hits"])),
            -int(item[1]["heading_match"]),
            -int(item[1]["lexical_score"]),
            str(enrich_metadata(item[0].metadata).get("source", "")),
            enrich_metadata(item[0].metadata).get("page", "?"),
        )
    )
    limit = max((k or default_k()) * 20, 50)
    return scored[:limit]


def _merge_hybrid_candidates(
    vector_pairs: list[tuple[Any, float]],
    lexical_pairs: list[tuple[Any, dict[str, Any]]],
) -> tuple[list[tuple[Any, float]], dict[str, Any]]:
    merged: dict[tuple[str, Any, str, str], dict[str, Any]] = {}
    best_vector_distance = min((float(distance) for _doc, distance in vector_pairs), default=retrieval_score_max())

    for doc, distance in vector_pairs:
        meta = enrich_metadata(doc.metadata)
        doc.metadata = meta
        key = _candidate_identity(doc)
        merged[key] = {
            "doc": doc,
            "vector_distance": float(distance),
            "distance": float(distance),
            "candidate_origin": "vector",
            "lexical_score": None,
            "lexical_exact_hits": [],
            "lexical_phrase_hits": [],
            "heading_match": False,
        }

    lexical_rank = 0
    for doc, info in lexical_pairs:
        meta = enrich_metadata(doc.metadata)
        doc.metadata = meta
        key = _candidate_identity(doc)
        lexical_rank += 1
        if key in merged:
            row = merged[key]
            row["candidate_origin"] = "both"
            row["lexical_score"] = int(info.get("lexical_score") or 0)
            row["lexical_exact_hits"] = list(info.get("lexical_exact_hits") or [])
            row["lexical_phrase_hits"] = list(info.get("lexical_phrase_hits") or [])
            row["heading_match"] = bool(info.get("heading_match", False))
            row["doc"].metadata.update(
                {
                    "candidate_origin": "both",
                    "vector_distance": row["vector_distance"],
                    "lexical_score": row["lexical_score"],
                    "lexical_exact_hits": row["lexical_exact_hits"],
                    "lexical_phrase_hits": row["lexical_phrase_hits"],
                    "heading_match": row["heading_match"],
                }
            )
            continue
        pseudo_distance = best_vector_distance + 0.0001 * lexical_rank
        doc.metadata.update(
            {
                "candidate_origin": "lexical",
                "vector_distance": None,
                "lexical_score": int(info.get("lexical_score") or 0),
                "lexical_exact_hits": list(info.get("lexical_exact_hits") or []),
                "lexical_phrase_hits": list(info.get("lexical_phrase_hits") or []),
                "heading_match": bool(info.get("heading_match", False)),
            }
        )
        merged[key] = {
            "doc": doc,
            "vector_distance": None,
            "distance": pseudo_distance,
            "candidate_origin": "lexical",
            "lexical_score": int(info.get("lexical_score") or 0),
            "lexical_exact_hits": list(info.get("lexical_exact_hits") or []),
            "lexical_phrase_hits": list(info.get("lexical_phrase_hits") or []),
            "heading_match": bool(info.get("heading_match", False)),
        }

    def _hybrid_sort_key(row: dict[str, Any]) -> tuple[int, int, int, float, str, Any]:
        meta = enrich_metadata(row["doc"].metadata)
        row["doc"].metadata = meta
        has_exact = bool(row["lexical_exact_hits"])
        origin = str(row["candidate_origin"])
        origin_priority = {
            "both": 0,
            "lexical": 1 if has_exact else 3,
            "vector": 2,
        }.get(origin, 4)
        exact_flag = 0 if has_exact else 1
        lexical_score = -int(row["lexical_score"] or 0)
        distance = float(row["distance"])
        return (
            origin_priority,
            exact_flag,
            lexical_score,
            distance,
            str(meta.get("source", "")),
            meta.get("page", "?"),
        )

    merged_rows = sorted(merged.values(), key=_hybrid_sort_key)
    output: list[tuple[Any, float]] = []
    origin_counts = Counter()
    for row in merged_rows:
        doc = row["doc"]
        doc.metadata.update(
            {
                "candidate_origin": row["candidate_origin"],
                "vector_distance": row["vector_distance"],
                "lexical_score": row["lexical_score"],
                "lexical_exact_hits": row["lexical_exact_hits"],
                "lexical_phrase_hits": row["lexical_phrase_hits"],
                "heading_match": row["heading_match"],
            }
        )
        origin_counts[str(row["candidate_origin"])] += 1
        output.append((doc, float(row["distance"])))
    diagnostics = {
        "vector_raw_count": len(vector_pairs),
        "lexical_raw_count": len(lexical_pairs),
        "merged_raw_count": len(output),
        "candidate_origins": dict(origin_counts),
    }
    return output, diagnostics


def ollama_timeout_s() -> float:
    # Default 300s: retrieval (embedding) calls under load exceed 120s and
    # must still surface as exit 1 rather than hang forever for Hermes.
    # NOT used for generation — see ollama_gen_timeout_s().
    return float(os.environ.get("RAG_OLLAMA_TIMEOUT", "300"))


def ollama_gen_timeout_s() -> float:
    """Hard per-call cap for every generation attempt (primary/repair/fallback).

    Deliberately tight and independent of RAG_OLLAMA_TIMEOUT: a malformed or
    stalled generation must fail fast enough that the repair step and the
    deterministic salvage path still return well inside a human-tolerable
    budget, instead of the 300s retrieval timeout being hit two or three
    times in a chain.
    """
    return float(os.environ.get("RAG_OLLAMA_GEN_TIMEOUT", "8"))


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
    status: str  # ok | no_coverage | empty_question | error
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


@lru_cache(maxsize=8)
def _get_llm(
    model: str,
    num_ctx: int | None,
    num_predict: int | None,
) -> OllamaLLM:
    kwargs: dict[str, Any] = {"model": model, "temperature": 0}
    if num_ctx is not None:
        kwargs["num_ctx"] = num_ctx
    if num_predict is not None:
        kwargs["num_predict"] = num_predict
    return OllamaLLM(**kwargs)


def clear_caches() -> None:
    _get_db.cache_clear()
    _get_llm.cache_clear()
    _lexical_cache.cache_clear()


def resolve_answer_model(
    model: str | None = None,
    *,
    use_fallback: bool = False,
) -> str:
    """Explicit model > fallback model > configured default."""
    if model:
        return model
    if use_fallback:
        return llm_fallback_model()
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
        "vector_raw_count": 0,
        "lexical_raw_count": 0,
        "merged_raw_count": 0,
        "post_admissibility_count": 0,
        "post_scope_count": 0,
        "post_rerank_count": 0,
        "post_dedupe_count": 0,
        "final_retained_count": 0,
        "final_confidence_passed": False,
        "candidate_origins": {},
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
    hybrid_diagnostics: dict[str, Any] | None = None,
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
        "vector_raw_count": int((hybrid_diagnostics or {}).get("vector_raw_count", len(pairs))),
        "lexical_raw_count": int((hybrid_diagnostics or {}).get("lexical_raw_count", 0)),
        "merged_raw_count": int((hybrid_diagnostics or {}).get("merged_raw_count", len(pairs))),
        "post_admissibility_count": len(admissible),
        "post_scope_count": len(scope_filtered),
        "post_rerank_count": len(ranked),
        "post_dedupe_count": len(deduped),
        "final_retained_count": 0,
        "final_confidence_passed": False,
        "candidate_origins": dict((hybrid_diagnostics or {}).get("candidate_origins", {})),
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

    vector_results = _run_with_timeout(_call)
    lexical_results = _lexical_retrieve(q, scope=scope, k=k)
    raw_results, hybrid_diag = _merge_hybrid_candidates(vector_results, lexical_results)
    results, _diagnostics = _apply_retrieval_controls(raw_results, scope=scope, k=k, hybrid_diagnostics=hybrid_diag)
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

    vector_results = _run_with_timeout(_call)
    lexical_results = _lexical_retrieve(q, scope=scope, k=k)
    raw_results, hybrid_diag = _merge_hybrid_candidates(vector_results, lexical_results)
    return _apply_retrieval_controls(raw_results, scope=scope, k=k, hybrid_diagnostics=hybrid_diag)


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
                **(
                    {"candidate_origin": str(meta.get("candidate_origin"))}
                    if meta.get("candidate_origin")
                    else {}
                ),
                **(
                    {"vector_distance": float(meta.get("vector_distance"))}
                    if meta.get("vector_distance") is not None
                    else {}
                ),
                **(
                    {"lexical_score": int(meta.get("lexical_score"))}
                    if meta.get("lexical_score") is not None
                    else {}
                ),
                **(
                    {"lexical_exact_hits": list(meta.get("lexical_exact_hits") or [])}
                    if meta.get("lexical_exact_hits")
                    else {}
                ),
                **(
                    {"lexical_phrase_hits": list(meta.get("lexical_phrase_hits") or [])}
                    if meta.get("lexical_phrase_hits")
                    else {}
                ),
                **({"heading_match": True} if meta.get("heading_match") else {}),
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


def _invoke_llm(
    model: str,
    prompt: str,
    num_ctx: int | None,
    num_predict: int | None,
    *,
    timeout: float | None = None,
) -> str:
    llm = _get_llm(model, num_ctx, num_predict)
    t = ollama_gen_timeout_s() if timeout is None else timeout
    return str(_run_with_timeout(lambda: llm.invoke(prompt), timeout=t)).strip()


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


def answer(
    question: str,
    scope: str | None = None,
    k: int | None = None,
    *,
    requested_scope: str | None = None,
    suggest_scopes: bool = False,
    scope_resolution_s: float | None = None,
    model: str | None = None,
    use_fallback: bool = False,
    num_ctx: int | None = None,
    num_predict: int | None = None,
) -> AskResult:
    """Grounded ask. Returns AskResult (ok | no_coverage | empty_question | error)."""
    t0 = time.perf_counter()
    raw_q = question or ""
    q = normalize_text(raw_q)
    req = requested_scope if requested_scope is not None else scope
    resolved = scope
    chosen_model = resolve_answer_model(model, use_fallback=use_fallback)
    ctx_size = llm_num_ctx() if num_ctx is None else num_ctx
    predict = llm_num_predict() if num_predict is None else num_predict

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

    retrieval_s: float | None = None
    retrieval_diag: dict[str, Any] = {}
    try:
        t_ret = time.perf_counter()
        pairs, retrieval_diag = retrieve_with_scores_and_diagnostics(q, scope=resolved, k=k)
        retrieval_s = time.perf_counter() - t_ret
        retrieval_diag = {**_empty_retrieval_diagnostics(), **(retrieval_diag or {})}
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
        raw = _invoke_llm(chosen_model, prompt, ctx_size, predict)
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

"""Shared retrieval for CLI, Hermes plugin, and Gradio.

ORCH_104: retrieval-only backend. Final natural-language answer generation
is Hermes' responsibility. This module returns evidence packages
(status / scopes / sources / chunks / clarification / diagnostics).
"""

from __future__ import annotations

import os
import re
import time
from math import isfinite
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

from rag_engine.authority import (
    authority_family_counts_for_confidence,
    enrich_metadata,
)
from rag_engine.config import (
    chroma_client_settings,
    default_k,
    embed_model,
    known_scopes,
    llm_model,
    persist_dir,
    retrieval_score_max,
)
from rag_engine.index_compatibility import (
    FingerprintError,
    enforce_retrieval_compatibility,
)
from rag_engine.index_compatibility.chroma_inspect import count_vectors_readonly
from rag_engine.pdf_links import citation_page_fields, viewer_page
from rag_engine.scope_rules import scope_allows_candidate
from rag_engine.text import normalize_text

# Exit semantics for CLI / Hermes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_COVERAGE = 2

# v4 (ORCH_104): ask path is retrieval-only. Successful hits return an evidence
# package (sources / retrieved_chunks / retrieval_context / …) with answer=null;
# Hermes owns final NL generation. Clarification prompts still use `answer`.
# v3: F-18 retrieval_evidence + gate (additive). sources[].score → distance.
# v2: plain-text generation, no partial_coverage.
SCHEMA_VERSION = 4

DEFAULT_NO_COVERAGE_HINT = (
    "No supporting chunks were found in this scope. "
    "The requested document may belong to another scope; "
    "use scope diagnostics rather than answering from model memory."
)

_STATUSES_WITH_SOURCES = frozenset({"ok"})

# Hermes owns generation; engine never claims a chat/completion model.
GENERATION_OWNER = "hermes"


def ollama_timeout_s() -> float:
    # Default 300s: retrieval (embedding) calls under load exceed 120s and
    # must still surface as exit 1 rather than hang forever for Hermes.
    # Embeddings only — no chat/completion provider in the ask path.
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
    # Clarification prompts and rare deterministic source-only messages only.
    # Successful retrieval leaves answer=None — Hermes generates the NL answer.
    answer: str | None = None
    sources: list[dict] = field(default_factory=list)
    retrieved_chunks: list[dict] = field(default_factory=list)
    retrieval_context: str | None = None
    hint: str | None = None
    error: str | None = None
    coverage: str | None = None  # retrieval package state only: full | none
    # ``full`` means an admissible evidence package was assembled for Hermes.
    # It does NOT mean answer completeness, factual completeness, or evidence
    # sufficiency — Hermes must still judge whether chunks support the claim.
    missing_information: str | None = None
    timings: dict[str, float | None] = field(default_factory=_empty_timings)
    # Always None on the retrieval-only ask path (Hermes selects the model).
    model: str | None = None
    best_distance: float | None = None
    score_floor: float | None = None
    # F-18: what retrieval actually found, independent of `sources` and its
    # status-gated emptying below. Always reflects retrieve_with_scores()'s
    # real output -- [] only when retrieval genuinely found nothing (or
    # never ran, e.g. empty_question / a retrieval-stage error), never
    # emptied just because the final status ended up non-"ok".
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
        sources = self.sources if keep_sources else []
        chunks = self.retrieved_chunks if keep_sources else []
        clarification = (self.retrieval_diagnostics or {}).get("clarification")
        page_numbers: list[Any] = []
        document_names: list[str] = []
        seen_docs: set[str] = set()
        for src in sources:
            if not isinstance(src, dict):
                continue
            page = src.get("page")
            if page is not None and page not in page_numbers:
                page_numbers.append(page)
            path = str(src.get("path") or "")
            name = Path(path).name if path else ""
            if name and name not in seen_docs:
                seen_docs.add(name)
                document_names.append(name)
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "query": self.query,
            "requested_scope": self.requested_scope,
            "resolved_scope": self.resolved_scope,
            "coverage": self.coverage,
            "answer": self.answer,
            "missing_information": self.missing_information,
            "sources": sources,
            "retrieved_chunks": chunks,
            "retrieval_context": self.retrieval_context if keep_sources else None,
            "page_numbers": page_numbers,
            "document_names": document_names,
            "clarification_state": clarification if isinstance(clarification, dict) else None,
            "retrieval_metadata": {
                "best_distance": self.best_distance,
                "score_floor": self.score_floor,
                "gate": self.gate,
                "diagnostics": self.retrieval_diagnostics or {},
            },
            "generation_owner": GENERATION_OWNER,
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


def _fingerprint_retrieval_gate() -> dict[str, Any]:
    """Enforce Phase 6A retrieval policy. Read-only; never writes fingerprint state.

    Returns a diagnostics fragment. Raises FingerprintError when retrieval is blocked.
    """
    persist = persist_dir()
    vector_count = count_vectors_readonly(persist)
    result = enforce_retrieval_compatibility(persist, vector_count=vector_count)
    return {
        "index_fingerprint_state": result.state,
        "index_fingerprint_degraded": bool(result.retrieval_degraded),
        "index_fingerprint_reason": result.reason,
        "index_fingerprint_runtime": result.runtime_index_fingerprint,
        "index_fingerprint_stored": result.stored_index_fingerprint,
        "index_fingerprint_vector_count": result.vector_count,
    }


def resolve_answer_model(model: str | None = None) -> str:
    """Legacy helper: configured default chat model name (unused by ask path)."""
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


# Explicit manual / filename mentions in the user question (e.g. "In M 1.3, …").
# Used only as a post-retrieval ranking signal — never invents coverage.
_EXPLICIT_MANUAL_ID_RE = re.compile(r"\bM\s+\d+(?:\.\d+)+\b", re.IGNORECASE)
_PDF_EXT_RE = re.compile(r"\.pdf\b", re.IGNORECASE)
# Leading prose commonly glued onto a greedy ".pdf" walk-back; stripped after extract.
_PDF_LEADING_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "see",
        "check",
        "refer",
        "to",
        "from",
        "in",
        "on",
        "of",
        "according",
        "please",
        "open",
        "read",
        "use",
        "using",
        "with",
        "for",
        "and",
        "or",
        "as",
        "per",
        "by",
        "at",
        "is",
        "are",
        "this",
        "that",
        "document",
        "file",
        "named",
        "called",
        "into",
        "about",
        "review",
        "chapter",
        "compare",
        "versus",
        "vs",
        "between",
        "against",
        "including",
        "include",
        "using",
    }
)
_PDF_BODY_MAX = 80


def _normalize_mention_key(value: str) -> str:
    return re.sub(r"[\s_]+", "", value or "").lower()


def _extract_pdf_filename_at(question: str, ext_start: int, ext_end: int) -> str | None:
    """Walk left from a `.pdf` hit to recover a conservative filename token.

    Walk includes alnum / ``._-`` / spaces. When a completed left-hand word is a
    prose stopword, stop *before* including it so ``see Manual.pdf`` yields
    ``Manual.pdf`` while ``Main Engine Manual.pdf`` keeps its spaces.
    """
    i = ext_start - 1
    if i >= 0 and question[i] in "'\"":
        i -= 1
    body_chars: list[str] = []
    current_word: list[str] = []

    def _flush_word() -> bool:
        """Return False if the completed word is a stopword (caller should halt)."""
        if not current_word:
            return True
        # current_word holds characters collected right-to-left.
        word = "".join(reversed(current_word))
        if word.lower().strip(".,;:!?") in _PDF_LEADING_STOPWORDS:
            current_word.clear()
            return False
        body_chars.extend(current_word)
        current_word.clear()
        return True

    while i >= 0 and len(body_chars) + len(current_word) < _PDF_BODY_MAX:
        ch = question[i]
        if ch.isalnum() or ch in "._-":
            current_word.append(ch)
            i -= 1
            continue
        if ch == " ":
            if not _flush_word():
                break
            if body_chars:
                body_chars.append(" ")
            i -= 1
            continue
        if ch in "'\"":
            if not _flush_word():
                break
            i -= 1
            continue
        # Punctuation / other boundary.
        _flush_word()
        break
    else:
        # Exhausted left edge or max length without a hard break.
        _flush_word()

    body = "".join(reversed(body_chars)).strip()
    body = re.sub(r"\s+", " ", body).strip(".,;:!? ")
    if not body or not any(c.isalnum() for c in body):
        return None
    return f"{body}{question[ext_start:ext_end]}"


def extract_explicit_document_mentions(question: str) -> tuple[str, ...]:
    """Return distinct document mentions explicitly present in the question."""
    if not question:
        return ()
    found: list[str] = []
    seen: set[str] = set()
    for match in _EXPLICIT_MANUAL_ID_RE.finditer(question):
        token = re.sub(r"\s+", " ", match.group(0)).strip()
        key = token.lower()
        if key not in seen:
            seen.add(key)
            found.append(token)
    for match in _PDF_EXT_RE.finditer(question):
        token = _extract_pdf_filename_at(question, match.start(), match.end())
        if not token:
            continue
        key = token.lower()
        if key not in seen:
            seen.add(key)
            found.append(token)
    return tuple(found)


def source_matches_explicit_mention(source: str, mention: str) -> bool:
    """True when a retrieved source basename exactly matches an explicit mention.

    Uses normalized identity on basename/stem only — no bidirectional substring
    matching (avoids M 1.3 → M.pdf / XM 1.3.pdf / M 1.30.pdf false boosts).
    """
    if not source or not mention:
        return False
    src = source.replace("\\", "/")
    base = src.rsplit("/", 1)[-1]
    mention_key = _normalize_mention_key(mention)
    if not mention_key:
        return False
    base_key = _normalize_mention_key(base)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem_key = _normalize_mention_key(stem)

    if mention_key.endswith(".pdf"):
        mention_stem = mention_key[: -len(".pdf")]
        return base_key == mention_key or stem_key == mention_stem

    # Non-PDF mention (e.g. "M 1.3"): exact stem identity, or basename == mention + ".pdf".
    if stem_key == mention_key:
        return True
    if base_key == mention_key or base_key == f"{mention_key}.pdf":
        return True
    return False


def _candidate_sort_key(
    item: tuple[Any, float],
    *,
    family_support: dict[str, int] | None = None,
    source_support: dict[str, int] | None = None,
    explicit_mentions: tuple[str, ...] = (),
) -> tuple[int, int, int, int, int, int, int, float, str, Any]:
    doc, distance = item
    meta = enrich_metadata(doc.metadata)
    doc.metadata = meta
    band = int(float(distance) / authority_preference_distance_window())
    family = str(meta.get("authority_family", ""))
    support = 0 if family_support is None else int(family_support.get(family, 0))
    source = str(meta.get("source", ""))
    source_coherence = 0 if source_support is None else int(source_support.get(source, 0))
    # 0 = explicit mention match (preferred); 1 = no match.
    explicit_miss = 0 if (
        explicit_mentions
        and any(source_matches_explicit_mention(source, m) for m in explicit_mentions)
    ) else 1
    return (
        explicit_miss,
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


# Generic operational / UI vocabulary. Matching only these tokens must not
# count as topical agreement (avoids coherent stop/auto/alarm false positives).
_GENERIC_OPERATIONAL_TOKENS = frozenset(
    {
        "stop",
        "start",
        "starts",
        "stopped",
        "stopping",
        "automatic",
        "automatically",
        "manual",
        "manually",
        "auto",
        "operation",
        "operating",
        "operate",
        "system",
        "systems",
        "mode",
        "modes",
        "alarm",
        "alarms",
        "emergency",
        "overflow",
        "discharge",
        "pump",
        "pumps",
        "valve",
        "valves",
        "switch",
        "switches",
        "button",
        "buttons",
        "procedure",
        "procedures",
        "control",
        "controls",
        "controller",
        "running",
        "standby",
        "feed",
        "level",
        "tank",
        "flush",
        "cycle",
        "interval",
        "activated",
        "message",
        "display",
        "screen",
        "hmi",
        "note",
        "caution",
        "chapter",
        "page",
        "plant",
        "what",
        "which",
        "when",
        "where",
        "how",
        "does",
        "from",
        "with",
        "that",
        "this",
        "into",
        "only",
        "also",
        "than",
        "then",
        "over",
        "under",
        "after",
        "before",
        "during",
        "about",
        "describe",
        "according",
    }
)

# Ambiguous short tokens often produced by splitting model codes (e.g. COM).
_AMBIGUOUS_SHORT_TOKENS = frozenset(
    {
        "com",
        "man",
        "eng",
        "set",
        "max",
        "min",
        "oil",
        "air",
        "gas",
        "pdf",
        "doc",
        "rev",
        "the",
        "and",
        "for",
        "any",
        "all",
        "not",
        "use",
        "via",
    }
)


# Unicode dashes that must fold to ASCII "-" before topical anchor matching.
# NFKC alone does not normalize these (en/em dash, minus sign, etc.).
_UNICODE_DASH_TO_ASCII = str.maketrans(
    {
        "\u2010": "-",  # HYPHEN
        "\u2011": "-",  # NON-BREAKING HYPHEN
        "\u2012": "-",  # FIGURE DASH
        "\u2013": "-",  # EN DASH
        "\u2014": "-",  # EM DASH
        "\u2212": "-",  # MINUS SIGN
    }
)


def _fold_unicode_dashes(text: str) -> str:
    """Map common Unicode dash/minus glyphs to ASCII hyphen-minus."""
    if not text:
        return ""
    return text.translate(_UNICODE_DASH_TO_ASCII)


def _normalize_for_topical(text: str) -> str:
    """Normalize text for topical anchor extraction and blob matching."""
    return _fold_unicode_dashes(normalize_text(text or "")).lower()


def _query_anchor_tokens(question: str) -> set[str]:
    """Distinctive query tokens used for topical agreement checks.

    Prefers hyphenated compounds and non-generic content tokens so coherent
    families that only share stop/auto/alarm vocabulary cannot pass.
    """
    q = _normalize_for_topical(question or "")
    if not q:
        return set()
    anchors: set[str] = set()
    for match in re.finditer(r"[a-z0-9]+(?:-[a-z0-9]+)+", q):
        compound = match.group(0)
        anchors.add(compound)
        for part in compound.split("-"):
            if len(part) >= 4 and part not in _GENERIC_OPERATIONAL_TOKENS:
                anchors.add(part)
            elif (
                len(part) == 3
                and part.isalpha()
                and part not in _GENERIC_OPERATIONAL_TOKENS
                and part not in _AMBIGUOUS_SHORT_TOKENS
            ):
                anchors.add(part)
    for match in re.finditer(r"[a-z0-9]{4,}", q):
        token = match.group(0)
        if token in _GENERIC_OPERATIONAL_TOKENS:
            continue
        anchors.add(token)
    return anchors


def _pair_topical_blob(doc: Any) -> str:
    meta = getattr(doc, "metadata", None) or {}
    source = str(meta.get("source", "") or "")
    text = str(getattr(doc, "page_content", "") or "")
    return _normalize_for_topical(f"{source}\n{text}")


def _token_in_blob(token: str, blob: str) -> bool:
    if not token or not blob:
        return False
    if "-" in token or any(ch.isdigit() for ch in token):
        return token in blob
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", blob) is not None


def _query_topical_agreement(
    question: str | None,
    pairs: list[tuple[Any, float]],
) -> bool:
    """True when the given evidence subset shares distinctive query anchors.

    Callers must pass the support subset for the active pass path (coherent
    source/family/consensus, or top-authority support) — not the full retained
    set — so an unrelated hitchhiker hit cannot lend topicality.
    """
    if not question or not pairs:
        return False
    anchors = _query_anchor_tokens(question)
    if not anchors:
        return False
    for doc, _distance in pairs:
        blob = _pair_topical_blob(doc)
        if any(_token_in_blob(token, blob) for token in anchors):
            return True
    return False


def _pair_source(doc: Any) -> str:
    return str((getattr(doc, "metadata", None) or {}).get("source", "") or "")


def _pair_family(doc: Any) -> str:
    return str((getattr(doc, "metadata", None) or {}).get("authority_family", "") or "")


def _family_coherence_eligible(
    top_source: str,
    top_family: str,
    top_family_support: int,
) -> bool:
    """Family-only coherence requires allowlisted semantic family + support."""
    return top_family_support >= 2 and authority_family_counts_for_confidence(
        top_source, top_family
    )


def _coherent_support_pairs(
    pairs: list[tuple[Any, float]],
    *,
    top_source: str,
    top_family: str,
    top_source_support: int,
    top_family_support: int,
) -> list[tuple[Any, float]]:
    """Evidence subset that established coherent_support (source > family > consensus)."""
    if top_source_support >= 2 and top_source:
        return [(doc, dist) for doc, dist in pairs if _pair_source(doc) == top_source]
    if _family_coherence_eligible(top_source, top_family, top_family_support):
        return [(doc, dist) for doc, dist in pairs if _pair_family(doc) == top_family]
    top_n = min(3, len(pairs))
    if len(pairs) >= 2 and single_source_consensus(pairs, top_n=top_n):
        return list(pairs[:top_n])
    return []


def _top_authority_support_pairs(
    pairs: list[tuple[Any, float]],
    *,
    top_source: str,
) -> list[tuple[Any, float]]:
    """Top-authority evidence for the CAR <= 2 fallback (same source as top, else top hit)."""
    if not pairs:
        return []
    if top_source:
        same_source = [(doc, dist) for doc, dist in pairs if _pair_source(doc) == top_source]
        if same_source:
            return same_source
    return [pairs[0]]


def _apply_final_confidence_gate(
    pairs: list[tuple[Any, float]],
    *,
    diagnostics: dict[str, Any],
    question: str | None = None,
) -> tuple[list[tuple[Any, float]], dict[str, Any]]:
    """Accept retrieval only when authority is paired with distance or topic.

    Pass rule:
      top_is_authoritative
      AND (
          strong_distance
          OR (coherent_support AND topical_agreement_with_coherent_support)
          OR (top_canonical_authority_rank <= 2
              AND topical_agreement_with_top_authority_support)
      )

    coherent_support is earned by same-source support, allowlisted semantic
    family support (maker/equipment and Training/<course> only), or
    single-source consensus. Organizational buckets are not allowlisted.
    Topical agreement is bound to the evidence subset for that pass path —
    not the global retained set — so hitchhiker hits cannot invent coverage.
    Strong-distance remains sufficient without topical anchors.
    """
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
    family_coherence_eligible = _family_coherence_eligible(
        top_source, top_family, top_family_support
    )
    coherent_support = (
        top_source_support >= 2
        or family_coherence_eligible
        or (len(pairs) >= 2 and single_source_consensus(pairs, top_n=min(3, len(pairs))))
    )
    coherent_pairs = (
        _coherent_support_pairs(
            pairs,
            top_source=top_source,
            top_family=top_family,
            top_source_support=top_source_support,
            top_family_support=top_family_support,
        )
        if coherent_support
        else []
    )
    top_authority_pairs = _top_authority_support_pairs(pairs, top_source=top_source)
    topical_agreement_with_coherent_support = (
        _query_topical_agreement(question, coherent_pairs) if coherent_support else False
    )
    topical_agreement_with_top_authority_support = _query_topical_agreement(
        question, top_authority_pairs
    )
    # Bound topical signal used by non-strong pass paths (diagnostics alias).
    topical_agreement = bool(
        (coherent_support and topical_agreement_with_coherent_support)
        or (
            top_canonical_authority_rank <= 2
            and topical_agreement_with_top_authority_support
        )
    )
    support_signal = bool(
        strong_distance
        or (coherent_support and topical_agreement_with_coherent_support)
        or (
            top_canonical_authority_rank <= 2
            and topical_agreement_with_top_authority_support
        )
    )
    final_pass = bool(top_is_authoritative and support_signal)

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
            "family_coherence_eligible": family_coherence_eligible,
            "strong_distance": strong_distance,
            "coherent_support": coherent_support,
            "topical_agreement": topical_agreement,
            "topical_agreement_with_coherent_support": topical_agreement_with_coherent_support,
            "topical_agreement_with_top_authority_support": (
                topical_agreement_with_top_authority_support
            ),
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
    question: str | None = None,
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
    explicit_mentions = extract_explicit_document_mentions(question or "")
    ranked = sorted(
        scope_filtered,
        key=lambda item: _candidate_sort_key(
            item,
            family_support=family_support,
            source_support=source_support,
            explicit_mentions=explicit_mentions,
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
        "explicit_document_mentions": list(explicit_mentions),
    }
    return deduped[:k], diagnostics


def retrieve_with_scores(
    question: str,
    scope: str | None = None,
    k: int | None = None,
) -> list[tuple[Any, float]]:
    fp_diag = _fingerprint_retrieval_gate()
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
    results, _diagnostics = _apply_retrieval_controls(
        raw_results, scope=scope, k=k, question=q
    )
    _ = fp_diag  # enforced above; detailed diagnostics via *_and_diagnostics
    return results


def retrieve_with_scores_and_diagnostics(
    question: str,
    scope: str | None = None,
    k: int | None = None,
) -> tuple[list[tuple[Any, float]], dict[str, Any]]:
    fp_diag = _fingerprint_retrieval_gate()
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
    pairs, diagnostics = _apply_retrieval_controls(
        raw_results, scope=scope, k=k, question=q
    )
    diagnostics = {**(diagnostics or {}), **fp_diag}
    return pairs, diagnostics


def retrieve(question: str, scope: str | None = None, k: int | None = None):
    return [doc for doc, _ in retrieve_with_scores(question, scope=scope, k=k)]


def _sources_from_pairs(pairs: list[tuple[Any, float]]) -> list[dict]:
    """Build human-facing source citations from retrieval pairs.

    Chroma / Document metadata ``page`` remains the internal 0-based
    ``page_index`` for PDFs. Public source objects emit:
    * ``page`` — 1-based human/viewer citation page
    * ``page_index`` — internal stored index (when parseable)
    """
    sources: list[dict] = []
    seen: set[tuple] = set()
    for doc, distance in pairs:
        meta = enrich_metadata(doc.metadata)
        doc.metadata = meta
        src = meta.get("source", "unknown")
        stored_page = meta.get("page", "?")
        coll = meta.get("collection", "other")
        # Dedupe on internal stored page so conversion cannot merge distinct sheets.
        key = (src, stored_page, coll)
        if key in seen:
            continue
        seen.add(key)
        entry: dict[str, Any] = {
            "path": src,
            "collection": coll,
            # Chroma L2 distance: lower = closer/more relevant.
            "distance": float(distance),
            "authority_rank": int(meta.get("authority_rank", 5)),
            "machine_transcribed": bool(meta.get("machine_transcribed", False)),
        }
        fields = citation_page_fields(stored_page, source=str(src))
        if fields:
            entry["page"] = fields["page"]
            entry["page_index"] = fields["page_index"]
        else:
            # Preserve opaque / missing values without inventing a page number.
            entry["page"] = stored_page
        sources.append(entry)
    return sources


def _chunks_from_pairs(pairs: list[tuple[Any, float]]) -> list[dict]:
    """Build evidence chunks (citation metadata + text) for Hermes generation."""
    chunks: list[dict] = []
    for doc, distance in pairs:
        meta = enrich_metadata(doc.metadata)
        doc.metadata = meta
        src = meta.get("source", "unknown")
        stored_page = meta.get("page", "?")
        entry: dict[str, Any] = {
            "path": src,
            "collection": meta.get("collection", "other"),
            "distance": float(distance),
            "authority_rank": int(meta.get("authority_rank", 5)),
            "machine_transcribed": bool(meta.get("machine_transcribed", False)),
            "text": (doc.page_content or "").strip(),
        }
        fields = citation_page_fields(stored_page, source=str(src))
        if fields:
            entry["page"] = fields["page"]
            entry["page_index"] = fields["page_index"]
        else:
            entry["page"] = stored_page
        chunks.append(entry)
    return chunks


def _build_retrieval_context(pairs: list[tuple[Any, float]]) -> str:
    """Plain-text evidence block Hermes (or a human CLI) can feed to a model."""
    parts: list[str] = []
    for doc, _score in pairs:
        meta = enrich_metadata(doc.metadata)
        doc.metadata = meta
        src = meta.get("source")
        stored = meta.get("page")
        human = viewer_page(stored, source=str(src) if src is not None else None)
        page_for_prompt = human if human is not None else stored
        parts.append(
            f"[source={src} page={page_for_prompt} "
            f"collection={meta.get('collection')}]\n{(doc.page_content or '').strip()}"
        )
    return "\n\n".join(parts)


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
    """Archived prompt builder (ORCH_104). Unused by the retrieval-only ask path."""
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
    """Archived (ORCH_104). Ask path must not call a generation provider."""
    raise RuntimeError(
        "rag_engine ask path is retrieval-only (ORCH_104); "
        "Hermes owns final answer generation "
        f"(model={model!r}, timeout={timeout!r}, prompt_chars={len(prompt or '')})"
    )


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
    # Lifecycle contract (schema v4 / Phase 11A): any clarification_required
    # response means confirmation continuation must run a fresh constrained
    # retrieval and must not reuse pre-confirmation evidence. technical_state
    # already names the stage; do not conflate it with this invariant.
    diagnostics["clarification"] = {
        "technical_state": technical_state,
        "prompt": prompt,
        "fresh_retrieval_required": True,
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
    """Grounded retrieval. Returns AskResult evidence package (no NL generation).

    Statuses: ok | no_coverage | clarification_required | empty_question | error.
    On ``ok``, ``answer`` is null (Hermes generates). Clarification prompts still
    use ``answer``. ``model`` is accepted for CLI compatibility and ignored.
    """
    t0 = time.perf_counter()
    raw_q = question or ""
    q = normalize_text(raw_q)
    req = requested_scope if requested_scope is not None else scope
    resolved = scope
    # Retrieval-only: do not resolve or claim a chat/completion model.
    _ = model

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
            model=None,
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
                model=None,
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
            model=None,
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
            model=None,
            retrieval_evidence=[],
            retrieval_diagnostics=_empty_retrieval_diagnostics(),
            gate="retrieval_timeout",
        )
    except FingerprintError as e:
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            error=str(e),
            timings=_timings(retrieval=retrieval_s),
            model=None,
            retrieval_evidence=[],
            retrieval_diagnostics={
                **_empty_retrieval_diagnostics(),
                "index_fingerprint_state": getattr(e, "details", {}).get("state"),
                "index_fingerprint_reason": str(e),
            },
            gate="index_fingerprint",
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
            model=None,
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
            model=None,
            best_distance=retrieval_diag.get("best_raw_distance"),
            score_floor=retrieval_diag.get("score_floor"),
            retrieval_evidence=[],
            retrieval_diagnostics=retrieval_diag,
            gate=resolved_gate,
        )

    retrieval_evidence = _sources_from_pairs(pairs)
    pairs, retrieval_diag = _apply_final_confidence_gate(
        pairs,
        diagnostics=retrieval_diag,
        question=q,
    )
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
            model=None,
            best_distance=retrieval_diag.get("best_raw_distance"),
            score_floor=retrieval_diag.get("score_floor"),
            retrieval_evidence=retrieval_evidence,
            retrieval_diagnostics=retrieval_diag,
            gate="final_confidence_failed",
        )

    sources = _sources_from_pairs(pairs)
    chunks = _chunks_from_pairs(pairs)
    context = _build_retrieval_context(pairs)
    conservative_success = retrieval_is_conservative_success(pairs)

    # Deterministic source-locator response (metadata only — not LLM generation).
    if is_source_only_query(q) and conservative_success:
        return AskResult(
            status="ok",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=build_source_only_answer(sources, resolved),
            sources=sources,
            retrieved_chunks=chunks,
            retrieval_context=context,
            coverage="full",
            missing_information=None,
            timings=_timings(retrieval=retrieval_s),
            model=None,
            best_distance=retrieval_diag.get("best_raw_distance"),
            score_floor=retrieval_diag.get("score_floor"),
            retrieval_evidence=sources,
            retrieval_diagnostics=retrieval_diag,
            gate="ok",
        )

    # Successful retrieval package — Hermes performs NL generation.
    return AskResult(
        status="ok",
        query=q,
        requested_scope=req,
        resolved_scope=resolved,
        answer=None,
        sources=sources,
        retrieved_chunks=chunks,
        retrieval_context=context,
        coverage="full",
        missing_information=None,
        timings=_timings(retrieval=retrieval_s),
        model=None,
        best_distance=retrieval_diag.get("best_raw_distance"),
        score_floor=retrieval_diag.get("score_floor"),
        retrieval_evidence=sources,
        retrieval_diagnostics=retrieval_diag,
        gate="ok",
    )

"""Shared retrieval + answer synthesis for CLI and Gradio."""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings, OllamaLLM

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
)
from rag_engine.text import normalize_text

# Exit semantics for CLI / Hermes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_COVERAGE = 2

# v3: sources[].score renamed to sources[].distance (Chroma L2 distance,
# lower = more relevant). v2: plain-text generation, no partial_coverage.
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


def retrieve_with_scores(
    question: str,
    scope: str | None = None,
    k: int | None = None,
) -> list[tuple[Any, float]]:
    db = _get_db()
    k = default_k() if k is None else k
    q = normalize_text(question)

    def _call():
        kwargs: dict[str, Any] = {"k": k}
        if scope:
            kwargs["filter"] = {"collection": scope}
        return db.similarity_search_with_score(q, **kwargs)

    return _run_with_timeout(_call)


def retrieve(question: str, scope: str | None = None, k: int | None = None):
    return [doc for doc, _ in retrieve_with_scores(question, scope=scope, k=k)]


def _sources_from_pairs(pairs: list[tuple[Any, float]]) -> list[dict]:
    sources: list[dict] = []
    seen: set[tuple] = set()
    for doc, distance in pairs:
        meta = doc.metadata or {}
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
            }
        )
    return sources


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
        )

    retrieval_s: float | None = None
    try:
        t_ret = time.perf_counter()
        pairs = retrieve_with_scores(q, scope=resolved, k=k)
        retrieval_s = time.perf_counter() - t_ret
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
            model=chosen_model,
        )

    sources = _sources_from_pairs(pairs)
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
        )
    generation_s = time.perf_counter() - t_gen

    if model_declared_not_in_context(raw):
        # Chunks were retrieved but do not answer the question.
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
    )

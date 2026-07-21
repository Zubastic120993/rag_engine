"""Shared retrieval + answer synthesis for CLI and Gradio."""

from __future__ import annotations

import json
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

SCHEMA_VERSION = 2

DEFAULT_NO_COVERAGE_HINT = (
    "No supporting chunks were found in this scope. "
    "The requested document may belong to another scope; "
    "use scope diagnostics rather than answering from model memory."
)

_COVERAGE_TO_STATUS = {
    "full": "ok",
    "partial": "partial_coverage",
    "none": "no_coverage",
}

_STATUSES_WITH_SOURCES = frozenset({"ok", "partial_coverage"})


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
    status: str  # ok | partial_coverage | no_coverage | empty_question | error
    query: str
    requested_scope: str | None
    resolved_scope: str | None
    answer: str | None = None
    sources: list[dict] = field(default_factory=list)
    hint: str | None = None
    error: str | None = None
    coverage: str | None = None  # full | partial | none
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
    for doc, score in pairs:
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
                "score": float(score),
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


def parse_model_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from model output (raw or fenced)."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")

    # Prefer fenced ```json ... ``` / ``` ... ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        return json.loads(fence.group(1))

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return json.loads(raw[start : end + 1])
    raise ValueError("model response is not valid JSON")


def detect_internal_contradiction(
    answer: str | None,
    missing_information: str | None,
) -> str | None:
    """Return an error message if answer+missing contradict on thresholds."""
    combined = " ".join(
        p for p in ((answer or "").strip(), (missing_information or "").strip()) if p
    )
    if not combined:
        return None

    lower = combined.lower()

    # Asserts numeric alarm / limit / threshold behavior
    asserts_threshold = bool(
        re.search(
            r"(?:"
            r"(?:alarm|indicator|limit|threshold|setpoint|trip)\s+"
            r"(?:activates?|triggers?|trips?|sounds?|occurs?|starts?|sets?)?\s*"
            r"(?:at|when|if|above|over|below|under|exceeds?)?\s*"
            r"\d+(?:\.\d+)?\s*(?:ppm|%|bar|pa|kpa|mpa|deg|°|c|f)?|"
            r"(?:above|over|below|under|exceeds?|activates?\s+at|triggers?\s+at|"
            r"trips?\s+at|occurs?\s+at)\s+\d+(?:\.\d+)?\s*"
            r"(?:ppm|%|bar|pa|kpa|mpa|deg|°|c|f)?|"
            r"\d+(?:\.\d+)?\s*ppm"
            r")",
            lower,
            re.IGNORECASE,
        )
    )
    # Claims threshold / setpoint / alarm limit is unspecified
    claims_unspecified = bool(
        re.search(
            r"(?:"
            r"(?:exact\s+)?(?:alarm\s+)?(?:threshold|setpoint|alarm\s+limit|limit|"
            r"set\s*point)\s+"
            r"(?:is\s+|are\s+|was\s+|were\s+)?"
            r"(?:not\s+specified|unspecified|unknown|not\s+given|not\s+stated|"
            r"not\s+provided|not\s+defined)|"
            r"(?:no|without)\s+(?:exact\s+)?(?:threshold|setpoint|alarm\s+limit|"
            r"limit)|"
            r"(?:threshold|setpoint|alarm\s+limit)\s+(?:not\s+specified|unspecified)"
            r")",
            lower,
            re.IGNORECASE,
        )
    )
    if asserts_threshold and claims_unspecified:
        return (
            "internal contradiction: states a numeric alarm/limit/threshold "
            "behavior while also claiming the threshold/setpoint is unspecified"
        )
    return None


def normalize_coverage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize LLM JSON into coverage / answer / missing_information / status."""
    if not isinstance(payload, dict):
        raise ValueError("coverage payload must be an object")

    cov_raw = payload.get("coverage")
    if cov_raw is None:
        raise ValueError("missing coverage field")
    cov = str(cov_raw).strip().lower()
    aliases = {
        "complete": "full",
        "full_coverage": "full",
        "partial_coverage": "partial",
        "incomplete": "partial",
        "no": "none",
        "no_coverage": "none",
        "empty": "none",
    }
    cov = aliases.get(cov, cov)
    if cov not in _COVERAGE_TO_STATUS:
        raise ValueError(f"unknown coverage value: {cov_raw!r}")

    # Empty string answer → null; reject empty for full/partial below
    answer_raw = payload.get("answer")
    if answer_raw is None:
        answer: str | None = None
    else:
        answer = str(answer_raw).strip() or None

    missing = payload.get("missing_information")
    if missing is not None:
        missing = str(missing).strip() or None

    if cov == "full":
        if not answer:
            raise ValueError("full coverage requires non-empty answer")
    elif cov == "partial":
        if not answer:
            raise ValueError(
                "partial coverage requires non-empty answer and non-empty "
                "missing_information"
            )
        if not missing:
            raise ValueError(
                "partial coverage requires non-empty answer and non-empty "
                "missing_information"
            )
    elif cov == "none":
        # Empty string already coerced to None; non-empty with none is invalid
        if answer is not None:
            raise ValueError("none coverage requires answer=null")
        answer = None

    contradiction = detect_internal_contradiction(answer, missing)
    if contradiction:
        raise ValueError(contradiction)

    status = _COVERAGE_TO_STATUS[cov]
    return {
        "coverage": cov,
        "status": status,
        "answer": answer,
        "missing_information": missing,
    }


def _build_prompt(question: str, context: str, resolved: str | None) -> str:
    scope_line = (
        f"Search scope: {resolved}\n" if resolved else "Search scope: entire corpus\n"
    )
    return (
        "You are a grounded document assistant. Use ONLY the context below.\n"
        "Respond with ONLY a single JSON object (no markdown, no prose) with keys:\n"
        '  "coverage": one of "full", "partial", "none",\n'
        '  "answer": string or null,\n'
        '  "missing_information": string or null\n'
        "Rules:\n"
        "- Preserve every useful fact directly supported by the retrieved context.\n"
        '- "full": context fully answers the question; put the answer in "answer"; '
        'set "missing_information" to null.\n'
        '- "partial": use only when part of the question is answered and part '
        "remains unsupported; put what is supported in \"answer\"; describe gaps "
        'in "missing_information".\n'
        '- "none": context does not support an answer; set "answer" to null; '
        'optionally explain in "missing_information".\n'
        "Invariants: full ⇒ non-empty answer; partial ⇒ non-empty answer AND "
        "non-empty missing_information; none ⇒ answer null.\n"
        "A numeric value described as the point where an alarm activates, an "
        "indicator changes, or a limit is exceeded is a threshold/setpoint — "
        "state that value in answer.\n"
        "Never claim a value is unspecified when the same answer (or "
        "missing_information) states that the alarm behavior occurs at that value.\n"
        "Do not invent values, part numbers, crew data, or procedures "
        "from adjacent or unrelated manuals.\n"
        f"{scope_line}\n"
        f"Context:\n{context}\n\n"
        f"Question: {question}\n\n"
        "JSON:"
    )


def _build_correction_prompt(
    question: str,
    context: str,
    resolved: str | None,
    previous_raw: str,
    error_msg: str,
) -> str:
    scope_line = (
        f"Search scope: {resolved}\n" if resolved else "Search scope: entire corpus\n"
    )
    return (
        "Your previous JSON response was invalid. Produce a corrected JSON object "
        "only (no markdown, no prose).\n"
        f"Error: {error_msg}\n"
        "Invariants: full ⇒ non-empty answer; partial ⇒ non-empty answer AND "
        "non-empty missing_information; none ⇒ answer null.\n"
        "Preserve every useful fact directly supported by the retrieved context.\n"
        "Use partial only when part of the question is answered and part remains "
        "unsupported.\n"
        "A numeric value described as the point where an alarm activates, an "
        "indicator changes, or a limit is exceeded is a threshold/setpoint — "
        "state that value in answer.\n"
        "Never claim a value is unspecified when the same answer (or "
        "missing_information) states that the alarm behavior occurs at that value.\n"
        "Keys: coverage (full|partial|none), answer (string|null), "
        "missing_information (string|null).\n"
        f"{scope_line}\n"
        f"Context:\n{context}\n\n"
        f"Question: {question}\n\n"
        f"Previous output:\n{previous_raw}\n\n"
        "Corrected JSON:"
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


def _parse_and_validate(raw: str) -> dict[str, Any]:
    payload = parse_model_json(raw)
    return normalize_coverage_payload(payload)


class GenerationFailed(Exception):
    """Primary + repair (+ optional explicit heavy fallback) all failed.

    Carries enough detail for the caller to build a deterministic salvage
    answer from retrieved evidence instead of surfacing a bare error.
    """

    def __init__(
        self,
        message: str,
        *,
        phase_timings: dict[str, float | None],
        total_s: float,
        last_raw: str = "",
    ) -> None:
        super().__init__(message)
        self.phase_timings = phase_timings
        self.total_s = total_s
        self.last_raw = last_raw


def _split_context_body(part: str) -> str:
    """Strip the leading '[source=... page=... collection=...]' tag line."""
    if part.startswith("[") and "]\n" in part:
        return part.split("]\n", 1)[1].strip()
    return part.strip()


def deterministic_salvage_answer(
    context_parts: list[str], *, max_sentences: int = 5
) -> str:
    """Build a conservative extractive answer directly from retrieved text.

    No model call — used only when generation is unavailable but retrieval
    already produced relevant chunks. Pulls the leading sentences of each
    retrieved chunk in ranked order until max_sentences is reached, so the
    result is traceable back to the sources list rather than synthesized.
    """
    sentences: list[str] = []
    seen: set[str] = set()
    for part in context_parts:
        body = _split_context_body(part)
        if not body:
            continue
        for s in re.split(r"(?<=[.!?])\s+", body):
            s = s.strip()
            if len(s) < 20:
                continue
            if s in seen:
                continue
            seen.add(s)
            sentences.append(s)
            if len(sentences) >= max_sentences:
                break
        if len(sentences) >= max_sentences:
            break
    return " ".join(sentences)


def _generate_with_repair(
    *,
    question: str,
    context: str,
    resolved: str | None,
    primary_model: str,
    use_fallback: bool,
    num_ctx: int | None,
    num_predict: int | None,
) -> tuple[dict[str, Any], str, dict[str, float | None], float]:
    """Generate, repair at most once, done.

    Exactly two generation calls in the default case: one primary attempt
    on ``primary_model``, and — only if that fails to parse/validate — one
    repair attempt on the fast default model (never a repeated or recursive
    loop). The heavy fallback model is invoked only when ``use_fallback`` is
    True, and only as the primary attempt's model (via resolve_answer_model);
    it is never chained in automatically after a failed repair.

    Returns (norm, model_used, phase_timings, total_seconds). Raises
    GenerationFailed if every attempt failed to produce valid output.
    """
    t0 = time.perf_counter()
    main_prompt = _build_prompt(question, context, resolved)
    last_error = "malformed model JSON"
    previous_raw = ""
    phase_timings: dict[str, float | None] = {
        "primary": None,
        "repair": None,
        "fallback": None,
    }

    def _attempt(model: str, prompt: str, phase: str) -> dict[str, Any] | None:
        nonlocal last_error, previous_raw
        t_phase = time.perf_counter()
        try:
            raw = _invoke_llm(model, prompt, num_ctx, num_predict)
            previous_raw = raw
            result = _parse_and_validate(raw)
            phase_timings[phase] = time.perf_counter() - t_phase
            return result
        except TimeoutError as e:
            last_error = str(e)
            phase_timings[phase] = time.perf_counter() - t_phase
            return None
        except (ValueError, json.JSONDecodeError, TypeError) as e:
            last_error = str(e)
            phase_timings[phase] = time.perf_counter() - t_phase
            return None

    # 1) one primary attempt, main prompt
    norm = _attempt(primary_model, main_prompt, "primary")
    if norm is not None:
        return norm, primary_model, phase_timings, time.perf_counter() - t0

    # 2) exactly one repair attempt — always the fast default model, even if
    # the primary attempt already used the heavy model via --fallback. This
    # is the "no recursive or repeated repair loop" boundary: whatever
    # happens next, we do not call back into _attempt a second time here.
    fast_model = llm_model()
    corr = _build_correction_prompt(
        question, context, resolved, previous_raw, last_error
    )
    norm = _attempt(fast_model, corr, "repair")
    if norm is not None:
        return norm, fast_model, phase_timings, time.perf_counter() - t0

    # 3) heavy fallback — ONLY when explicitly requested, and only if it
    # would be a genuinely different model from what was already tried.
    if use_fallback:
        fallback_name = llm_fallback_model()
        if fallback_name and fallback_name not in (primary_model, fast_model):
            corr_fb = _build_correction_prompt(
                question, context, resolved, previous_raw, last_error
            )
            norm = _attempt(fallback_name, corr_fb, "fallback")
            if norm is not None:
                return norm, fallback_name, phase_timings, time.perf_counter() - t0

    raise GenerationFailed(
        f"malformed model JSON after repair: {last_error}",
        phase_timings=phase_timings,
        total_s=time.perf_counter() - t0,
        last_raw=previous_raw,
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
    """Grounded ask. Returns AskResult (ok | partial_coverage | no_coverage | …)."""
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

    generation_s: float | None = None
    model_used = chosen_model
    try:
        norm, model_used, phase_timings, generation_s = _generate_with_repair(
            question=q,
            context=context,
            resolved=resolved,
            primary_model=chosen_model,
            use_fallback=use_fallback,
            num_ctx=ctx_size,
            num_predict=predict,
        )
    except GenerationFailed as e:
        # Primary + one fast-model repair (+ explicit heavy fallback, if
        # requested) all failed to produce valid output. Retrieval already
        # succeeded, so degrade to a deterministic, sourced partial answer
        # instead of throwing away good evidence behind status=error.
        salvage = deterministic_salvage_answer(context_parts)
        gen_timings = dict(
            generation_primary=e.phase_timings.get("primary"),
            generation_repair=e.phase_timings.get("repair"),
            generation_fallback=e.phase_timings.get("fallback"),
            generation=e.total_s,
        )
        if salvage:
            return AskResult(
                status="partial_coverage",
                query=q,
                requested_scope=req,
                resolved_scope=resolved,
                answer=salvage,
                sources=sources,
                coverage="partial",
                missing_information=(
                    "Automated synthesis was unavailable (model returned "
                    f"malformed or empty output: {e}). The answer above is a "
                    "conservative excerpt taken directly from the retrieved "
                    "passages, not an AI-generated summary, and may omit "
                    "relevant detail."
                ),
                timings=_timings(retrieval=retrieval_s, **gen_timings),
                model=model_used,
            )
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=sources,
            error=str(e),
            timings=_timings(retrieval=retrieval_s, **gen_timings),
            model=model_used,
        )
    except TimeoutError as e:
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=sources,
            error=str(e),
            timings=_timings(retrieval=retrieval_s),
            model=model_used,
        )
    except Exception as e:  # noqa: BLE001
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=sources,
            error=str(e),
            timings=_timings(retrieval=retrieval_s),
            model=model_used,
        )

    status = norm["status"]
    coverage = norm["coverage"]
    ans = norm["answer"]
    missing = norm["missing_information"]
    out_sources = sources if status in _STATUSES_WITH_SOURCES else []
    hint = None
    if status == "no_coverage":
        hint = _no_coverage_hint(q, resolved, suggest_scopes)
        if missing and not suggest_scopes:
            hint = missing

    return AskResult(
        status=status,
        query=q,
        requested_scope=req,
        resolved_scope=resolved,
        answer=ans,
        sources=out_sources,
        coverage=coverage,
        missing_information=missing,
        hint=hint,
        timings=_timings(
            retrieval=retrieval_s,
            generation_primary=phase_timings.get("primary"),
            generation_repair=phase_timings.get("repair"),
            generation_fallback=phase_timings.get("fallback"),
            generation=generation_s,
        ),
        model=model_used,
    )

"""Shared retrieval + answer synthesis for CLI and Gradio."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings, OllamaLLM

from rag_engine.config import default_k, embed_model, known_scopes, llm_model, persist_dir
from rag_engine.text import normalize_text

# Exit semantics for CLI / Hermes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_COVERAGE = 2

SCHEMA_VERSION = 1

DEFAULT_NO_COVERAGE_HINT = (
    "No supporting chunks were found in this scope. "
    "The requested document may belong to another scope; "
    "use scope diagnostics rather than answering from model memory."
)


def ollama_timeout_s() -> float:
    # Default 300s: local LLM asks under load exceed 120s and must still
    # surface as exit 1 rather than hang forever for Hermes.
    return float(os.environ.get("RAG_OLLAMA_TIMEOUT", "300"))


def suggest_score_max() -> float:
    """Chroma distance threshold: lower is better; keep hits at or below this."""
    return float(os.environ.get("RAG_SUGGEST_SCORE_MAX", "1.2"))


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

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "query": self.query,
            "requested_scope": self.requested_scope,
            "resolved_scope": self.resolved_scope,
            "answer": self.answer,
            "sources": self.sources if self.status == "ok" else [],
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


@lru_cache(maxsize=1)
def _get_llm() -> OllamaLLM:
    return OllamaLLM(model=llm_model(), temperature=0)


def clear_caches() -> None:
    _get_db.cache_clear()
    _get_llm.cache_clear()


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


def answer(
    question: str,
    scope: str | None = None,
    k: int | None = None,
    *,
    requested_scope: str | None = None,
    suggest_scopes: bool = False,
) -> AskResult:
    """Grounded ask. Returns AskResult (status ok | no_coverage | …)."""
    raw_q = question or ""
    q = normalize_text(raw_q)
    req = requested_scope if requested_scope is not None else scope
    resolved = scope

    if not q:
        return AskResult(
            status="empty_question",
            query=raw_q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=[],
            hint=DEFAULT_NO_COVERAGE_HINT,
        )

    try:
        pairs = retrieve_with_scores(q, scope=resolved, k=k)
    except TimeoutError as e:
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            error=str(e),
        )
    except Exception as e:  # noqa: BLE001
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            error=str(e),
        )

    if not pairs:
        hint = DEFAULT_NO_COVERAGE_HINT
        if suggest_scopes:
            alts = suggest_other_scopes(q, exclude=resolved)
            if alts:
                names = ", ".join(
                    f"{a['scope']} (score={a['best_score']:.3f})" for a in alts[:5]
                )
                hint = (
                    f"{DEFAULT_NO_COVERAGE_HINT} "
                    f"Verified hits exist in other scopes: {names}."
                )
        return AskResult(
            status="no_coverage",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=[],
            hint=hint,
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
    scope_line = (
        f"Search scope: {resolved}\n" if resolved else "Search scope: entire corpus\n"
    )
    prompt = (
        "Answer the question using ONLY the context below. "
        "If the context does not contain the answer, say exactly: "
        "I do not know — not specified in the retrieved documents.\n"
        "Do not invent values, part numbers, crew data, or procedures "
        "from adjacent or unrelated manuals.\n"
        f"{scope_line}\n"
        f"Context:\n{context}\n\n"
        f"Question: {q}\n\nAnswer:"
    )

    try:
        text = str(_run_with_timeout(lambda: _get_llm().invoke(prompt))).strip()
    except TimeoutError as e:
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            error=str(e),
        )
    except Exception as e:  # noqa: BLE001
        return AskResult(
            status="error",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            error=str(e),
        )

    low = text.lower()
    if "i do not know" in low or "not specified in the retrieved" in low:
        hint = DEFAULT_NO_COVERAGE_HINT
        if suggest_scopes:
            alts = suggest_other_scopes(q, exclude=resolved)
            if alts:
                names = ", ".join(
                    f"{a['scope']} (score={a['best_score']:.3f})" for a in alts[:5]
                )
                hint = (
                    f"{DEFAULT_NO_COVERAGE_HINT} "
                    f"Verified hits exist in other scopes: {names}."
                )
        return AskResult(
            status="no_coverage",
            query=q,
            requested_scope=req,
            resolved_scope=resolved,
            answer=None,
            sources=[],
            hint=hint,
        )

    return AskResult(
        status="ok",
        query=q,
        requested_scope=req,
        resolved_scope=resolved,
        answer=text,
        sources=sources,
    )

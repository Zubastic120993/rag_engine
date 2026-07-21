"""Shared retrieval + answer synthesis for CLI and Gradio."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings, OllamaLLM

from rag_engine.config import default_k, embed_model, llm_model, persist_dir
from rag_engine.text import normalize_text

# Exit semantics for CLI / Hermes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_COVERAGE = 2


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


def retrieve_with_scores(
    question: str,
    scope: str | None = None,
    k: int | None = None,
) -> list[tuple[Any, float]]:
    db = _get_db()
    k = default_k() if k is None else k
    kwargs: dict[str, Any] = {"k": k}
    if scope:
        kwargs["filter"] = {"collection": scope}
    return db.similarity_search_with_score(normalize_text(question), **kwargs)


def retrieve(question: str, scope: str | None = None, k: int | None = None):
    return [doc for doc, _ in retrieve_with_scores(question, scope=scope, k=k)]


def answer(
    question: str,
    scope: str | None = None,
    k: int | None = None,
) -> tuple[str, list[dict], str]:
    """Return (answer_text, sources, status).

    status is \"ok\" | \"no_coverage\" | \"empty_question\".
    """
    question = normalize_text(question or "")
    if not question:
        return "Please provide a question.", [], "empty_question"

    pairs = retrieve_with_scores(question, scope=scope, k=k)
    if not pairs:
        scope_note = f" in scope '{scope}'" if scope else ""
        return f"No relevant documents found{scope_note}.", [], "no_coverage"

    context_parts = []
    sources: list[dict] = []
    seen: set[tuple] = set()
    for doc, score in pairs:
        meta = doc.metadata or {}
        src = meta.get("source", "unknown")
        page = meta.get("page", "?")
        coll = meta.get("collection", "other")
        key = (src, page, coll)
        if key not in seen:
            seen.add(key)
            sources.append(
                {
                    "path": src,
                    "page": page,
                    "collection": coll,
                    "score": float(score),
                }
            )
        context_parts.append(
            f"[source={src} page={page} collection={coll}]\n{doc.page_content.strip()}"
        )

    context = "\n\n".join(context_parts)
    scope_line = f"Search scope: {scope}\n" if scope else "Search scope: entire corpus\n"
    prompt = (
        "Answer the question using ONLY the context below. "
        "If the context does not contain the answer, say exactly: "
        "I do not know — not specified in the retrieved documents.\n"
        "Do not invent values, part numbers, crew data, or procedures "
        "from adjacent or unrelated manuals.\n"
        f"{scope_line}\n"
        f"Context:\n{context}\n\n"
        f"Question: {question}\n\nAnswer:"
    )
    text = str(_get_llm().invoke(prompt)).strip()
    low = text.lower()
    if "i do not know" in low or "not specified in the retrieved" in low:
        return text, sources, "no_coverage"
    return text, sources, "ok"

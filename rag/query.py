"""Shared retrieval + answer synthesis for CLI and Gradio."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings, OllamaLLM

from rag.config import DEFAULT_K, EMBED_MODEL, LLM_MODEL, PERSIST_DIR
from rag.text import normalize_text


@lru_cache(maxsize=1)
def _get_db() -> Chroma:
    embeddings = OllamaEmbeddings(model=EMBED_MODEL)
    return Chroma(persist_directory=str(PERSIST_DIR), embedding_function=embeddings)


@lru_cache(maxsize=1)
def _get_llm() -> OllamaLLM:
    return OllamaLLM(model=LLM_MODEL, temperature=0)


def retrieve(question: str, scope: str | None = None, k: int = DEFAULT_K):
    db = _get_db()
    kwargs: dict[str, Any] = {"k": k}
    if scope:
        kwargs["filter"] = {"collection": scope}
    # Normalize query the same way as stored chunks (µ → μ, ligatures, etc.)
    return db.similarity_search(normalize_text(question), **kwargs)


def answer(
    question: str,
    scope: str | None = None,
    k: int = DEFAULT_K,
) -> tuple[str, list[dict]]:
    """Return (answer_text, sources) for a question, optionally scoped."""
    question = normalize_text(question or "")
    if not question:
        return "Please provide a question.", []

    docs = retrieve(question, scope=scope, k=k)
    if not docs:
        scope_note = f" in scope '{scope}'" if scope else ""
        return f"No relevant documents found{scope_note}.", []

    context_parts = []
    sources: list[dict] = []
    seen: set[tuple] = set()
    for doc in docs:
        meta = doc.metadata or {}
        src = meta.get("source", "unknown")
        page = meta.get("page", "?")
        coll = meta.get("collection", "other")
        key = (src, page, coll)
        if key not in seen:
            seen.add(key)
            sources.append({"source": src, "page": page, "collection": coll})
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
    return text, sources

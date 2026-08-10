"""AI Chief Engineer Alpha — presentation helpers over rag_engine.answer.

The Gradio shell in app.py must call these helpers (or answer() itself).
ORCH_104: rag_engine is retrieval-only; Alpha surfaces evidence packages.
Final NL answer generation is Hermes-owned (not OpenAI-in-engine).
"""

from __future__ import annotations

import os
import re
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import requests

from rag_engine.config import (
    embed_model,
    library_root,
    persist_dir,
)
from rag_engine.pdf_links import source_open_markdown, viewer_page
from rag_engine.query import AskResult, answer

DISPLAY_STATUSES = ("ok", "clarification_required", "no_coverage", "error")

_NO_COVERAGE_TEXT = "I do not know — not specified in the retrieved documents."
_RETRIEVAL_ONLY_MSG = (
    "Retrieval package ready. Final answer generation is Hermes-owned; "
    "review sources below."
)
_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-*]+")


def _sanitize_display_text(text: str) -> str:
    """Never surface API key material in the Alpha UI."""
    cleaned = _API_KEY_RE.sub("[REDACTED]", text or "")
    if "Incorrect API key" in cleaned or "invalid_api_key" in cleaned:
        return "Provider error: API key rejected."
    return cleaned


def document_name(path: str | None) -> str:
    raw = str(path or "").replace("\\", "/").strip()
    if not raw:
        return "unknown"
    return Path(raw).name or raw


def _stored_page_for_url(source: dict[str, Any]) -> Any:
    """Page value to feed into viewer_page / PDF #page= helpers.

    Prefer ``page_index`` (0-based) when present so a human ``page`` is never
    double-incremented. Legacy sources without ``page_index`` still carry the
    raw stored index in ``page``.
    """
    if "page_index" in source:
        return source.get("page_index")
    return source.get("page")


def _human_citation_page(source: dict[str, Any]) -> int | None:
    """1-based citation page for display; never double-convert."""
    path = str(source.get("path") or "") or None
    if "page_index" in source:
        # Public boundary already normalized ``page``; trust it when parseable.
        raw = source.get("page")
        if raw is not None and raw != "?":
            try:
                n = int(raw)
            except (TypeError, ValueError):
                n = None
            if n is not None and n >= 1:
                return n
        return viewer_page(source.get("page_index"), source=path)
    # Legacy: ``page`` is still the stored 0-based index.
    return viewer_page(source.get("page"), source=path)


def format_source_line(source: dict[str, Any], *, index: int, root: Path | None = None) -> str:
    path = str(source.get("path") or "")
    name = document_name(path)
    vp = _human_citation_page(source)
    page_note = f"p.{vp}" if vp is not None else "p.?"
    scope = str(source.get("collection") or source.get("scope") or "unknown")
    link = source_open_markdown(path, _stored_page_for_url(source), root=root)
    return (
        f"{index}. {name} — {page_note}\n"
        f"   scope: `{scope}`\n"
        f"   path: `{path}`\n"
        f"   open: {link}"
    )


def format_sources_markdown(sources: list[dict[str, Any]] | None, *, root: Path | None = None) -> str:
    if not sources:
        return "_No sources._"
    lines = [
        format_source_line(src, index=i, root=root)
        for i, src in enumerate(sources, start=1)
    ]
    lines.append(
        "\n_PDF `#page=N` works in Chrome/Firefox built-in viewers; "
        "Safari’s viewer is unreliable._"
    )
    return "\n".join(lines)


def format_sources_copy_text(sources: list[dict[str, Any]] | None) -> str:
    if not sources:
        return ""
    lines: list[str] = []
    for i, src in enumerate(sources, start=1):
        path = str(src.get("path") or "")
        name = document_name(path)
        vp = _human_citation_page(src)
        page_note = f"p.{vp}" if vp is not None else "p.?"
        scope = str(src.get("collection") or src.get("scope") or "unknown")
        lines.append(f"{i}. {name} — {page_note} | scope={scope} | path={path}")
    return "\n".join(lines)


def display_answer_text(result: AskResult) -> str:
    status = result.status
    if status == "clarification_required":
        return (result.answer or "").strip() or "Which equipment/component do you mean?"
    if status == "ok":
        text = (result.answer or "").strip()
        if text:
            return text
        return _RETRIEVAL_ONLY_MSG
    if status == "no_coverage":
        parts = [_NO_COVERAGE_TEXT]
        if result.hint:
            parts.append(result.hint)
        return "\n\n".join(parts)
    if status == "empty_question":
        return "Enter an engineering question first."
    err = (result.error or "").strip()
    if err:
        return _sanitize_display_text(err)
    return _sanitize_display_text(result.answer or "Request failed.")


def normalize_status(status: str | None) -> str:
    raw = (status or "error").strip()
    if raw in DISPLAY_STATUSES:
        return raw
    if raw == "empty_question":
        return "error"
    return "error"


def ask(
    question: str,
    *,
    confirmation_text: str | None = None,
    scope: str | None = None,
    k: int | None = None,
) -> dict[str, Any]:
    """Call rag_engine.answer and shape a UI-friendly retrieval payload."""
    conf = None if confirmation_text is None else str(confirmation_text).strip()
    if conf == "":
        conf = None
    result = answer(
        question,
        scope=scope,
        k=k,
        confirmation_text=conf,
        requested_scope=scope,
    )
    status = normalize_status(result.status)
    show_sources = result.status == "ok" and bool(result.sources)
    sources = result.sources if show_sources else []
    clarification = result.status == "clarification_required"
    return {
        "status": status,
        "raw_status": result.status,
        "answer": display_answer_text(result),
        "sources_md": format_sources_markdown(sources),
        "sources_copy": format_sources_copy_text(sources),
        "retrieval_context": result.retrieval_context if show_sources else None,
        "retrieved_chunks": result.retrieved_chunks if show_sources else [],
        "clarification_required": clarification,
        "clarification_prompt": (result.answer or "").strip() if clarification else "",
        "pending_question": (question or "").strip() if clarification else "",
        "error": result.error,
        "hint": result.hint,
        "gate": result.gate,
        "model": result.model,
        "resolved_scope": result.resolved_scope,
        "generation_owner": "hermes",
    }


def _check_ollama_embed() -> tuple[bool, str]:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = embed_model()
    try:
        r = requests.get(f"{host}/api/tags", timeout=3)
        r.raise_for_status()
        models = r.json().get("models") or []
        names: set[str] = set()
        for m in models:
            n = str(m.get("name") or "")
            if not n:
                continue
            names.add(n)
            if ":" in n:
                names.add(n.split(":", 1)[0])
        ok = model in names or model.split(":")[0] in names
        if ok:
            return True, f"{model} available"
        return False, f"{model} not found on Ollama"
    except Exception as e:  # noqa: BLE001
        return False, f"Ollama unreachable ({e.__class__.__name__})"


def health_snapshot() -> dict[str, Any]:
    """Lightweight Alpha health: engine reachability and embeddings."""
    checks: list[dict[str, Any]] = []

    try:
        root = library_root()
        db = persist_dir()
        reachable = root.is_dir() and db.is_dir() and find_spec("rag_engine") is not None
        detail = f"library={root} db={db}"
        checks.append({"name": "rag_engine_reachable", "ok": reachable, "detail": detail})
    except Exception as e:  # noqa: BLE001
        checks.append({"name": "rag_engine_reachable", "ok": False, "detail": str(e)})

    checks.append(
        {
            "name": "generation_owner",
            "ok": True,
            "detail": "hermes (rag_engine is retrieval-only; ORCH_104)",
        }
    )

    embed_ok, embed_detail = _check_ollama_embed()
    checks.append(
        {
            "name": "embedding_backend_available",
            "ok": embed_ok,
            "detail": embed_detail,
        }
    )

    overall = "ready" if all(c["ok"] for c in checks) else "degraded"
    return {"status": overall, "checks": checks}


def format_health_markdown(snapshot: dict[str, Any] | None = None) -> str:
    snap = snapshot or health_snapshot()
    lines = [f"**Health:** `{snap.get('status', 'unknown')}`"]
    for c in snap.get("checks") or []:
        mark = "OK" if c.get("ok") else "FAIL"
        lines.append(f"- {c.get('name')}: **{mark}** — {c.get('detail')}")
    return "\n".join(lines)

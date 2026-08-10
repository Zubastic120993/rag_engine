"""AI Chief Engineer Alpha — presentation helpers over rag_engine.answer.

The Gradio shell in app.py must call these helpers (or answer() itself).
It must never call OpenAI directly.
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
    openai_api_key_env,
    persist_dir,
)
from rag_engine.pdf_links import source_open_markdown, viewer_page
from rag_engine.query import AskResult, answer

DISPLAY_STATUSES = ("ok", "clarification_required", "no_coverage", "error")

_NO_COVERAGE_TEXT = "I do not know — not specified in the retrieved documents."
_MISSING_KEY_MSG = "OpenAI API key not configured"
_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-*]+")


def _sanitize_display_text(text: str) -> str:
    """Never surface API key material in the Alpha UI."""
    cleaned = _API_KEY_RE.sub("[REDACTED]", text or "")
    if "Incorrect API key" in cleaned or "invalid_api_key" in cleaned:
        return "OpenAI provider error: API key rejected (check OPENAI_API_KEY)."
    if "Error code:" in cleaned and "openai" in cleaned.lower():
        return f"OpenAI provider error: {_API_KEY_RE.sub('[REDACTED]', cleaned)}"
    return cleaned


def document_name(path: str | None) -> str:
    raw = str(path or "").replace("\\", "/").strip()
    if not raw:
        return "unknown"
    return Path(raw).name or raw


def format_source_line(source: dict[str, Any], *, index: int, root: Path | None = None) -> str:
    path = str(source.get("path") or "")
    name = document_name(path)
    page = source.get("page")
    vp = viewer_page(page)
    page_note = f"p.{vp}" if vp is not None else "p.?"
    scope = str(source.get("collection") or source.get("scope") or "unknown")
    link = source_open_markdown(path, page, root=root)
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
        vp = viewer_page(src.get("page"))
        page_note = f"p.{vp}" if vp is not None else "p.?"
        scope = str(src.get("collection") or src.get("scope") or "unknown")
        lines.append(f"{i}. {name} — {page_note} | scope={scope} | path={path}")
    return "\n".join(lines)


def display_answer_text(result: AskResult) -> str:
    status = result.status
    if status == "clarification_required":
        return (result.answer or "").strip() or "Which equipment/component do you mean?"
    if status == "ok":
        return (result.answer or "").strip()
    if status == "no_coverage":
        parts = [_NO_COVERAGE_TEXT]
        if result.hint:
            parts.append(result.hint)
        return "\n\n".join(parts)
    if status == "empty_question":
        return "Enter an engineering question first."
    err = (result.error or "").strip()
    if "OPENAI_API_KEY is not set" in err or (
        "OPENAI_API_KEY" in err and "not set" in err
    ):
        return _MISSING_KEY_MSG
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
    """Call rag_engine.answer and shape a UI-friendly payload."""
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
        "clarification_required": clarification,
        "clarification_prompt": (result.answer or "").strip() if clarification else "",
        "pending_question": (question or "").strip() if clarification else "",
        "error": result.error,
        "hint": result.hint,
        "gate": result.gate,
        "model": result.model,
        "resolved_scope": result.resolved_scope,
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
    """Lightweight Alpha health: engine reachability, OpenAI key, embeddings."""
    checks: list[dict[str, Any]] = []

    try:
        root = library_root()
        db = persist_dir()
        reachable = root.is_dir() and db.is_dir() and find_spec("rag_engine") is not None
        detail = f"library={root} db={db}"
        checks.append({"name": "rag_engine_reachable", "ok": reachable, "detail": detail})
    except Exception as e:  # noqa: BLE001
        checks.append({"name": "rag_engine_reachable", "ok": False, "detail": str(e)})

    key_var = openai_api_key_env()
    key_present = bool(os.environ.get(key_var, "").strip())
    checks.append(
        {
            "name": "openai_api_key_configured",
            "ok": key_present,
            "detail": key_var if key_present else f"{key_var} missing — {_MISSING_KEY_MSG}",
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

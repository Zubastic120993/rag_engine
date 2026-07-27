"""Shared paths and settings — env-driven, scopes from registry YAML."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:11434")
os.environ.setdefault("LANGCHAIN_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("POSTHOG_DISABLED", "true")

PACKAGE_ROOT = Path(__file__).resolve().parent
SCOPES_FILE = PACKAGE_ROOT / "scopes.yaml"


def chroma_client_settings():
    """Shared Chroma Settings with telemetry off, for every call site that
    opens a Chroma client/collection. requirements.txt pins posthog<6.0.0
    so this flag is actually honored (posthog>=6.0.0 changed capture()'s
    signature in a way chromadb 0.5.23 doesn't call it, which crashed every
    chromadb call regardless of this setting — see the pin's comment).

    is_persistent=True is required here, not optional. chromadb.Settings
    defaults it to False, which routes chromadb/db/impl/sqlite.py's SqliteDB
    to an in-memory "file::memory:?cache=shared" connection instead of the
    real persist_directory — silently, with no error. chromadb.PersistentClient()
    forces is_persistent=True itself regardless of what's passed in, so
    doctor.py/diagnostics.py (which call it directly) were never affected.
    But langchain_chroma.Chroma(client_settings=...) does NOT force it — it
    only copies persist_directory onto whatever Settings object it's given
    and leaves is_persistent at the object's own default. Every call site
    that passes chroma_client_settings() as client_settings to
    langchain_chroma.Chroma (ingest.py, query.py, backfill_collections.py)
    was therefore silently writing to and reading from a throwaway in-memory
    database — one that verifies correctly within the same process (so
    _verify_ids() never caught it) but vanishes the moment the process
    exits, leaving the real on-disk index untouched and stale."""
    import chromadb

    return chromadb.config.Settings(anonymized_telemetry=False, is_persistent=True)


SKIP_DIR_PARTS = (
    ".rag_db",
    ".obsidian",
    "_Inbox",
    "_Backup",
    "/Graph",
    "Tools",
    "venv",
    "rag_env",
    ".git",
    "30_Knowledge",
    "_retired",
)


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    with SCOPES_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def library_root() -> Path:
    d = load_registry()["defaults"]
    env = os.environ.get(d["library_root_env"])
    if env:
        return _expand(env)
    return _expand(d["library_root_default"])


def persist_dir() -> Path:
    d = load_registry()["defaults"]
    env = os.environ.get(d["db_path_env"])
    if env:
        return _expand(env)
    return (library_root() / ".rag_db").resolve()


def track_file() -> Path:
    return persist_dir() / "embedded.json"


def ingest_lock_file() -> Path:
    return persist_dir() / "ingest.lock"


def embed_model() -> str:
    d = load_registry()["defaults"]
    return os.environ.get(d["embed_model_env"], d["embed_model_default"])


def llm_model() -> str:
    """Fast default answer model (overridable via RAG_LLM_MODEL)."""
    d = load_registry()["defaults"]
    return os.environ.get(d["llm_model_env"], d["llm_model_default"])


def llm_fallback_model() -> str:
    """Optional heavier synthesis model (RAG_LLM_FALLBACK_MODEL)."""
    d = load_registry()["defaults"]
    env_key = d.get("llm_fallback_model_env", "RAG_LLM_FALLBACK_MODEL")
    default = d.get("llm_fallback_model_default", "qwen3.5:9b")
    return os.environ.get(env_key, default)


def heavy_fallback_enabled_by_default() -> bool:
    """Explicit opt-in for the heavy fallback model without passing --fallback
    on every call. Off unless RAG_ENABLE_HEAVY_FALLBACK (or the configured
    env key) is set to a truthy value. This is the only way besides CLI
    --fallback to enable it — it is never enabled implicitly by a failed
    generation."""
    d = load_registry()["defaults"]
    env_key = d.get("enable_heavy_fallback_env", "RAG_ENABLE_HEAVY_FALLBACK")
    raw = os.environ.get(env_key, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def llm_num_ctx() -> int | None:
    """Context window size for generation; None leaves Ollama default."""
    d = load_registry()["defaults"]
    env_key = d.get("llm_num_ctx_env", "RAG_LLM_NUM_CTX")
    raw = os.environ.get(env_key)
    if raw is not None and str(raw).strip() != "":
        return int(raw)
    val = d.get("llm_num_ctx_default")
    return int(val) if val is not None else None


def llm_num_predict() -> int | None:
    """Max output tokens for generation; None leaves Ollama default."""
    d = load_registry()["defaults"]
    env_key = d.get("llm_num_predict_env", "RAG_LLM_NUM_PREDICT")
    raw = os.environ.get(env_key)
    if raw is not None and str(raw).strip() != "":
        return int(raw)
    val = d.get("llm_num_predict_default")
    return int(val) if val is not None else None


def chunk_size() -> int:
    return int(load_registry()["defaults"]["chunk_size"])


def chunk_overlap() -> int:
    return int(load_registry()["defaults"]["chunk_overlap"])


def default_k() -> int:
    return int(load_registry()["defaults"]["default_k"])


def known_scopes() -> tuple[str, ...]:
    return tuple(load_registry()["scopes"].keys())


def hermes_aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    for scope, meta in load_registry()["scopes"].items():
        for alias in meta.get("hermes_aliases") or []:
            out[alias.lower()] = scope
    return out


def list_scopes() -> list[dict[str, Any]]:
    rows = []
    for name, meta in load_registry()["scopes"].items():
        rows.append(
            {
                "name": name,
                "description": meta.get("description", ""),
                "hermes_aliases": list(meta.get("hermes_aliases") or []),
            }
        )
    return rows


def resolve_scope(name: str | None) -> str | None:
    if not name:
        return None
    raw = name.strip()
    key = raw.lower().replace("-", "_")
    scopes = known_scopes()
    if raw in scopes:
        return raw
    aliases = hermes_aliases()
    if key in aliases:
        return aliases[key]
    for scope in scopes:
        if scope.replace("-", "_") == key:
            return scope
    raise ValueError(
        f"Unknown scope {name!r}. Use `rag-engine list-scopes` or one of {scopes}."
    )


def collection_from_relpath(rel: str) -> str:
    """Map a relative source path to a collection using scopes.yaml."""
    from rag_engine.scope_rules import explain_path_assignment

    return explain_path_assignment(rel)["scope"]


def wiki_extensions() -> set[str]:
    meta = load_registry()["scopes"].get("wiki") or {}
    return {e.lower() for e in (meta.get("include_extensions") or [".md"])}


def should_skip_dir(dirpath: str) -> bool:
    path = dirpath.replace("\\", "/")
    return any(part in path for part in SKIP_DIR_PARTS)

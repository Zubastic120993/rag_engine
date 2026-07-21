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

SKIP_DIR_PARTS = (
    ".rag_db",
    ".obsidian",
    "_Inbox",
    "_Backup",
    "/Graph",
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
    d = load_registry()["defaults"]
    return os.environ.get(d["llm_model_env"], d["llm_model_default"])


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
    norm = rel.replace("\\", "/")
    upper = norm.upper()
    reg = load_registry()
    scopes: dict[str, Any] = reg["scopes"]

    for hint_scope in ("me-c", "inspection", "regulatory", "maker-manuals"):
        meta = scopes.get(hint_scope) or {}
        for hint in meta.get("path_hints") or []:
            if hint.upper() in upper:
                return hint_scope

    for scope_name in reg.get("prefix_order") or list(scopes.keys()):
        meta = scopes.get(scope_name) or {}
        for prefix in meta.get("path_prefixes") or []:
            if norm.startswith(prefix):
                return scope_name

    return "other"


def wiki_extensions() -> set[str]:
    meta = load_registry()["scopes"].get("wiki") or {}
    return {e.lower() for e in (meta.get("include_extensions") or [".md"])}


def should_skip_dir(dirpath: str) -> bool:
    path = dirpath.replace("\\", "/")
    return any(part in path for part in SKIP_DIR_PARTS)

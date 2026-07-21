"""Shared paths and settings for the local scoped RAG."""

from __future__ import annotations

import os
from pathlib import Path

# Telemetry / Ollama
os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:11434")
os.environ.setdefault("LANGCHAIN_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
os.environ.setdefault("POSTHOG_DISABLED", "true")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_ROOT = Path(os.path.expanduser("~/CE_Library")).resolve()
DATA_ROOT = (PROJECT_ROOT / "data").resolve()
PERSIST_DIR = (LIBRARY_ROOT / "99_Rules" / ".rag_db").resolve()
TRACK_FILE = PERSIST_DIR / "embedded.json"

EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "mxbai-embed-large")
# Override with RAG_LLM_MODEL if you pull a different chat model into Ollama.
LLM_MODEL = os.environ.get("RAG_LLM_MODEL", "qwen3.5:9b")
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
DEFAULT_K = 5

# Directory name fragments skipped while walking the library
SKIP_DIR_PARTS = (
    ".rag_db",
    ".obsidian",
    "_Inbox",
    "_Backup",
    "/Graph",
)

# First-match prefix map: relative path under LIBRARY_ROOT → collection
# Order matters. Project data/ is handled separately as "me-c".
COLLECTION_MAP: list[tuple[str, str]] = [
    ("10_Company/", "sms"),
    ("90_CE_Wiki/", "wiki"),
    ("20_Vessels/", "vessels"),
    ("00_Career/", "career"),
    ("99_Rules/", "rules"),
]

KNOWN_SCOPES = (
    "me-c",
    "sms",
    "wiki",
    "vessels",
    "career",
    "rules",
    "other",
)

ME_C_HINTS = ("ME-C", "G50ME", "ME-C9", "MEC")


def collection_from_relpath(rel: str, *, from_project_data: bool = False) -> str:
    """Map a relative source path to a collection scope name."""
    if from_project_data:
        return "me-c"

    norm = rel.replace("\\", "/")
    upper = norm.upper()
    for hint in ME_C_HINTS:
        if hint.upper() in upper:
            return "me-c"

    for prefix, name in COLLECTION_MAP:
        if norm.startswith(prefix) or f"/{prefix}" in f"/{norm}":
            return name

    return "other"


def should_skip_dir(dirpath: str) -> bool:
    path = dirpath.replace("\\", "/")
    return any(part in path for part in SKIP_DIR_PARTS)

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

# Sibling of content folders — not under 99_Rules/ (rebuild, do not relocate).
PERSIST_DIR = (LIBRARY_ROOT / ".rag_db").resolve()
TRACK_FILE = PERSIST_DIR / "embedded.json"

EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "mxbai-embed-large")
# Override with RAG_LLM_MODEL if you pull a different chat model into Ollama.
LLM_MODEL = os.environ.get("RAG_LLM_MODEL", "qwen3.5:9b")

# Explicit decision for this week's free reindex: keep 800/100 unless eval says otherwise.
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
DEFAULT_K = 5

# Directory name fragments skipped while walking the library.
# Keep ".rag_db" even though the store sits at LIBRARY_ROOT/.rag_db — insurance
# if content is later reorganised above the DB.
SKIP_DIR_PARTS = (
    ".rag_db",
    ".obsidian",
    "_Inbox",
    "_Backup",
    "/Graph",
)

# Hermes-aligned scopes (see 90_CE_Wiki/00_Hermes_RAG_Routing_Guide.md).
# Path prefixes — first match wins (most specific first).
COLLECTION_MAP: list[tuple[str, str]] = [
    ("90_CE_Wiki/", "wiki"),
    ("10_Company/", "sms"),
    ("00_Career/02_Statutory/SIRE_OCIMF/", "inspection"),
    ("00_Career/02_Statutory/", "regulatory"),
    ("00_Career/01_Class_Rules/", "regulatory"),
    ("00_Career/03_Engine_Knowledge/", "maker-manuals"),
    ("00_Career/07_SDS_Datasheets/", "maker-manuals"),
    ("00_Career/", "career"),
    ("20_Vessels/", "vessels"),
    ("99_Rules/", "rules"),
]

KNOWN_SCOPES = (
    "me-c",
    "sms",
    "wiki",
    "maker-manuals",
    "regulatory",
    "inspection",
    "vessels",
    "career",
    "rules",
    "other",
)

# Aliases Hermes routing guide names → our collection scopes
HERMES_SCOPE_ALIASES: dict[str, str] = {
    "sms_library": "sms",
    "imo_library": "regulatory",
    "sire_library": "inspection",
    "manual_library": "maker-manuals",
    "manual_library_gaschem_europe": "vessels",
    "manual_library_gaschem_africa": "vessels",
    "ce_wiki": "wiki",
    "wiki_library": "wiki",
    "maker_manual": "maker-manuals",
    "statutory": "regulatory",
}


def resolve_scope(name: str | None) -> str | None:
    """Normalize a scope or Hermes library alias to a collection name."""
    if not name:
        return None
    key = name.strip().lower().replace("-", "_")
    if name in KNOWN_SCOPES:
        return name
    if key in HERMES_SCOPE_ALIASES:
        return HERMES_SCOPE_ALIASES[key]
    # allow underscore form of known scopes
    for scope in KNOWN_SCOPES:
        if scope.replace("-", "_") == key:
            return scope
    raise ValueError(
        f"Unknown scope {name!r}. Use one of {KNOWN_SCOPES} or Hermes aliases "
        f"{tuple(HERMES_SCOPE_ALIASES)}"
    )

# Substring hints checked after prefix map (path uppercased).
INSPECTION_HINTS = ("SIRE", "OCIMF", "CDI", "/VIQ")
REGULATORY_HINTS = (
    "MARPOL",
    "SOLAS",
    "/IMO/",
    "MEPC",
    "MSC.1",
    "CLASSNK",
    "FLAG_STATE",
)
ME_C_HINTS = ("ME-C", "G50ME", "ME-C9", "MEC")
MAKER_HINTS = ("/MANUAL", "/MANUALS/", "INSTRUCTION_MANUAL", "OPERATION_MANUAL")


def collection_from_relpath(rel: str, *, from_project_data: bool = False) -> str:
    """Map a relative source path to a Hermes-aligned collection scope."""
    if from_project_data:
        return "me-c"

    norm = rel.replace("\\", "/")
    upper = norm.upper()

    for hint in ME_C_HINTS:
        if hint.upper() in upper:
            return "me-c"

    for hint in INSPECTION_HINTS:
        if hint in upper:
            return "inspection"

    for hint in REGULATORY_HINTS:
        if hint in upper:
            return "regulatory"

    for prefix, name in COLLECTION_MAP:
        if norm.startswith(prefix):
            return name

    for hint in MAKER_HINTS:
        if hint in upper:
            return "maker-manuals"

    return "other"


def should_skip_dir(dirpath: str) -> bool:
    path = dirpath.replace("\\", "/")
    return any(part in path for part in SKIP_DIR_PARTS)

"""Path helpers for the metadata registry (no DB open on import).

Approved production location (docs/RAG_METADATA_REGISTRY_LOCATION_POLICY_V1.md):
  <LIBRARY_ROOT>/.rag_state/metadata_registry/metadata_registry_v1.sqlite3

Computing this path must not create directories or open SQLite.
"""

from __future__ import annotations

from pathlib import Path

APPROVED_PRODUCTION_FILENAME = "metadata_registry_v1.sqlite3"


def production_registry_dir(library_root: str | Path) -> Path:
    """Return the approved registry directory under ``library_root`` (not created)."""
    root = Path(library_root)
    return root / ".rag_state" / "metadata_registry"


def production_registry_path(library_root: str | Path) -> Path:
    """Return the approved production DB path (not created / not opened)."""
    return production_registry_dir(library_root) / APPROVED_PRODUCTION_FILENAME

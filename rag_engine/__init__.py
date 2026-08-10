"""rag-engine — local scoped RAG tool over a client document library."""

from __future__ import annotations

import sys
from pathlib import Path


def _scrub_foreign_site_packages() -> None:
    """Keep the active interpreter's site-packages, drop leaked foreign ones.

    Hermes desktop sessions can inject another venv's site-packages via
    PYTHONPATH/VIRTUAL_ENV leakage. When rag-engine is executed with its own
    interpreter, those foreign entries can shadow this venv's binary wheels
    (notably pydantic/pydantic-core) and break lightweight CLI commands.
    """

    prefix = Path(sys.prefix).resolve()
    cleaned: list[str] = []
    for entry in sys.path:
        if not entry:
            cleaned.append(entry)
            continue
        try:
            resolved = Path(entry).resolve()
        except OSError:
            cleaned.append(entry)
            continue
        if "site-packages" in resolved.parts and prefix not in resolved.parents:
            continue
        cleaned.append(entry)
    sys.path[:] = cleaned


_scrub_foreign_site_packages()

__all__ = ["answer"]


def __getattr__(name: str):
    if name == "answer":
        from rag_engine.query import answer as _answer

        return _answer
    raise AttributeError(name)

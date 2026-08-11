"""Bounded source-file observation — hash only when needed; never mutate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rag_engine.reconciliation.models import SourceObservation
from rag_engine.stable_identity import (
    PathNormalizationError,
    normalize_relative_path,
    source_hash_from_file,
)


class SourceHashCache:
    """In-memory hash cache for one reconciliation run (never persisted)."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self.hits = 0
        self.misses = 0

    def get_or_hash(self, absolute_path: Path) -> str:
        key = str(absolute_path)
        if key in self._cache:
            self.hits += 1
            return self._cache[key]
        self.misses += 1
        digest = source_hash_from_file(absolute_path)
        self._cache[key] = digest
        return digest


def observe_source_path(
    relative_or_abs: str,
    *,
    library_root: str | Path,
    hash_if_exists: bool = True,
    cache: SourceHashCache | None = None,
) -> SourceObservation:
    """Observe a single source path under library_root (read-only)."""
    root = Path(library_root).resolve()
    try:
        norm = normalize_relative_path(relative_or_abs, library_root=root)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        # Still try existence via raw join for reporting
        candidate = root / str(relative_or_abs).lstrip("/")
        return SourceObservation(
            path=str(relative_or_abs),
            exists=candidate.is_file(),
            source_hash=None,
            normalized_relative_path=None,
            error=f"path_normalization: {exc}",
        )

    abs_path = root / norm
    exists = abs_path.is_file()
    digest: str | None = None
    err: str | None = None
    if exists and hash_if_exists:
        try:
            if cache is not None:
                digest = cache.get_or_hash(abs_path)
            else:
                digest = source_hash_from_file(abs_path)
        except OSError as exc:
            err = f"hash_error: {exc}"
    return SourceObservation(
        path=str(relative_or_abs),
        exists=exists,
        source_hash=digest,
        normalized_relative_path=norm,
        error=err,
    )


def observe_paths(
    paths: list[str] | tuple[str, ...],
    *,
    library_root: str | Path,
    hash_if_exists: bool = True,
    cache: SourceHashCache | None = None,
) -> list[SourceObservation]:
    """Observe multiple paths; hashes cached for the run."""
    cache = cache or SourceHashCache()
    return [
        observe_source_path(
            p,
            library_root=library_root,
            hash_if_exists=hash_if_exists,
            cache=cache,
        )
        for p in paths
    ]


def normalize_locator(path: str, *, library_root: str | Path | None = None) -> str | None:
    """Best-effort locator normalization; returns None on failure."""
    try:
        return normalize_relative_path(path, library_root=library_root)
    except Exception:
        try:
            return normalize_relative_path(path, library_root=None)
        except Exception:
            return None

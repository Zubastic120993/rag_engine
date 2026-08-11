"""Path normalization for registry locators (Spec §9). Not used as identity."""

from __future__ import annotations

import unicodedata
from pathlib import Path, PurePosixPath


class PathNormalizationError(ValueError):
    """Raised when a path cannot be safely normalized as a relative locator."""


def normalize_relative_path(
    path: str | Path,
    *,
    library_root: str | Path | None = None,
) -> str:
    """Canonical relative path for registry locator keys (Spec §9.1).

    Rules:
    - if ``library_root`` given, resolve and require path is under root
    - store as relative posix path with ``/`` separators
    - strip leading ``./``
    - reject ``..`` escape
    - Unicode NFC for registry keys (not NFKC)
    - preserve path segment case

    Paths are **locators only** — never ``document_id`` / sole ``subject_id``.
    """
    if isinstance(path, Path):
        raw = str(path)
    else:
        if not isinstance(path, str):
            raise TypeError("path must be str or Path")
        raw = path

    raw = raw.replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]

    if library_root is not None:
        root = Path(library_root).resolve()
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
            rel = resolved.relative_to(root)
        except (ValueError, OSError) as exc:
            raise PathNormalizationError(
                "path must resolve under library_root without escape"
            ) from exc
        raw = rel.as_posix()

    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise PathNormalizationError(
            "relative_path must be relative when library_root is not used to relativize"
        )
    if ".." in pure.parts:
        raise PathNormalizationError("relative_path must not contain '..'")

    normalized = "/".join(pure.parts)
    if normalized in ("", "."):
        raise PathNormalizationError("relative_path must be non-empty")

    # Spec §9.1: NFC for registry keys; do not NFKC-normalize paths.
    return unicodedata.normalize("NFC", normalized)

"""Typed failures for certified new-generation rebuilds (B5I)."""

from __future__ import annotations

from typing import Any


class CertifiedGenerationError(Exception):
    """Base class for certified-generation failures."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ExplicitTargetRequiredError(CertifiedGenerationError):
    """Certified rebuild omitted --persist-dir / persist_dir argument."""


class LegacyPathForbiddenError(CertifiedGenerationError):
    """Certified rebuild targeted current production .rag_db or equivalent."""


class UnsafeGenerationPathError(CertifiedGenerationError):
    """Target path collides with corpus, intake, registry, or production roots."""


class EmptyGenerationGuardError(CertifiedGenerationError):
    """Target is not a safe empty/new certified generation directory."""


class GenerationIdError(CertifiedGenerationError):
    """Generation ID format or binding is invalid."""


class CorpusManifestError(CertifiedGenerationError):
    """Frozen corpus manifest missing, drifted, or malformed."""


class CollisionGuardError(CertifiedGenerationError):
    """Stable ID / vector ID / metadata collision."""


class DimensionGuardError(CertifiedGenerationError):
    """Embedding dimension does not match v1 / collection contract."""


class ModelGuardError(CertifiedGenerationError):
    """Embedding provider/model changed mid-generation."""


class PartialBuildError(CertifiedGenerationError):
    """Unsafe resume of an unknown or mismatched partial generation."""


class CheckpointError(CertifiedGenerationError):
    """Checkpoint missing, corrupt, or illegally marked accepted."""

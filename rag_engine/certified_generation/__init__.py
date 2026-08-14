"""Certified new-generation rebuild API (B5I).

Distinct from legacy ingest. Import does not create production paths,
write v1, or open Chroma.
"""

from __future__ import annotations

from rag_engine.certified_generation.checkpoints import (
    STATE_BUILDING,
    STATE_BUILT_UNVALIDATED,
    STATE_PREPARED,
    read_checkpoint,
)
from rag_engine.certified_generation.contracts import (
    certified_chunking_contract,
    certified_chunking_fingerprint,
    certified_v1_envelope,
)
from rag_engine.certified_generation.corpus import (
    build_manifest_from_paths,
    dedup_by_document_id,
    load_corpus_manifest,
    verify_manifest_against_disk,
)
from rag_engine.certified_generation.exceptions import (
    CertifiedGenerationError,
    CollisionGuardError,
    CorpusManifestError,
    DimensionGuardError,
    EmptyGenerationGuardError,
    ExplicitTargetRequiredError,
    GenerationIdError,
    LegacyPathForbiddenError,
    ModelGuardError,
    PartialBuildError,
    UnsafeGenerationPathError,
)
from rag_engine.certified_generation.ids import (
    dirname_to_generation_id,
    generation_id_to_dirname,
    make_generation_id,
    parse_generation_id,
)
from rag_engine.certified_generation.inspect import compare_generations, inspect_generation
from rag_engine.certified_generation.metadata import (
    REQUIRED_VECTOR_FIELDS,
    certified_chunk_id,
    certified_vector_metadata,
)
from rag_engine.certified_generation.paths import (
    PRODUCTION_RAG_DB,
    assert_not_legacy_production,
    assert_safe_generation_path,
    is_legacy_production_persist,
    require_explicit_persist_dir,
)
from rag_engine.certified_generation.v1 import initialize_certified_v1

__all__ = [
    "CertifiedGenerationError",
    "CollisionGuardError",
    "CorpusManifestError",
    "DimensionGuardError",
    "EmptyGenerationGuardError",
    "ExplicitTargetRequiredError",
    "GenerationIdError",
    "LegacyPathForbiddenError",
    "ModelGuardError",
    "PRODUCTION_RAG_DB",
    "PartialBuildError",
    "REQUIRED_VECTOR_FIELDS",
    "STATE_BUILDING",
    "STATE_BUILT_UNVALIDATED",
    "STATE_PREPARED",
    "UnsafeGenerationPathError",
    "assert_not_legacy_production",
    "assert_safe_generation_path",
    "build_certified_generation",
    "build_manifest_from_paths",
    "certified_chunk_id",
    "certified_chunking_contract",
    "certified_chunking_fingerprint",
    "certified_v1_envelope",
    "certified_vector_metadata",
    "compare_generations",
    "dedup_by_document_id",
    "dirname_to_generation_id",
    "generation_id_to_dirname",
    "init_certified_generation",
    "initialize_certified_v1",
    "inspect_generation",
    "is_legacy_production_persist",
    "load_corpus_manifest",
    "make_generation_id",
    "parse_generation_id",
    "read_checkpoint",
    "require_explicit_persist_dir",
    "verify_manifest_against_disk",
]


def init_certified_generation(*args, **kwargs):
    from rag_engine.certified_generation.build import init_certified_generation as _init

    return _init(*args, **kwargs)


def build_certified_generation(*args, **kwargs):
    from rag_engine.certified_generation.build import build_certified_generation as _build

    return _build(*args, **kwargs)

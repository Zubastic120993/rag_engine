"""Compatibility classification for embedding-fp-v1."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rag_engine.index_compatibility.builders import (
    build_runtime_contracts_from_config,
    stored_envelope_from_specs,
)
from rag_engine.index_compatibility.chroma_inspect import count_vectors_readonly
from rag_engine.index_compatibility.constants import (
    COMPAT_CONFIGURATION_ERROR,
    COMPAT_CONFLICT,
    COMPAT_CORRUPT,
    COMPAT_EMPTY_UNINITIALIZED,
    COMPAT_KNOWN_COMPATIBLE,
    COMPAT_KNOWN_INCOMPATIBLE,
    COMPAT_UNKNOWN_LEGACY,
    COMPAT_UNSUPPORTED_SCHEMA,
    DEFAULT_PHYSICAL_COLLECTION,
)
from rag_engine.index_compatibility.exceptions import (
    FingerprintConfigurationError,
    FingerprintConflictError,
    FingerprintCorruptError,
    FingerprintUnsupportedVersionError,
)
from rag_engine.index_compatibility.specs import (
    CorpusFingerprintSpec,
    EmbeddingFingerprintSpec,
    IndexFingerprintSpec,
    StoredIndexFingerprint,
)
from rag_engine.index_compatibility.state import load_authoritative_state


@dataclass(frozen=True)
class CompatibilityResult:
    state: str
    reason: str
    runtime_index_fingerprint: str | None = None
    stored_index_fingerprint: str | None = None
    runtime_embedding_fingerprint: str | None = None
    stored_embedding_fingerprint: str | None = None
    runtime_corpus_fingerprint: str | None = None
    stored_corpus_fingerprint: str | None = None
    vector_count: int = 0
    authority_source: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ingest_allowed(self) -> bool:
        from rag_engine.index_compatibility.constants import INGEST_ALLOWED_STATES

        return self.state in INGEST_ALLOWED_STATES

    @property
    def retrieval_allowed(self) -> bool:
        from rag_engine.index_compatibility.constants import RETRIEVAL_ALLOWED_STATES

        return self.state in RETRIEVAL_ALLOWED_STATES

    @property
    def retrieval_degraded(self) -> bool:
        from rag_engine.index_compatibility.constants import RETRIEVAL_DEGRADED_STATES

        return self.state in RETRIEVAL_DEGRADED_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "runtime_index_fingerprint": self.runtime_index_fingerprint,
            "stored_index_fingerprint": self.stored_index_fingerprint,
            "runtime_embedding_fingerprint": self.runtime_embedding_fingerprint,
            "stored_embedding_fingerprint": self.stored_embedding_fingerprint,
            "runtime_corpus_fingerprint": self.runtime_corpus_fingerprint,
            "stored_corpus_fingerprint": self.stored_corpus_fingerprint,
            "vector_count": self.vector_count,
            "authority_source": self.authority_source,
            "ingest_allowed": self.ingest_allowed,
            "retrieval_allowed": self.retrieval_allowed,
            "retrieval_degraded": self.retrieval_degraded,
            "evidence": dict(self.evidence),
        }


def evaluate_compatibility(
    persist: str | Path,
    *,
    registry_db: str | Path | None = None,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
    vector_count: int | None = None,
    runtime_embedding: EmbeddingFingerprintSpec | None = None,
    runtime_corpus: CorpusFingerprintSpec | None = None,
    runtime_index: IndexFingerprintSpec | None = None,
) -> CompatibilityResult:
    """Classify index compatibility. Read-only — never writes fingerprint state."""
    persist_path = Path(persist)
    if vector_count is None:
        vector_count = count_vectors_readonly(
            persist_path, physical_collection_name=physical_collection_name
        )

    runtime_error: str | None = None
    try:
        if runtime_embedding is None or runtime_corpus is None or runtime_index is None:
            emb, corp, idx = build_runtime_contracts_from_config(
                physical_collection_name=physical_collection_name,
            )
            runtime_embedding = runtime_embedding or emb
            runtime_corpus = runtime_corpus or corp
            runtime_index = runtime_index or idx
        assert runtime_embedding is not None
        assert runtime_corpus is not None
        assert runtime_index is not None
        runtime_ifp = runtime_index.digest()
        runtime_efp = runtime_embedding.digest()
        runtime_cfp = runtime_corpus.digest()
    except FingerprintConfigurationError as exc:
        runtime_error = str(exc)
        runtime_ifp = runtime_efp = runtime_cfp = None
        runtime_embedding = runtime_corpus = runtime_index = None

    stored: StoredIndexFingerprint | None = None
    try:
        stored = load_authoritative_state(
            persist_path,
            registry_db=registry_db,
            physical_collection_name=physical_collection_name,
        )
    except FingerprintConflictError as exc:
        return CompatibilityResult(
            state=COMPAT_CONFLICT,
            reason=str(exc),
            runtime_index_fingerprint=runtime_ifp,
            runtime_embedding_fingerprint=runtime_efp,
            runtime_corpus_fingerprint=runtime_cfp,
            vector_count=vector_count,
            evidence={"details": exc.details},
        )
    except FingerprintUnsupportedVersionError as exc:
        return CompatibilityResult(
            state=COMPAT_UNSUPPORTED_SCHEMA,
            reason=str(exc),
            runtime_index_fingerprint=runtime_ifp,
            runtime_embedding_fingerprint=runtime_efp,
            runtime_corpus_fingerprint=runtime_cfp,
            vector_count=vector_count,
            evidence={"details": exc.details},
        )
    except FingerprintCorruptError as exc:
        return CompatibilityResult(
            state=COMPAT_CORRUPT,
            reason=str(exc),
            runtime_index_fingerprint=runtime_ifp,
            runtime_embedding_fingerprint=runtime_efp,
            runtime_corpus_fingerprint=runtime_cfp,
            vector_count=vector_count,
            evidence={"details": exc.details},
        )

    if runtime_error is not None:
        return CompatibilityResult(
            state=COMPAT_CONFIGURATION_ERROR,
            reason=runtime_error,
            stored_index_fingerprint=stored.index_fingerprint if stored else None,
            stored_embedding_fingerprint=stored.embedding_fingerprint if stored else None,
            stored_corpus_fingerprint=stored.corpus_fingerprint if stored else None,
            vector_count=vector_count,
            authority_source=stored.source if stored else None,
        )

    assert runtime_ifp is not None and runtime_embedding is not None
    assert runtime_corpus is not None and runtime_index is not None

    if stored is None:
        if vector_count > 0:
            return CompatibilityResult(
                state=COMPAT_UNKNOWN_LEGACY,
                reason=(
                    "vectors exist but no trustworthy embedding-fp-v1 authority; "
                    "sidecar v0 / live config cannot certify historical vectors"
                ),
                runtime_index_fingerprint=runtime_ifp,
                runtime_embedding_fingerprint=runtime_efp,
                runtime_corpus_fingerprint=runtime_cfp,
                vector_count=vector_count,
                evidence={"sidecar_v0_is_not_authority": True},
            )
        return CompatibilityResult(
            state=COMPAT_EMPTY_UNINITIALIZED,
            reason="empty collection with no fingerprint authority; may initialize",
            runtime_index_fingerprint=runtime_ifp,
            runtime_embedding_fingerprint=runtime_efp,
            runtime_corpus_fingerprint=runtime_cfp,
            vector_count=0,
            evidence={
                "proposed_envelope": stored_envelope_from_specs(
                    runtime_embedding, runtime_corpus, runtime_index
                )
            },
        )

    if stored.index_fingerprint == runtime_ifp:
        return CompatibilityResult(
            state=COMPAT_KNOWN_COMPATIBLE,
            reason="stored index fingerprint matches runtime contract",
            runtime_index_fingerprint=runtime_ifp,
            stored_index_fingerprint=stored.index_fingerprint,
            runtime_embedding_fingerprint=runtime_efp,
            stored_embedding_fingerprint=stored.embedding_fingerprint,
            runtime_corpus_fingerprint=runtime_cfp,
            stored_corpus_fingerprint=stored.corpus_fingerprint,
            vector_count=vector_count,
            authority_source=stored.source,
        )

    mismatch_parts: list[str] = []
    if stored.embedding_fingerprint != runtime_efp:
        mismatch_parts.append("embedding_fingerprint")
    if stored.corpus_fingerprint != runtime_cfp:
        mismatch_parts.append("corpus_fingerprint")
    if stored.index_fingerprint != runtime_ifp:
        mismatch_parts.append("index_fingerprint")

    return CompatibilityResult(
        state=COMPAT_KNOWN_INCOMPATIBLE,
        reason=f"runtime/stored fingerprint mismatch: {', '.join(mismatch_parts)}",
        runtime_index_fingerprint=runtime_ifp,
        stored_index_fingerprint=stored.index_fingerprint,
        runtime_embedding_fingerprint=runtime_efp,
        stored_embedding_fingerprint=stored.embedding_fingerprint,
        runtime_corpus_fingerprint=runtime_cfp,
        stored_corpus_fingerprint=stored.corpus_fingerprint,
        vector_count=vector_count,
        authority_source=stored.source,
        evidence={"mismatch_fields": mismatch_parts},
    )

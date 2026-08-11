"""Build runtime embedding / corpus / index fingerprint specs from config."""

from __future__ import annotations

import os
from typing import Any

from rag_engine.index_compatibility.constants import (
    DEFAULT_DISTANCE_SPACE,
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODE,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_PHYSICAL_COLLECTION,
    DEFAULT_VECTOR_STORE,
    EMBEDDED_TEXT_COMPOSITION_VERSION,
    FINGERPRINT_SCHEMA_VERSION,
)
from rag_engine.index_compatibility.exceptions import FingerprintConfigurationError
from rag_engine.index_compatibility.specs import (
    CorpusFingerprintSpec,
    EmbeddingFingerprintSpec,
    IndexFingerprintSpec,
)
from rag_engine.stable_identity.constants import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNKING_SEPARATORS,
    DEFAULT_EXTRACTOR,
    DEFAULT_EXTRACTOR_VERSION,
    DEFAULT_MAX_CHUNK_CHARS,
    DEFAULT_MIN_CHUNK_CHARS,
    DEFAULT_NORMALIZATION,
    IDENTITY_SCHEME_VERSION,
)


def embedding_dimension_from_env(*, default: int = DEFAULT_EMBEDDING_DIMENSION) -> int:
    raw = os.environ.get("RAG_EMBED_DIMENSION")
    if raw is None or raw.strip() == "":
        return int(default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise FingerprintConfigurationError(
            f"RAG_EMBED_DIMENSION must be an integer, got {raw!r}"
        ) from exc
    if value <= 0:
        raise FingerprintConfigurationError("RAG_EMBED_DIMENSION must be positive")
    return value


def build_embedding_spec(
    *,
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER,
    embedding_model: str,
    embedding_model_revision: str | None = None,
    embedding_dimension: int | None = None,
    embedding_normalization: str | None = None,
    embedding_mode: str = DEFAULT_EMBEDDING_MODE,
    tokenizer_id: str | None = None,
    max_input_tokens: int | None = None,
) -> EmbeddingFingerprintSpec:
    if not isinstance(embedding_model, str) or not embedding_model.strip():
        raise FingerprintConfigurationError("embedding_model must be a non-empty string")
    dim = (
        int(embedding_dimension)
        if embedding_dimension is not None
        else embedding_dimension_from_env()
    )
    if dim <= 0:
        raise FingerprintConfigurationError("embedding_dimension must be positive")
    return EmbeddingFingerprintSpec(
        fingerprint_schema_version=FINGERPRINT_SCHEMA_VERSION,
        embedding_provider=str(embedding_provider),
        embedding_model=str(embedding_model),
        embedding_model_revision=embedding_model_revision,
        embedding_dimension=dim,
        embedding_normalization=embedding_normalization,
        embedding_mode=str(embedding_mode),
        tokenizer_id=tokenizer_id,
        max_input_tokens=max_input_tokens,
    )


def build_corpus_spec(
    *,
    identity_scheme_version: str = IDENTITY_SCHEME_VERSION,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    separators: tuple[str, ...] | list[str] = DEFAULT_CHUNKING_SEPARATORS,
    normalization: str = DEFAULT_NORMALIZATION,
    min_chunk_chars: int = DEFAULT_MIN_CHUNK_CHARS,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    extractor: str = DEFAULT_EXTRACTOR,
    extractor_version: str = DEFAULT_EXTRACTOR_VERSION,
    embedded_text_composition_version: str = EMBEDDED_TEXT_COMPOSITION_VERSION,
) -> CorpusFingerprintSpec:
    return CorpusFingerprintSpec(
        identity_scheme_version=str(identity_scheme_version),
        chunk_size=int(chunk_size),
        chunk_overlap=int(chunk_overlap),
        separators=tuple(separators),
        normalization=str(normalization),
        min_chunk_chars=int(min_chunk_chars),
        max_chunk_chars=int(max_chunk_chars),
        extractor=str(extractor),
        extractor_version=str(extractor_version),
        embedded_text_composition_version=str(embedded_text_composition_version),
    )


def build_index_spec(
    *,
    embedding: EmbeddingFingerprintSpec,
    corpus: CorpusFingerprintSpec,
    vector_store: str = DEFAULT_VECTOR_STORE,
    distance_space: str = DEFAULT_DISTANCE_SPACE,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
    index_schema_notes: str | None = None,
) -> IndexFingerprintSpec:
    return IndexFingerprintSpec(
        fingerprint_schema_version=FINGERPRINT_SCHEMA_VERSION,
        embedding_fingerprint=embedding.digest(),
        corpus_fingerprint=corpus.digest(),
        vector_store=str(vector_store),
        distance_space=str(distance_space),
        physical_collection_name=str(physical_collection_name),
        index_schema_notes=index_schema_notes,
    )


def build_runtime_contracts_from_config(
    *,
    embedding_model: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    embedding_dimension: int | None = None,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
    distance_space: str = DEFAULT_DISTANCE_SPACE,
    extractor: str = DEFAULT_EXTRACTOR,
    extractor_version: str = DEFAULT_EXTRACTOR_VERSION,
) -> tuple[EmbeddingFingerprintSpec, CorpusFingerprintSpec, IndexFingerprintSpec]:
    """Build runtime contracts using live rag_engine.config values when omitted."""
    try:
        from rag_engine.config import (
            chunk_overlap as cfg_overlap,
            chunk_size as cfg_size,
            embed_model,
        )
    except Exception as exc:  # noqa: BLE001
        raise FingerprintConfigurationError(
            f"cannot load runtime config for fingerprint: {exc}"
        ) from exc

    try:
        model = embedding_model if embedding_model is not None else embed_model()
        size = int(chunk_size if chunk_size is not None else cfg_size())
        overlap = int(chunk_overlap if chunk_overlap is not None else cfg_overlap())
    except Exception as exc:  # noqa: BLE001
        raise FingerprintConfigurationError(
            f"cannot resolve embedding/chunk config: {exc}"
        ) from exc

    embedding = build_embedding_spec(
        embedding_model=model,
        embedding_dimension=embedding_dimension,
    )
    corpus = build_corpus_spec(
        chunk_size=size,
        chunk_overlap=overlap,
        extractor=extractor,
        extractor_version=extractor_version,
    )
    index = build_index_spec(
        embedding=embedding,
        corpus=corpus,
        physical_collection_name=physical_collection_name,
        distance_space=distance_space,
    )
    return embedding, corpus, index


def stored_envelope_from_specs(
    embedding: EmbeddingFingerprintSpec,
    corpus: CorpusFingerprintSpec,
    index: IndexFingerprintSpec,
) -> dict[str, Any]:
    return {
        "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
        "physical_collection_name": index.physical_collection_name,
        "index_fingerprint": index.digest(),
        "embedding_fingerprint": embedding.digest(),
        "corpus_fingerprint": corpus.digest(),
        "embedding_contract": embedding.to_contract(),
        "corpus_contract": corpus.to_contract(),
        "index_contract": index.to_contract(),
    }

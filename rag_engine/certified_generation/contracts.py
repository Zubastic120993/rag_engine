"""Chunking fingerprint + embedding contract freeze for NEW generations."""

from __future__ import annotations

from typing import Any

from rag_engine.index_compatibility.builders import (
    build_corpus_spec,
    build_embedding_spec,
    build_index_spec,
    stored_envelope_from_specs,
)
from rag_engine.index_compatibility.constants import (
    DEFAULT_DISTANCE_SPACE,
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODE,
    DEFAULT_EMBEDDING_PROVIDER,
    DEFAULT_PHYSICAL_COLLECTION,
    EMBEDDED_TEXT_COMPOSITION_VERSION,
)
from rag_engine.stable_identity.canonical import (
    chunking_fingerprint,
    default_chunking_contract,
)
from rag_engine.stable_identity.constants import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNKING_SEPARATORS,
    DEFAULT_MAX_CHUNK_CHARS,
    DEFAULT_MIN_CHUNK_CHARS,
    DEFAULT_NORMALIZATION,
    IDENTITY_SCHEME_VERSION,
)

CERTIFIED_EXTRACTOR = "ingest._load_documents"
CERTIFIED_EXTRACTOR_VERSION = "pypdf+textloader"


def certified_chunking_contract(
    *,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> dict[str, Any]:
    """Stable-id-v1 contract matching live ingest chunking (not legacy v0)."""
    from rag_engine.config import chunk_overlap as cfg_overlap, chunk_size as cfg_size

    size = int(chunk_size if chunk_size is not None else cfg_size())
    overlap = int(chunk_overlap if chunk_overlap is not None else cfg_overlap())
    return default_chunking_contract(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=DEFAULT_CHUNKING_SEPARATORS,
        normalization=DEFAULT_NORMALIZATION,
        min_chunk_chars=DEFAULT_MIN_CHUNK_CHARS,
        max_chunk_chars=DEFAULT_MAX_CHUNK_CHARS,
        extractor=CERTIFIED_EXTRACTOR,
        extractor_version=CERTIFIED_EXTRACTOR_VERSION,
        identity_scheme_version=IDENTITY_SCHEME_VERSION,
    )


def certified_chunking_fingerprint(
    *,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> str:
    return chunking_fingerprint(certified_chunking_contract(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    ))


def certified_embedding_spec(
    *,
    embedding_model: str | None = None,
    embedding_dimension: int | None = None,
    embedding_model_revision: str | None = None,
):
    from rag_engine.config import embed_model

    model = embedding_model if embedding_model is not None else embed_model()
    dim = (
        int(embedding_dimension)
        if embedding_dimension is not None
        else DEFAULT_EMBEDDING_DIMENSION
    )
    return build_embedding_spec(
        embedding_provider=DEFAULT_EMBEDDING_PROVIDER,
        embedding_model=model,
        embedding_model_revision=embedding_model_revision,  # honest null if unknown
        embedding_dimension=dim,
        embedding_normalization=None,
        embedding_mode=DEFAULT_EMBEDDING_MODE,
    )


def certified_corpus_spec(*, chunk_size: int | None = None, chunk_overlap: int | None = None):
    contract = certified_chunking_contract(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return build_corpus_spec(
        identity_scheme_version=str(contract["identity_scheme_version"]),
        chunk_size=int(contract["chunk_size"]),
        chunk_overlap=int(contract["chunk_overlap"]),
        separators=tuple(contract["separators"]),
        normalization=str(contract["normalization"]),
        min_chunk_chars=int(contract["min_chunk_chars"]),
        max_chunk_chars=int(contract["max_chunk_chars"]),
        extractor=str(contract["extractor"]),
        extractor_version=str(contract["extractor_version"]),
        embedded_text_composition_version=EMBEDDED_TEXT_COMPOSITION_VERSION,
    )


def certified_v1_envelope(
    *,
    embedding_model: str | None = None,
    embedding_dimension: int | None = None,
    embedding_model_revision: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
) -> dict[str, Any]:
    """v1 sidecar envelope matching live index_compatibility runtime contracts.

    extractor/version follow embedding-fp-v1 runtime defaults so a new
    generation evaluates as KNOWN_COMPATIBLE. Stable chunk IDs still use
    ``certified_chunking_fingerprint()`` (stable-id-v1), which is a separate
    digest and must not be confused with v1 ``corpus_fingerprint``.
    Historical model revision/digest is not invented (null unless provided).
    """
    from rag_engine.index_compatibility.builders import build_runtime_contracts_from_config

    emb, corp, idx = build_runtime_contracts_from_config(
        embedding_model=embedding_model,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        embedding_dimension=embedding_dimension,
        physical_collection_name=physical_collection_name,
        distance_space=DEFAULT_DISTANCE_SPACE,
    )
    envelope = stored_envelope_from_specs(emb, corp, idx)
    # Honesty: do not fabricate an Ollama digest. Override only if caller
    # actually obtained a revision; empty/None stays None.
    if embedding_model_revision:
        contract = dict(envelope["embedding_contract"])
        contract["embedding_model_revision"] = embedding_model_revision
        emb2 = build_embedding_spec(
            embedding_provider=contract["embedding_provider"],
            embedding_model=contract["embedding_model"],
            embedding_model_revision=embedding_model_revision,
            embedding_dimension=contract["embedding_dimension"],
            embedding_normalization=contract.get("embedding_normalization"),
            embedding_mode=contract["embedding_mode"],
            tokenizer_id=contract.get("tokenizer_id"),
            max_input_tokens=contract.get("max_input_tokens"),
        )
        idx2 = build_index_spec(
            embedding=emb2,
            corpus=corp,
            physical_collection_name=physical_collection_name,
            distance_space=DEFAULT_DISTANCE_SPACE,
        )
        envelope = stored_envelope_from_specs(emb2, corp, idx2)
    return envelope

"""Strong typed specs and canonicalization for embedding-fp-v1."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from rag_engine.index_compatibility.constants import (
    CORPUS_CONTRACT_FIELDS,
    EMBEDDING_CONTRACT_FIELDS,
    FINGERPRINT_SCHEMA_VERSION,
    INDEX_CONTRACT_FIELDS,
    SHA256_HEX_LEN,
)
from rag_engine.index_compatibility.exceptions import (
    FingerprintCorruptError,
    FingerprintUnsupportedVersionError,
)
from rag_engine.stable_identity.canonical import CanonicalizationError, _canonical_json_value
from rag_engine.stable_identity.hashing import sha256_utf8


def canonical_json(obj: Mapping[str, Any]) -> str:
    """Deterministic canonical JSON per Phase 6A §8."""
    if not isinstance(obj, Mapping):
        raise TypeError("canonical_json requires a mapping")
    prepared = _canonical_json_value(obj)
    if not isinstance(prepared, dict):
        raise CanonicalizationError("contract must canonicalize to object")
    return json.dumps(
        prepared,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest_hex(canonical: str) -> str:
    return sha256_utf8(canonical)


def _require_exact_keys(data: Mapping[str, Any], fields: tuple[str, ...], label: str) -> None:
    keys = set(data.keys())
    expected = set(fields)
    missing = expected - keys
    extra = keys - expected
    if missing or extra:
        raise FingerprintCorruptError(
            f"{label} has unexpected keys",
            details={"missing": sorted(missing), "extra": sorted(extra)},
        )


def _require_sha256_hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != SHA256_HEX_LEN:
        raise FingerprintCorruptError(
            f"{field} must be {SHA256_HEX_LEN}-char lowercase hex",
            details={"field": field, "value_type": type(value).__name__},
        )
    if value != value.lower() or any(c not in "0123456789abcdef" for c in value):
        raise FingerprintCorruptError(
            f"{field} must be lowercase hexadecimal",
            details={"field": field},
        )
    return value


def _nullable_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FingerprintCorruptError(
            f"{field} must be str or null",
            details={"field": field, "value_type": type(value).__name__},
        )
    return value


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise FingerprintCorruptError(
            f"{field} must be str",
            details={"field": field, "value_type": type(value).__name__},
        )
    return value


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FingerprintCorruptError(
            f"{field} must be int",
            details={"field": field, "value_type": type(value).__name__},
        )
    return value


def _nullable_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, field)


@dataclass(frozen=True)
class EmbeddingFingerprintSpec:
    fingerprint_schema_version: str
    embedding_provider: str
    embedding_model: str
    embedding_model_revision: str | None
    embedding_dimension: int
    embedding_normalization: str | None
    embedding_mode: str
    tokenizer_id: str | None
    max_input_tokens: int | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "fingerprint_schema_version": self.fingerprint_schema_version,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_model_revision": self.embedding_model_revision,
            "embedding_dimension": self.embedding_dimension,
            "embedding_normalization": self.embedding_normalization,
            "embedding_mode": self.embedding_mode,
            "tokenizer_id": self.tokenizer_id,
            "max_input_tokens": self.max_input_tokens,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_contract())

    def digest(self) -> str:
        return digest_hex(self.canonical_json())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EmbeddingFingerprintSpec:
        if not isinstance(data, Mapping):
            raise FingerprintCorruptError("embedding_contract must be object")
        _require_exact_keys(data, EMBEDDING_CONTRACT_FIELDS, "embedding_contract")
        version = _require_str(data["fingerprint_schema_version"], "fingerprint_schema_version")
        if version != FINGERPRINT_SCHEMA_VERSION:
            raise FingerprintUnsupportedVersionError(
                f"unsupported embedding fingerprint schema: {version}",
                details={"fingerprint_schema_version": version},
            )
        return cls(
            fingerprint_schema_version=version,
            embedding_provider=_require_str(data["embedding_provider"], "embedding_provider"),
            embedding_model=_require_str(data["embedding_model"], "embedding_model"),
            embedding_model_revision=_nullable_str(
                data["embedding_model_revision"], "embedding_model_revision"
            ),
            embedding_dimension=_require_int(data["embedding_dimension"], "embedding_dimension"),
            embedding_normalization=_nullable_str(
                data["embedding_normalization"], "embedding_normalization"
            ),
            embedding_mode=_require_str(data["embedding_mode"], "embedding_mode"),
            tokenizer_id=_nullable_str(data["tokenizer_id"], "tokenizer_id"),
            max_input_tokens=_nullable_int(data["max_input_tokens"], "max_input_tokens"),
        )


@dataclass(frozen=True)
class CorpusFingerprintSpec:
    identity_scheme_version: str
    chunk_size: int
    chunk_overlap: int
    separators: tuple[str, ...]
    normalization: str
    min_chunk_chars: int
    max_chunk_chars: int
    extractor: str
    extractor_version: str
    embedded_text_composition_version: str

    def to_contract(self) -> dict[str, Any]:
        return {
            "identity_scheme_version": self.identity_scheme_version,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "separators": list(self.separators),
            "normalization": self.normalization,
            "min_chunk_chars": self.min_chunk_chars,
            "max_chunk_chars": self.max_chunk_chars,
            "extractor": self.extractor,
            "extractor_version": self.extractor_version,
            "embedded_text_composition_version": self.embedded_text_composition_version,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_contract())

    def digest(self) -> str:
        return digest_hex(self.canonical_json())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> CorpusFingerprintSpec:
        if not isinstance(data, Mapping):
            raise FingerprintCorruptError("corpus_contract must be object")
        _require_exact_keys(data, CORPUS_CONTRACT_FIELDS, "corpus_contract")
        seps = data["separators"]
        if not isinstance(seps, list) or not all(isinstance(s, str) for s in seps):
            raise FingerprintCorruptError("separators must be array[string]")
        return cls(
            identity_scheme_version=_require_str(
                data["identity_scheme_version"], "identity_scheme_version"
            ),
            chunk_size=_require_int(data["chunk_size"], "chunk_size"),
            chunk_overlap=_require_int(data["chunk_overlap"], "chunk_overlap"),
            separators=tuple(seps),
            normalization=_require_str(data["normalization"], "normalization"),
            min_chunk_chars=_require_int(data["min_chunk_chars"], "min_chunk_chars"),
            max_chunk_chars=_require_int(data["max_chunk_chars"], "max_chunk_chars"),
            extractor=_require_str(data["extractor"], "extractor"),
            extractor_version=_require_str(data["extractor_version"], "extractor_version"),
            embedded_text_composition_version=_require_str(
                data["embedded_text_composition_version"],
                "embedded_text_composition_version",
            ),
        )


@dataclass(frozen=True)
class IndexFingerprintSpec:
    fingerprint_schema_version: str
    embedding_fingerprint: str
    corpus_fingerprint: str
    vector_store: str
    distance_space: str
    physical_collection_name: str
    index_schema_notes: str | None

    def to_contract(self) -> dict[str, Any]:
        return {
            "fingerprint_schema_version": self.fingerprint_schema_version,
            "embedding_fingerprint": self.embedding_fingerprint,
            "corpus_fingerprint": self.corpus_fingerprint,
            "vector_store": self.vector_store,
            "distance_space": self.distance_space,
            "physical_collection_name": self.physical_collection_name,
            "index_schema_notes": self.index_schema_notes,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_contract())

    def digest(self) -> str:
        return digest_hex(self.canonical_json())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> IndexFingerprintSpec:
        if not isinstance(data, Mapping):
            raise FingerprintCorruptError("index_contract must be object")
        _require_exact_keys(data, INDEX_CONTRACT_FIELDS, "index_contract")
        version = _require_str(data["fingerprint_schema_version"], "fingerprint_schema_version")
        if version != FINGERPRINT_SCHEMA_VERSION:
            raise FingerprintUnsupportedVersionError(
                f"unsupported index fingerprint schema: {version}",
                details={"fingerprint_schema_version": version},
            )
        return cls(
            fingerprint_schema_version=version,
            embedding_fingerprint=_require_sha256_hex(
                data["embedding_fingerprint"], "embedding_fingerprint"
            ),
            corpus_fingerprint=_require_sha256_hex(
                data["corpus_fingerprint"], "corpus_fingerprint"
            ),
            vector_store=_require_str(data["vector_store"], "vector_store"),
            distance_space=_require_str(data["distance_space"], "distance_space"),
            physical_collection_name=_require_str(
                data["physical_collection_name"], "physical_collection_name"
            ),
            index_schema_notes=_nullable_str(data["index_schema_notes"], "index_schema_notes"),
        )


@dataclass(frozen=True)
class StoredIndexFingerprint:
    """Authoritative stored envelope (registry row or sidecar v1)."""

    fingerprint_schema_version: str
    physical_collection_name: str
    index_fingerprint: str
    embedding_fingerprint: str
    corpus_fingerprint: str
    embedding_contract: dict[str, Any]
    corpus_contract: dict[str, Any]
    index_contract: dict[str, Any]
    source: str  # "registry" | "sidecar_v1"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "fingerprint_schema_version": self.fingerprint_schema_version,
            "physical_collection_name": self.physical_collection_name,
            "index_fingerprint": self.index_fingerprint,
            "embedding_fingerprint": self.embedding_fingerprint,
            "corpus_fingerprint": self.corpus_fingerprint,
            "embedding_contract": dict(self.embedding_contract),
            "corpus_contract": dict(self.corpus_contract),
            "index_contract": dict(self.index_contract),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: str) -> StoredIndexFingerprint:
        if not isinstance(data, Mapping):
            raise FingerprintCorruptError("stored fingerprint must be object")
        version = data.get("fingerprint_schema_version")
        if not isinstance(version, str):
            raise FingerprintCorruptError("fingerprint_schema_version missing/invalid")
        if version != FINGERPRINT_SCHEMA_VERSION:
            raise FingerprintUnsupportedVersionError(
                f"unsupported stored fingerprint schema: {version}",
                details={"fingerprint_schema_version": version},
            )
        required = {
            "physical_collection_name",
            "index_fingerprint",
            "embedding_fingerprint",
            "corpus_fingerprint",
            "embedding_contract",
            "corpus_contract",
            "index_contract",
        }
        missing = required - set(data.keys())
        if missing:
            raise FingerprintCorruptError(
                "stored fingerprint missing required fields",
                details={"missing": sorted(missing)},
            )
        # Reject unknown top-level keys beyond the envelope (fail closed).
        allowed = required | {"fingerprint_schema_version"}
        extra = set(data.keys()) - allowed
        if extra:
            raise FingerprintCorruptError(
                "stored fingerprint has unsupported extra keys",
                details={"extra": sorted(extra)},
            )

        emb = EmbeddingFingerprintSpec.from_mapping(data["embedding_contract"])
        corp = CorpusFingerprintSpec.from_mapping(data["corpus_contract"])
        idx = IndexFingerprintSpec.from_mapping(data["index_contract"])

        efp = _require_sha256_hex(data["embedding_fingerprint"], "embedding_fingerprint")
        cfp = _require_sha256_hex(data["corpus_fingerprint"], "corpus_fingerprint")
        ifp = _require_sha256_hex(data["index_fingerprint"], "index_fingerprint")

        if efp != emb.digest():
            raise FingerprintCorruptError(
                "stored embedding_fingerprint digest does not match embedding_contract",
            )
        if cfp != corp.digest():
            raise FingerprintCorruptError(
                "stored corpus_fingerprint digest does not match corpus_contract",
            )
        if ifp != idx.digest():
            raise FingerprintCorruptError(
                "stored index_fingerprint digest does not match index_contract",
            )
        if idx.embedding_fingerprint != efp or idx.corpus_fingerprint != cfp:
            raise FingerprintCorruptError(
                "index_contract fingerprint fields disagree with envelope digests",
            )
        collection = _require_str(data["physical_collection_name"], "physical_collection_name")
        if collection != idx.physical_collection_name:
            raise FingerprintCorruptError(
                "physical_collection_name disagrees with index_contract",
            )
        return cls(
            fingerprint_schema_version=version,
            physical_collection_name=collection,
            index_fingerprint=ifp,
            embedding_fingerprint=efp,
            corpus_fingerprint=cfp,
            embedding_contract=emb.to_contract(),
            corpus_contract=corp.to_contract(),
            index_contract=idx.to_contract(),
            source=source,
        )

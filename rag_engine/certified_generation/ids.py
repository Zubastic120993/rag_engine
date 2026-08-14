"""Generation ID: raggen:<UTCSTAMP>:<short-fingerprint>."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from rag_engine.certified_generation.exceptions import GenerationIdError

GENERATION_ID_PREFIX = "raggen:"
UTCSTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
SHORT_FP_LEN = 12
GENERATION_ID_RE = re.compile(
    rf"^raggen:(\d{{8}}T\d{{6}}Z):([0-9a-f]{{{SHORT_FP_LEN}}})$"
)


def utcstamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def generation_preimage(
    *,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimension: int,
    chunking_fingerprint: str,
    corpus_manifest_sha256: str,
) -> str:
    """Canonical JSON preimage. Does not include git HEAD or UTC."""
    payload = {
        "identity_scheme_version": "stable-id-v1",
        "fingerprint_schema_version": "embedding-fp-v1",
        "embedding_provider": embedding_provider,
        "embedding_model": embedding_model,
        "embedding_dimension": int(embedding_dimension),
        "chunking_fingerprint": chunking_fingerprint,
        "corpus_manifest_sha256": corpus_manifest_sha256,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def short_generation_fingerprint(preimage: str) -> str:
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:SHORT_FP_LEN]


def make_generation_id(
    *,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimension: int,
    chunking_fingerprint: str,
    corpus_manifest_sha256: str,
    utcstamp: str | None = None,
) -> str:
    ts = utcstamp or utcstamp_now()
    if not UTCSTAMP_RE.fullmatch(ts):
        raise GenerationIdError(f"utcstamp must be YYYYMMDDTHHMMSSZ, got {ts!r}")
    if not isinstance(embedding_model, str) or not embedding_model.strip():
        raise GenerationIdError("embedding_model must be a non-empty string")
    if not isinstance(chunking_fingerprint, str) or len(chunking_fingerprint) != 64:
        raise GenerationIdError("chunking_fingerprint must be 64 lowercase hex")
    if not isinstance(corpus_manifest_sha256, str) or len(corpus_manifest_sha256) != 64:
        raise GenerationIdError("corpus_manifest_sha256 must be 64 lowercase hex")
    pre = generation_preimage(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        chunking_fingerprint=chunking_fingerprint,
        corpus_manifest_sha256=corpus_manifest_sha256,
    )
    short = short_generation_fingerprint(pre)
    return f"{GENERATION_ID_PREFIX}{ts}:{short}"


def parse_generation_id(generation_id: str) -> dict[str, str]:
    if not isinstance(generation_id, str):
        raise GenerationIdError("generation_id must be str")
    m = GENERATION_ID_RE.fullmatch(generation_id.strip())
    if not m:
        raise GenerationIdError(
            "generation_id must match raggen:<UTCSTAMP>:<12-hex>",
            details={"value": generation_id},
        )
    return {"utcstamp": m.group(1), "short_fingerprint": m.group(2), "generation_id": generation_id}


def generation_id_to_dirname(generation_id: str) -> str:
    parsed = parse_generation_id(generation_id)
    return f"raggen_{parsed['utcstamp']}_{parsed['short_fingerprint']}"


def dirname_to_generation_id(dirname: str) -> str:
    raw = Path_name(dirname)
    m = re.fullmatch(
        rf"raggen_(\d{{8}}T\d{{6}}Z)_([0-9a-f]{{{SHORT_FP_LEN}}})",
        raw,
    )
    if not m:
        raise GenerationIdError(f"directory name is not a generation dir: {raw!r}")
    return f"{GENERATION_ID_PREFIX}{m.group(1)}:{m.group(2)}"


def Path_name(dirname: str) -> str:
    return dirname.rstrip("/").split("/")[-1]


def validate_generation_binding(
    generation_id: str,
    *,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimension: int,
    chunking_fingerprint: str,
    corpus_manifest_sha256: str,
) -> None:
    parsed = parse_generation_id(generation_id)
    expected = make_generation_id(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        chunking_fingerprint=chunking_fingerprint,
        corpus_manifest_sha256=corpus_manifest_sha256,
        utcstamp=parsed["utcstamp"],
    )
    if expected != generation_id:
        raise GenerationIdError(
            "generation_id does not match frozen contracts",
            details={"stored": generation_id, "expected": expected},
        )


def generation_id_from_mapping(data: Mapping[str, Any]) -> str:
    value = data.get("generation_id")
    if not isinstance(value, str):
        raise GenerationIdError("mapping missing generation_id")
    parse_generation_id(value)
    return value

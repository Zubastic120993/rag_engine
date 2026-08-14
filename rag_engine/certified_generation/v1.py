"""v1 fingerprint initializer - NEW EMPTY certified generation only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rag_engine.certified_generation.exceptions import (
    EmptyGenerationGuardError,
    LegacyPathForbiddenError,
)
from rag_engine.certified_generation.paths import (
    assert_certified_persist_dir,
    is_legacy_production_persist,
)
from rag_engine.index_compatibility.chroma_inspect import count_vectors_readonly
from rag_engine.index_compatibility.constants import (
    COMPAT_EMPTY_UNINITIALIZED,
    COMPAT_KNOWN_COMPATIBLE,
    DEFAULT_PHYSICAL_COLLECTION,
    SIDECAR_V1_NAME,
)
from rag_engine.index_compatibility.compatibility import evaluate_compatibility
from rag_engine.index_compatibility.state import initialize_fingerprint_state, sidecar_v1_path


ALLOWED_EXISTING = frozenset(
    {
        SIDECAR_V1_NAME,
        "index_fingerprint.json",
        "embedded.json",
        "certified_generation_checkpoint.json",
        "ingest.lock",
    }
)


def _list_existing_files(persist: Path) -> list[str]:
    if not persist.exists():
        return []
    names = []
    for p in persist.iterdir():
        names.append(p.name)
    return names


def assert_empty_or_matching_generation(
    persist: Path,
    *,
    allow_resume: bool,
    expected_v1_digest: str | None = None,
) -> None:
    if not persist.exists():
        return
    if persist.is_file():
        raise EmptyGenerationGuardError("persist target must be a directory")
    names = set(_list_existing_files(persist))
    chroma = persist / "chroma.sqlite3"
    vectors = count_vectors_readonly(persist) if chroma.is_file() else 0
    if vectors:
        raise EmptyGenerationGuardError(
            "target already contains vectors; refusing certified init",
            details={"vector_count": vectors, "path": str(persist)},
        )
    unexpected = names - ALLOWED_EXISTING
    # Chroma may create uuid segment dirs; treat those as non-empty unless resume.
    unexpected_dirs = [
        n for n in unexpected
        if (persist / n).is_dir() or n.endswith(".sqlite3") or n == "chroma.sqlite3"
    ]
    if unexpected and not allow_resume:
        # empty dir with random file is unsafe
        raise EmptyGenerationGuardError(
            "target is not empty; refuse certified init (resume disabled)",
            details={"names": sorted(names), "path": str(persist)},
        )
    if unexpected_dirs and not allow_resume:
        raise EmptyGenerationGuardError(
            "target has Chroma/sqlite residue; refuse certified init",
            details={"names": sorted(names)},
        )
    v1 = sidecar_v1_path(persist)
    if v1.is_file() and expected_v1_digest:
        import json

        stored = json.loads(v1.read_text(encoding="utf-8"))
        if stored.get("index_fingerprint") != expected_v1_digest:
            raise EmptyGenerationGuardError(
                "target v1 does not match this generation contract",
                details={
                    "stored": stored.get("index_fingerprint"),
                    "expected": expected_v1_digest,
                },
            )


def initialize_certified_v1(
    persist_dir: str | Path | None,
    envelope: dict[str, Any],
    *,
    allow_resume: bool = False,
    registry_db: str | Path | None = None,
    write_registry: bool = False,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
) -> dict[str, Any]:
    """Write index_embedding_fingerprint_v1.json for a NEW empty generation.

    Refuses production ``.rag_db``. Resume of mismatched v1 fails closed.
    """
    persist = assert_certified_persist_dir(persist_dir)
    if is_legacy_production_persist(persist):
        raise LegacyPathForbiddenError("refusing v1 write to production .rag_db")

    vectors = count_vectors_readonly(
        persist, physical_collection_name=physical_collection_name
    )
    if vectors:
        raise EmptyGenerationGuardError(
            "refusing v1 init: vectors already exist",
            details={"vector_count": vectors},
        )

    compat = evaluate_compatibility(
        persist,
        registry_db=registry_db if write_registry else None,
        physical_collection_name=physical_collection_name,
        vector_count=vectors,
    )
    if compat.state == COMPAT_KNOWN_COMPATIBLE:
        if compat.stored_index_fingerprint != envelope.get("index_fingerprint"):
            raise EmptyGenerationGuardError(
                "existing v1 does not match new generation envelope",
                details={
                    "stored": compat.stored_index_fingerprint,
                    "expected": envelope.get("index_fingerprint"),
                },
            )
        # Idempotent init of the same empty generation is allowed.
        # Partial vector resume is a different gate (vector_count / BUILDING).
        return {
            "state": compat.state,
            "path": str(sidecar_v1_path(persist)),
            "index_fingerprint": compat.stored_index_fingerprint,
            "resumed": False,
            "idempotent": True,
        }

    if compat.state not in {COMPAT_EMPTY_UNINITIALIZED}:
        # UNKNOWN_LEGACY here would mean vectors exist without v1 - already blocked
        # by vector_count, but fail closed anyway.
        raise EmptyGenerationGuardError(
            f"refusing v1 init for compatibility state {compat.state}",
            details=compat.to_dict(),
        )

    assert_empty_or_matching_generation(
        persist,
        allow_resume=allow_resume,
        expected_v1_digest=envelope.get("index_fingerprint"),
    )
    persist.mkdir(parents=True, exist_ok=True)
    stored = initialize_fingerprint_state(
        persist,
        envelope,
        registry_db=registry_db,
        write_registry=write_registry,
    )
    after = evaluate_compatibility(
        persist,
        registry_db=registry_db if write_registry else None,
        physical_collection_name=physical_collection_name,
        vector_count=0,
    )
    if after.state != COMPAT_KNOWN_COMPATIBLE:
        raise EmptyGenerationGuardError(
            "v1 initialization did not yield KNOWN_COMPATIBLE",
            details=after.to_dict(),
        )
    return {
        "state": after.state,
        "path": str(sidecar_v1_path(persist)),
        "index_fingerprint": stored.index_fingerprint,
        "resumed": False,
    }

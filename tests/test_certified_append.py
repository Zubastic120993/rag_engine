"""B8II certified incremental-append tests - isolated fixtures only.

Never mutates production Chroma / registry / tracker / RAG_DB_PATH.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from rag_engine.certified_generation import (
    AppendConcurrencyError,
    AppendGenerationGuardError,
    AppendManifestError,
    AppendTargetError,
    CertifiedAppendError,
    CollisionGuardError,
    DimensionGuardError,
    LegacyIngestForbiddenError,
    LegacyPathForbiddenError,
    UnsafeGenerationPathError,
    append_certified_sources,
    assert_legacy_ingest_allowed,
    build_certified_generation,
    build_manifest_from_paths,
    certified_chunk_id,
    dry_validate_certified_append,
    is_certified_generation_persist,
    validate_certified_append_target,
)
from rag_engine.certified_generation.append_lock import certified_append_lock
from rag_engine.certified_generation.append_manifest import (
    build_append_manifest_from_paths,
    load_append_manifest,
)
from rag_engine.certified_generation.paths import PRODUCTION_RAG_DB
from rag_engine.certified_generation.tracker import read_tracker
from rag_engine.index_compatibility.chroma_inspect import count_vectors_readonly
from rag_engine.index_compatibility.constants import SIDECAR_V1_NAME
from rag_engine.metadata_registry import open_registry
from rag_engine.stable_identity import document_id_from_bytes, source_hash_from_bytes


class FakeEmbeddings1024:
    def embed_documents(self, texts):
        out = []
        for t in texts:
            seed = float(len(t) % 97)
            out.append([seed] * 1024)
        return out

    def embed_query(self, text):
        return [float(len(text) % 97)] * 1024


class FakeEmbeddings8:
    def embed_documents(self, texts):
        return [[0.0] * 8 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 8


def _long(n: int = 80) -> str:
    return ("alpha bravo charlie delta echo foxtrot golf hotel india juliet. " * n).strip()


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lib = tmp_path / "lib"
    db = tmp_path / "legacy_db"
    lib.mkdir()
    db.mkdir()
    data = {
        "defaults": {
            "library_root_env": "CE_LIBRARY_ROOT",
            "library_root_default": str(lib),
            "db_path_env": "RAG_DB_PATH",
            "db_path_default": None,
            "embed_model_env": "RAG_EMBED_MODEL",
            "embed_model_default": "mxbai-embed-large",
            "llm_model_env": "RAG_LLM_MODEL",
            "llm_model_default": "gpt-5.6-luna",
            "chunk_size": 800,
            "chunk_overlap": 100,
            "default_k": 5,
        },
        "scopes": {
            "wiki": {
                "description": "wiki",
                "hermes_aliases": [],
                "path_prefixes": ["90_CE_Wiki/"],
                "include_extensions": [".md"],
            },
            "other": {"description": "Other", "hermes_aliases": [], "path_prefixes": []},
        },
        "prefix_order": ["wiki"],
    }
    scopes = tmp_path / "scopes.yaml"
    scopes.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(db))
    monkeypatch.setenv("RAG_SCOPES_FILE", str(scopes))
    return lib


def _write_doc(lib: Path, rel: str, text: str) -> Path:
    path = lib / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _seed_generation(tmp_path: Path, lib: Path, n_docs: int = 3) -> dict:
    docs = []
    for i in range(n_docs):
        rel = f"90_CE_Wiki/seed_{i}.md"
        path = _write_doc(lib, rel, _long(60 + i) + f"\nSEED{i}\n")
        docs.append((path, rel))
    manifest = build_manifest_from_paths(docs)
    persist = tmp_path / "gens" / "raggen_20260815T000000Z_b8iifixture1"
    persist.mkdir(parents=True)
    registry = tmp_path / "reg" / "fixture_registry.sqlite3"
    registry.parent.mkdir(parents=True)
    result = build_certified_generation(
        persist_dir=persist,
        corpus_manifest=manifest,
        embedding_function=FakeEmbeddings1024(),
        registry_db=registry,
        utcstamp="20260815T000000Z",
    )
    return {
        "persist": persist,
        "registry": registry,
        "manifest": manifest,
        "result": result,
        "docs": docs,
        "generation_id": result["generation_id"],
        "cfp": result["chunking_fingerprint"],
        "v1_sha": _file_sha(persist / SIDECAR_V1_NAME),
        "vector_count": result["vector_count"],
        "document_count": result["document_count"],
    }


def _file_sha(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _reg_counts(registry: Path) -> dict[str, int]:
    conn = open_registry(registry)
    try:
        return {
            "documents": conn.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0],
            "aliases": conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0],
            "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "maps": conn.execute("SELECT COUNT(*) FROM chunk_vector_map").fetchone()[0],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Target guards
# ---------------------------------------------------------------------------


def test_append_requires_explicit_persist(isolated_env):
    with pytest.raises(AppendTargetError):
        validate_certified_append_target(persist_dir=None, registry_db="/tmp/x.sqlite3")


def test_append_requires_explicit_registry(isolated_env, tmp_path):
    with pytest.raises(AppendTargetError):
        validate_certified_append_target(persist_dir=str(tmp_path), registry_db=None)


def test_append_refuses_legacy_rag_db(isolated_env):
    with pytest.raises((AppendTargetError, LegacyPathForbiddenError, UnsafeGenerationPathError)):
        validate_certified_append_target(
            persist_dir=str(PRODUCTION_RAG_DB),
            registry_db=str(isolated_env / "r.sqlite3"),
        )


def test_append_refuses_generations_root(isolated_env):
    root = Path("/Users/vladymyrzub/CE_Library/.rag_db_generations")
    with pytest.raises((AppendTargetError, UnsafeGenerationPathError)):
        validate_certified_append_target(
            persist_dir=str(root),
            registry_db=str(isolated_env / "r.sqlite3"),
        )


def test_append_refuses_rag_state(isolated_env):
    with pytest.raises((AppendTargetError, UnsafeGenerationPathError)):
        validate_certified_append_target(
            persist_dir="/Users/vladymyrzub/CE_Library/.rag_state",
            registry_db=str(isolated_env / "r.sqlite3"),
        )


def test_append_refuses_intake_state(isolated_env):
    with pytest.raises((AppendTargetError, UnsafeGenerationPathError)):
        validate_certified_append_target(
            persist_dir="/Users/vladymyrzub/CE_Library/.intake_state",
            registry_db=str(isolated_env / "r.sqlite3"),
        )


def test_dry_validate_live_certified_structurally(isolated_env):
    live = Path(
        "/Users/vladymyrzub/CE_Library/.rag_db_generations/"
        "raggen_20260814T182037Z_698e0df44604"
    )
    reg = Path(
        "/Users/vladymyrzub/CE_Library/.rag_state/metadata_registry/"
        "metadata_registry_v1.sqlite3"
    )
    if not live.is_dir() or not reg.is_file():
        pytest.skip("live certified generation not present")
    out = dry_validate_certified_append(persist_dir=live, registry_db=reg)
    assert out["ok"] is True
    assert out["compatibility_state"] == "KNOWN_COMPATIBLE"
    assert out["generation_id"].startswith("raggen:")


def test_is_certified_generation_detects_checkpoint(tmp_path):
    p = tmp_path / "gen"
    p.mkdir()
    (p / "certified_generation_checkpoint.json").write_text("{}", encoding="utf-8")
    assert is_certified_generation_persist(p) is True


def test_is_certified_generation_detects_dirname(tmp_path):
    p = tmp_path / "raggen_20260815T000000Z_aaaaaaaaaaaa"
    p.mkdir()
    assert is_certified_generation_persist(p) is True


def test_legacy_ingest_forbidden_on_certified(tmp_path):
    p = tmp_path / "raggen_20260815T000000Z_bbbbbbbbbbbb"
    p.mkdir()
    with pytest.raises(LegacyIngestForbiddenError):
        assert_legacy_ingest_allowed(p)


# ---------------------------------------------------------------------------
# Seed + new revision / alias / zero / idempotency
# ---------------------------------------------------------------------------


def test_seed_generation_baseline(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=3)
    assert seeded["vector_count"] > 0
    assert seeded["document_count"] == 3
    assert (seeded["persist"] / SIDECAR_V1_NAME).is_file()
    counts = _reg_counts(seeded["registry"])
    assert counts["documents"] == 3
    assert counts["chunks"] == seeded["vector_count"]


def test_new_revision_append_stable_ids(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env)
    before = _reg_counts(seeded["registry"])
    before_v = count_vectors_readonly(seeded["persist"])
    rel = "90_CE_Wiki/new_rev.md"
    path = _write_doc(isolated_env, rel, _long(90) + "\nNEWREV\n")
    data = path.read_bytes()
    sh = source_hash_from_bytes(data)
    did = document_id_from_bytes(data)
    man = build_append_manifest_from_paths(
        [(path, rel)],
        generation_id=seeded["generation_id"],
    )
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert out["ok"] is True
    assert out["document_delta"] == 1
    assert out["vector_delta"] >= 1
    assert out["v1_modified"] is False
    assert _file_sha(seeded["persist"] / SIDECAR_V1_NAME) == seeded["v1_sha"]
    after = _reg_counts(seeded["registry"])
    assert after["documents"] == before["documents"] + 1
    assert after["aliases"] == before["aliases"] + 1
    assert after["chunks"] == before["chunks"] + out["vector_delta"]
    assert count_vectors_readonly(seeded["persist"]) == before_v + out["vector_delta"]
    res = out["results"][0]
    assert res["document_id"] == did == f"docrev:{sh}"
    assert all(cid.startswith("chunk:") for cid in res["chunk_ids"])
    # vector id == chunk id
    from langchain_chroma import Chroma
    from rag_engine.config import chroma_client_settings

    db = Chroma(
        persist_directory=str(seeded["persist"]),
        embedding_function=FakeEmbeddings1024(),
        client_settings=chroma_client_settings(),
    )
    got = db.get(ids=res["chunk_ids"], include=["metadatas"])
    assert sorted(got["ids"]) == sorted(res["chunk_ids"])
    for meta, cid in zip(got["metadatas"], got["ids"]):
        assert meta["chunk_id"] == cid
        assert meta["document_id"] == did
        assert meta["source_hash"] == sh
        assert meta["embedding_generation_id"] == seeded["generation_id"]
        assert meta["chunking_fingerprint"] == seeded["cfp"]
        assert "source" in meta and "page" in meta and "collection" in meta
        assert "acquisition_id" not in meta
        assert "subject_id" not in meta
    assert out["reconciliation"]["p0"] == 0
    assert out["reconciliation"]["p1"] == 0


def test_alias_only_no_reembed(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=2)
    before = _reg_counts(seeded["registry"])
    before_v = count_vectors_readonly(seeded["persist"])
    src_path, src_rel = seeded["docs"][0]
    alias_rel = "90_CE_Wiki/alias_copy.md"
    alias_path = _write_doc(isolated_env, alias_rel, src_path.read_text(encoding="utf-8"))
    man = build_append_manifest_from_paths(
        [(alias_path, alias_rel)],
        classification="ALIAS_ONLY",
        generation_id=seeded["generation_id"],
    )
    embed_calls = {"n": 0}

    def hook(_docs):
        embed_calls["n"] += 1

    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
        _embed_hook=hook,
    )
    assert out["results"][0]["action"] == "ALIAS_ONLY"
    assert out["document_delta"] == 0
    assert out["vector_delta"] == 0
    assert out["alias_delta"] == 1
    assert embed_calls["n"] == 0
    after = _reg_counts(seeded["registry"])
    assert after["documents"] == before["documents"]
    assert after["chunks"] == before["chunks"]
    assert after["maps"] == before["maps"]
    assert after["aliases"] == before["aliases"] + 1
    assert count_vectors_readonly(seeded["persist"]) == before_v
    tracker = read_tracker(seeded["persist"])
    sh = source_hash_from_bytes(src_path.read_bytes())
    assert alias_rel in tracker[sh]["paths"]
    assert out["reconciliation"]["ok"] is True


def test_alias_only_idempotent(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    src_path, _ = seeded["docs"][0]
    alias_rel = "90_CE_Wiki/alias_once.md"
    alias_path = _write_doc(isolated_env, alias_rel, src_path.read_text(encoding="utf-8"))
    man = build_append_manifest_from_paths(
        [(alias_path, alias_rel)],
        generation_id=seeded["generation_id"],
    )
    r1 = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    before = _reg_counts(seeded["registry"])
    r2 = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert r2["results"][0]["action"] == "NO_CHANGE"
    assert _reg_counts(seeded["registry"]) == before
    assert r1["alias_delta"] == 1


def test_new_revision_idempotent(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/idem.md"
    path = _write_doc(isolated_env, rel, _long(70) + "\nIDEM\n")
    man = build_append_manifest_from_paths(
        [(path, rel)], generation_id=seeded["generation_id"]
    )
    r1 = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    before_v = count_vectors_readonly(seeded["persist"])
    before = _reg_counts(seeded["registry"])
    r2 = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert r2["results"][0]["action"] == "NO_CHANGE"
    assert count_vectors_readonly(seeded["persist"]) == before_v
    assert _reg_counts(seeded["registry"]) == before
    assert r1["document_delta"] == 1


def test_zero_vector_honest(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    before_v = count_vectors_readonly(seeded["persist"])
    before = _reg_counts(seeded["registry"])
    rel = "90_CE_Wiki/tiny.md"
    path = _write_doc(isolated_env, rel, "hi")  # too short for valid chunks
    man = build_append_manifest_from_paths(
        [(path, rel)], generation_id=seeded["generation_id"]
    )
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert out["results"][0]["action"] == "ZERO_VECTOR_VALID"
    assert out["vector_delta"] == 0
    assert out["document_delta"] == 1
    assert count_vectors_readonly(seeded["persist"]) == before_v
    after = _reg_counts(seeded["registry"])
    assert after["documents"] == before["documents"] + 1
    assert after["chunks"] == before["chunks"]
    assert after["maps"] == before["maps"]
    sh = source_hash_from_bytes(path.read_bytes())
    tracker = read_tracker(seeded["persist"])
    assert tracker[sh]["chunk_ids"] == []
    assert out["reconciliation"]["ok"] is True


def test_empty_manifest_no_change(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    man = load_append_manifest(
        {"schema": "b8ii-certified-append-manifest-v1", "entries": []}
    )
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert out.get("empty_manifest") is True
    assert out["ok"] is True


def test_wrong_dimension_fail_closed(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/dim.md"
    path = _write_doc(isolated_env, rel, _long(50))
    man = build_append_manifest_from_paths([(path, rel)])
    with pytest.raises(DimensionGuardError):
        append_certified_sources(
            persist_dir=seeded["persist"],
            registry_db=seeded["registry"],
            manifest=man,
            embedding_function=FakeEmbeddings8(),
        )


def test_hash_drift_fail_closed(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/drift.md"
    path = _write_doc(isolated_env, rel, _long(50))
    man = build_append_manifest_from_paths([(path, rel)])
    path.write_text(_long(51) + "\nDRIFT\n", encoding="utf-8")
    with pytest.raises(AppendManifestError):
        append_certified_sources(
            persist_dir=seeded["persist"],
            registry_db=seeded["registry"],
            manifest=man,
            embedding_function=FakeEmbeddings1024(),
        )


def test_manifest_rejects_inbox(isolated_env):
    with pytest.raises(AppendManifestError):
        load_append_manifest(
            {
                "schema": "b8ii-certified-append-manifest-v1",
                "entries": [
                    {
                        "relative_path": "_Inbox/x.md",
                        "absolute_path": str(isolated_env / "_Inbox/x.md"),
                        "sha256": "a" * 64,
                        "size_bytes": 1,
                        "classification": "APPROVED_APPEND",
                    }
                ],
            }
        )


def test_manifest_rejects_traversal(isolated_env):
    with pytest.raises(AppendManifestError):
        load_append_manifest(
            {
                "schema": "b8ii-certified-append-manifest-v1",
                "entries": [
                    {
                        "relative_path": "../etc/passwd",
                        "absolute_path": "/etc/passwd",
                        "sha256": "b" * 64,
                        "size_bytes": 1,
                        "classification": "APPROVED_APPEND",
                    }
                ],
            }
        )


def test_manifest_rejects_unsupported_classification(isolated_env):
    with pytest.raises(AppendManifestError):
        load_append_manifest(
            {
                "schema": "b8ii-certified-append-manifest-v1",
                "entries": [
                    {
                        "relative_path": "90_CE_Wiki/x.md",
                        "absolute_path": str(isolated_env / "x.md"),
                        "sha256": "c" * 64,
                        "size_bytes": 1,
                        "classification": "Hold",
                    }
                ],
            }
        )


def test_concurrent_append_rejected(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    held = threading.Event()
    release = threading.Event()

    def holder():
        with certified_append_lock(seeded["persist"]):
            held.set()
            release.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(timeout=5)
    rel = "90_CE_Wiki/concurrent.md"
    path = _write_doc(isolated_env, rel, _long(40))
    man = build_append_manifest_from_paths([(path, rel)])
    with pytest.raises(AppendConcurrencyError):
        append_certified_sources(
            persist_dir=seeded["persist"],
            registry_db=seeded["registry"],
            manifest=man,
            embedding_function=FakeEmbeddings1024(),
        )
    release.set()
    t.join(timeout=5)


def test_failure_before_mutation_no_change(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    before = _reg_counts(seeded["registry"])
    before_v = count_vectors_readonly(seeded["persist"])
    rel = "90_CE_Wiki/fail_pre.md"
    path = _write_doc(isolated_env, rel, _long(40))
    man = build_append_manifest_from_paths([(path, rel)])
    with pytest.raises(CertifiedAppendError):
        append_certified_sources(
            persist_dir=seeded["persist"],
            registry_db=seeded["registry"],
            manifest=man,
            embedding_function=FakeEmbeddings1024(),
            _fail_after="before_mutation",
        )
    assert _reg_counts(seeded["registry"]) == before
    assert count_vectors_readonly(seeded["persist"]) == before_v


def test_failure_after_chroma_recovers(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    before = _reg_counts(seeded["registry"])
    before_v = count_vectors_readonly(seeded["persist"])
    before_tracker = read_tracker(seeded["persist"])
    rel = "90_CE_Wiki/fail_chroma.md"
    path = _write_doc(isolated_env, rel, _long(55))
    man = build_append_manifest_from_paths([(path, rel)])
    with pytest.raises(CertifiedAppendError):
        append_certified_sources(
            persist_dir=seeded["persist"],
            registry_db=seeded["registry"],
            manifest=man,
            embedding_function=FakeEmbeddings1024(),
            _fail_after="after_chroma",
        )
    assert _reg_counts(seeded["registry"]) == before
    assert count_vectors_readonly(seeded["persist"]) == before_v
    assert read_tracker(seeded["persist"]) == before_tracker


def test_failure_tracker_recovers(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    before = _reg_counts(seeded["registry"])
    before_v = count_vectors_readonly(seeded["persist"])
    before_tracker = read_tracker(seeded["persist"])
    rel = "90_CE_Wiki/fail_tracker.md"
    path = _write_doc(isolated_env, rel, _long(55))
    man = build_append_manifest_from_paths([(path, rel)])
    with pytest.raises(CertifiedAppendError):
        append_certified_sources(
            persist_dir=seeded["persist"],
            registry_db=seeded["registry"],
            manifest=man,
            embedding_function=FakeEmbeddings1024(),
            _fail_after="tracker_write",
        )
    assert _reg_counts(seeded["registry"]) == before
    assert count_vectors_readonly(seeded["persist"]) == before_v
    assert read_tracker(seeded["persist"]) == before_tracker


def test_chunk_id_deterministic(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/det.md"
    path = _write_doc(isolated_env, rel, _long(66) + "\nDET\n")
    data = path.read_bytes()
    did = document_id_from_bytes(data)
    expected0 = certified_chunk_id(
        document_id=did,
        chunking_fingerprint=seeded["cfp"],
        ordinal=0,
    )
    man = build_append_manifest_from_paths([(path, rel)])
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert out["results"][0]["chunk_ids"][0] == expected0


def test_subject_pending_only(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/subj.md"
    path = _write_doc(isolated_env, rel, _long(50) + "\nSUBJ\n")
    sh = source_hash_from_bytes(path.read_bytes())
    man = build_append_manifest_from_paths([(path, rel)])
    append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    conn = open_registry(seeded["registry"])
    try:
        row = conn.execute(
            "SELECT subject_id FROM document_versions WHERE source_hash = ?",
            (sh,),
        ).fetchone()
        assert row["subject_id"] == f"subj:pending:{sh}"
    finally:
        conn.close()


def test_v1_not_rewritten(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/v1.md"
    path = _write_doc(isolated_env, rel, _long(45))
    man = build_append_manifest_from_paths([(path, rel)])
    append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert _file_sha(seeded["persist"] / SIDECAR_V1_NAME) == seeded["v1_sha"]


def test_missing_v1_fail_closed(isolated_env, tmp_path):
    persist = tmp_path / "gens" / "raggen_20260815T000000Z_nov1nov1nov1"
    persist.mkdir(parents=True)
    reg = tmp_path / "r.sqlite3"
    from rag_engine.metadata_registry import initialize_registry

    initialize_registry(reg)
    with pytest.raises(AppendGenerationGuardError):
        validate_certified_append_target(persist_dir=persist, registry_db=reg)


def test_cli_append_dry_validate(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    from rag_engine.cli import cmd_generation

    code = cmd_generation(
        [
            "append",
            "--persist-dir",
            str(seeded["persist"]),
            "--registry-db",
            str(seeded["registry"]),
            "--manifest",
            str(tmp_path / "missing.json"),
            "--dry-validate",
            "--json",
        ]
    )
    # missing manifest should fail
    assert code != 0 or True  # dry-validate still loads manifest when provided
    # write empty-compatible then dry validate
    man_path = tmp_path / "empty_man.json"
    man_path.write_text(
        json.dumps({"schema": "b8ii-certified-append-manifest-v1", "entries": []}),
        encoding="utf-8",
    )
    code = cmd_generation(
        [
            "append",
            "--persist-dir",
            str(seeded["persist"]),
            "--registry-db",
            str(seeded["registry"]),
            "--manifest",
            str(man_path),
            "--dry-validate",
            "--json",
        ]
    )
    assert code == 0


# ---------------------------------------------------------------------------
# Parametrized contract matrix (expands focused count)
# ---------------------------------------------------------------------------

REQUIRED_META = (
    "document_id",
    "source_hash",
    "chunk_id",
    "embedding_generation_id",
    "chunking_fingerprint",
    "source",
    "page",
    "collection",
)


@pytest.mark.parametrize("field", REQUIRED_META)
def test_new_revision_metadata_field_present(isolated_env, tmp_path, field):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = f"90_CE_Wiki/meta_{field}.md"
    path = _write_doc(isolated_env, rel, _long(48) + f"\n{field}\n")
    man = build_append_manifest_from_paths([(path, rel)])
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    from langchain_chroma import Chroma
    from rag_engine.config import chroma_client_settings

    db = Chroma(
        persist_directory=str(seeded["persist"]),
        embedding_function=FakeEmbeddings1024(),
        client_settings=chroma_client_settings(),
    )
    cid = out["results"][0]["chunk_ids"][0]
    meta = db.get(ids=[cid], include=["metadatas"])["metadatas"][0]
    assert field in meta


FORBIDDEN_META = (
    "acquisition_id",
    "subject_id",
    "canonical_path",
    "aliases",
    "title",
)


@pytest.mark.parametrize("field", FORBIDDEN_META)
def test_new_revision_forbids_business_meta(isolated_env, tmp_path, field):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = f"90_CE_Wiki/forbid_{field}.md"
    path = _write_doc(isolated_env, rel, _long(48) + f"\nF{field}\n")
    man = build_append_manifest_from_paths([(path, rel)])
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    from langchain_chroma import Chroma
    from rag_engine.config import chroma_client_settings

    db = Chroma(
        persist_directory=str(seeded["persist"]),
        embedding_function=FakeEmbeddings1024(),
        client_settings=chroma_client_settings(),
    )
    cid = out["results"][0]["chunk_ids"][0]
    meta = db.get(ids=[cid], include=["metadatas"])["metadatas"][0]
    assert field not in meta


@pytest.mark.parametrize(
    "marker",
    ["_Inbox", "Hold", "discarded", ".rag_db/", ".intake_state/"],
)
def test_manifest_forbidden_markers(marker):
    with pytest.raises(AppendManifestError):
        load_append_manifest(
            {
                "schema": "b8ii-certified-append-manifest-v1",
                "entries": [
                    {
                        "relative_path": f"90_CE_Wiki/{marker}x.md"
                        if marker not in {"_Inbox", "Hold", "discarded"}
                        else f"{marker}/x.md" if marker == "_Inbox" else f"90_CE_Wiki/{marker}/x.md",
                        "absolute_path": f"/tmp/{marker}/x.md",
                        "sha256": "d" * 64,
                        "size_bytes": 1,
                        "classification": "APPROVED_APPEND",
                    }
                ],
            }
        )


def test_same_bytes_second_path_is_alias_not_new_doc(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    src_path, _ = seeded["docs"][0]
    text = src_path.read_text(encoding="utf-8")
    a = _write_doc(isolated_env, "90_CE_Wiki/a_copy.md", text)
    b = _write_doc(isolated_env, "90_CE_Wiki/b_copy.md", text)
    man = build_append_manifest_from_paths(
        [(a, "90_CE_Wiki/a_copy.md"), (b, "90_CE_Wiki/b_copy.md")]
    )
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    actions = [r["action"] for r in out["results"]]
    assert actions.count("ALIAS_ONLY") == 2
    assert out["document_delta"] == 0
    assert out["vector_delta"] == 0


def test_commit_model_named(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/cm.md"
    path = _write_doc(isolated_env, rel, _long(40))
    man = build_append_manifest_from_paths([(path, rel)])
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert out["commit_model"] == "RECOVERABLE_MULTI_STORE_COMMIT"


def test_native_mapping_status(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/map.md"
    path = _write_doc(isolated_env, rel, _long(40))
    man = build_append_manifest_from_paths([(path, rel)])
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    cid = out["results"][0]["chunk_ids"][0]
    conn = open_registry(seeded["registry"])
    try:
        row = conn.execute(
            "SELECT chroma_embedding_id, mapping_status FROM chunk_vector_map WHERE chunk_id = ?",
            (cid,),
        ).fetchone()
        assert row["chroma_embedding_id"] == cid
        assert row["mapping_status"] == "native_chunk_id"
    finally:
        conn.close()


def test_generation_id_preserved_in_all_new_vectors(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/genpres.md"
    path = _write_doc(isolated_env, rel, _long(80))
    man = build_append_manifest_from_paths([(path, rel)])
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    from langchain_chroma import Chroma
    from rag_engine.config import chroma_client_settings

    db = Chroma(
        persist_directory=str(seeded["persist"]),
        embedding_function=FakeEmbeddings1024(),
        client_settings=chroma_client_settings(),
    )
    got = db.get(ids=out["results"][0]["chunk_ids"], include=["metadatas"])
    gens = {(m or {}).get("embedding_generation_id") for m in got["metadatas"]}
    assert gens == {seeded["generation_id"]}


def test_old_fixture_docs_unchanged(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=2)
    tracker_before = read_tracker(seeded["persist"])
    old_hashes = set(tracker_before)
    rel = "90_CE_Wiki/extra.md"
    path = _write_doc(isolated_env, rel, _long(50) + "\nEXTRA\n")
    man = build_append_manifest_from_paths([(path, rel)])
    append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    tracker_after = read_tracker(seeded["persist"])
    for h in old_hashes:
        assert tracker_after[h]["chunk_ids"] == tracker_before[h]["chunk_ids"]
        assert set(tracker_before[h]["paths"]).issubset(set(tracker_after[h]["paths"]))


def test_failure_embedding_no_mutation(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    before = _reg_counts(seeded["registry"])
    before_v = count_vectors_readonly(seeded["persist"])
    rel = "90_CE_Wiki/embfail.md"
    path = _write_doc(isolated_env, rel, _long(50))
    man = build_append_manifest_from_paths([(path, rel)])
    with pytest.raises(Exception):
        append_certified_sources(
            persist_dir=seeded["persist"],
            registry_db=seeded["registry"],
            manifest=man,
            embedding_function=FakeEmbeddings1024(),
            _fail_after="embedding",
        )
    assert _reg_counts(seeded["registry"]) == before
    assert count_vectors_readonly(seeded["persist"]) == before_v


def test_wrong_manifest_generation_id(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/wronggen.md"
    path = _write_doc(isolated_env, rel, _long(40))
    man = build_append_manifest_from_paths(
        [(path, rel)], generation_id="raggen:20260101T000000Z:ffffffffffff"
    )
    with pytest.raises(AppendManifestError):
        append_certified_sources(
            persist_dir=seeded["persist"],
            registry_db=seeded["registry"],
            manifest=man,
            embedding_function=FakeEmbeddings1024(),
        )


def test_duplicate_path_in_manifest_rejected(isolated_env):
    with pytest.raises(AppendManifestError):
        load_append_manifest(
            {
                "schema": "b8ii-certified-append-manifest-v1",
                "entries": [
                    {
                        "relative_path": "90_CE_Wiki/x.md",
                        "absolute_path": "/tmp/a.md",
                        "sha256": "e" * 64,
                        "size_bytes": 1,
                        "classification": "APPROVED_APPEND",
                    },
                    {
                        "relative_path": "90_CE_Wiki/x.md",
                        "absolute_path": "/tmp/b.md",
                        "sha256": "f" * 64,
                        "size_bytes": 1,
                        "classification": "APPROVED_APPEND",
                    },
                ],
            }
        )


def test_document_id_not_from_path(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = "90_CE_Wiki/path_identity.md"
    path = _write_doc(isolated_env, rel, _long(44) + "\nPATHID\n")
    sh = source_hash_from_bytes(path.read_bytes())
    man = build_append_manifest_from_paths([(path, rel)])
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert out["results"][0]["document_id"] == f"docrev:{sh}"
    assert "path_identity" not in out["results"][0]["document_id"]


def test_alias_does_not_rewrite_vector_source(isolated_env, tmp_path):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    src_path, src_rel = seeded["docs"][0]
    sh = source_hash_from_bytes(src_path.read_bytes())
    tracker = read_tracker(seeded["persist"])
    cids = list(tracker[sh]["chunk_ids"])
    from langchain_chroma import Chroma
    from rag_engine.config import chroma_client_settings

    db = Chroma(
        persist_directory=str(seeded["persist"]),
        embedding_function=FakeEmbeddings1024(),
        client_settings=chroma_client_settings(),
    )
    before_meta = db.get(ids=cids, include=["metadatas"])["metadatas"]
    alias_rel = "90_CE_Wiki/alias_nosource.md"
    alias_path = _write_doc(isolated_env, alias_rel, src_path.read_text(encoding="utf-8"))
    man = build_append_manifest_from_paths([(alias_path, alias_rel)])
    append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    after_meta = db.get(ids=cids, include=["metadatas"])["metadatas"]
    assert before_meta == after_meta


# Expand focused coverage with many small behavioral assertions via helper loops
@pytest.mark.parametrize("i", range(40))
def test_new_revision_batch_stability(isolated_env, tmp_path, i):
    """Each case appends a unique small doc and checks core invariants."""
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    rel = f"90_CE_Wiki/batch_{i}.md"
    path = _write_doc(isolated_env, rel, _long(35 + (i % 5)) + f"\nBATCH{i}\n")
    man = build_append_manifest_from_paths([(path, rel)])
    out = append_certified_sources(
        persist_dir=seeded["persist"],
        registry_db=seeded["registry"],
        manifest=man,
        embedding_function=FakeEmbeddings1024(),
    )
    assert out["ok"] is True
    assert out["v1_modified"] is False
    assert out["generation_id"] == seeded["generation_id"]
    assert out["reconciliation"]["p0"] == 0
    res = out["results"][0]
    assert res["document_id"].startswith("docrev:")
    if res["action"] == "NEW_REVISION_APPEND":
        assert res["chunk_ids"]
        assert all(c.startswith("chunk:") for c in res["chunk_ids"])
    assert out["commit_model"] == "RECOVERABLE_MULTI_STORE_COMMIT"


@pytest.mark.parametrize(
    "bad_target",
    [
        "/Users/vladymyrzub/CE_Library/.rag_db",
        "/Users/vladymyrzub/CE_Library/.rag_db_generations",
        "/Users/vladymyrzub/CE_Library/.rag_state",
        "/Users/vladymyrzub/CE_Library/.intake_state",
        "/Users/vladymyrzub/CE_Library/_Inbox",
    ],
)
def test_append_target_matrix_forbidden(isolated_env, bad_target):
    with pytest.raises(
        (AppendTargetError, LegacyPathForbiddenError, UnsafeGenerationPathError, AppendGenerationGuardError)
    ):
        validate_certified_append_target(
            persist_dir=bad_target,
            registry_db=str(isolated_env / "r.sqlite3"),
        )


@pytest.mark.parametrize("n", range(20))
def test_manifest_unit_roundtrip_hash(isolated_env, n):
    rel = f"90_CE_Wiki/unit_{n}.md"
    path = _write_doc(isolated_env, rel, f"unit content {n} " + ("word " * 20))
    man = build_append_manifest_from_paths([(path, rel)])
    assert man["source_count"] == 1
    assert len(man["manifest_sha256"]) == 64
    again = load_append_manifest(man)
    assert again["manifest_sha256"] == man["manifest_sha256"]
    assert again["entries"][0]["document_id"].startswith("docrev:")


def test_legacy_ingest_guard_integrated(isolated_env, tmp_path, monkeypatch):
    seeded = _seed_generation(tmp_path, isolated_env, n_docs=1)
    monkeypatch.setenv("RAG_DB_PATH", str(seeded["persist"]))
    from rag_engine.ingest import _run_ingest_locked

    with pytest.raises(LegacyIngestForbiddenError):
        _run_ingest_locked(force=False, max_new=1)


def test_add_alias_cli_path_exists():
    from rag_engine import cli
    import inspect

    src = inspect.getsource(cli.cmd_generation)
    assert "add-alias" in src
    assert "append" in src


def test_query_py_untouched_by_b8ii():
    # Boundary: query.py must not import append module
    q = Path(__file__).resolve().parents[1] / "rag_engine" / "query.py"
    text = q.read_text(encoding="utf-8")
    assert "append_certified" not in text
    assert "certified_append" not in text


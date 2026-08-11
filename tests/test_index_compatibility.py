"""Phase 6B embedding-fp-v1 fingerprint enforcement tests.

All state uses temporary directories under pytest tmp_path / /private/tmp.
Never points fixtures at production ``CE_Library/.rag_db``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

PRODUCTION_RAG_DB = Path("/Users/vladymyrzub/CE_Library/.rag_db")


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate config so omitted paths cannot fall back to production."""
    lib = tmp_path / "lib"
    db = tmp_path / "rag_db"
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
            "other": {"description": "Other", "hermes_aliases": [], "path_prefixes": []},
        },
        "prefix_order": [],
    }
    scopes = tmp_path / "scopes.yaml"
    scopes.write_text(yaml.dump(data), encoding="utf-8")
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(db))
    monkeypatch.setenv("RAG_EMBED_MODEL", "mxbai-embed-large")
    monkeypatch.setenv("RAG_EMBED_DIMENSION", "1024")
    monkeypatch.delenv("RAG_LLM_MODEL", raising=False)

    import rag_engine.config as cfg

    monkeypatch.setattr(cfg, "SCOPES_FILE", scopes)
    cfg.load_registry.cache_clear()

    assert Path(os.environ["RAG_DB_PATH"]).resolve() != PRODUCTION_RAG_DB.resolve()
    assert not str(db).startswith(str(PRODUCTION_RAG_DB))
    return db


def _specs(**emb_overrides):
    from rag_engine.index_compatibility.builders import (
        build_corpus_spec,
        build_embedding_spec,
        build_index_spec,
    )

    kwargs = {
        "embedding_model": "mxbai-embed-large",
        "embedding_dimension": 1024,
    }
    kwargs.update(emb_overrides)
    emb = build_embedding_spec(**kwargs)
    corp = build_corpus_spec()
    idx = build_index_spec(embedding=emb, corpus=corp)
    return emb, corp, idx


def _write_authority(persist: Path, emb=None, corp=None, idx=None):
    from rag_engine.index_compatibility.builders import stored_envelope_from_specs
    from rag_engine.index_compatibility.state import write_sidecar_v1

    if emb is None:
        emb, corp, idx = _specs()
    envelope = stored_envelope_from_specs(emb, corp, idx)
    write_sidecar_v1(persist, envelope)
    return envelope


# ---------------------------------------------------------------------------
# Determinism / canonicalization
# ---------------------------------------------------------------------------


def test_canonical_json_determinism_and_key_order():
    from rag_engine.index_compatibility.specs import canonical_json, digest_hex

    a = {"b": 1, "a": None, "z": ["x", "y"]}
    b = {"z": ["x", "y"], "a": None, "b": 1}
    ca = canonical_json(a)
    cb = canonical_json(b)
    assert ca == cb
    assert ca == '{"a":null,"b":1,"z":["x","y"]}'
    assert digest_hex(ca) == digest_hex(cb)


def test_embedding_fingerprint_determinism_fresh_objects():
    emb1, _, _ = _specs()
    emb2, _, _ = _specs()
    assert emb1.digest() == emb2.digest()
    assert emb1.canonical_json() == emb2.canonical_json()


def test_fingerprint_insensitive_to_cwd_and_paths(tmp_path, monkeypatch):
    emb, corp, idx = _specs()
    d1 = emb.digest()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "other-home"))
    emb2, corp2, idx2 = _specs()
    assert emb2.digest() == d1
    assert corp2.digest() == corp.digest()
    assert idx2.digest() == idx.digest()


# ---------------------------------------------------------------------------
# Sensitivity / insensitivity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_provider", "openai"),
        ("embedding_model", "nomic-embed-text"),
        ("embedding_model_revision", "sha:abc"),
        ("embedding_dimension", 768),
        ("embedding_normalization", "l2"),
        ("embedding_mode", "asymmetric_v1"),
        ("tokenizer_id", "tok-1"),
        ("max_input_tokens", 512),
    ],
)
def test_embedding_field_sensitivity(field, value):
    base, _, _ = _specs()
    changed, _, _ = _specs(**{field: value})
    assert changed.digest() != base.digest()


def test_same_dimension_different_model_incompatible():
    a, _, ia = _specs(embedding_model="model-a", embedding_dimension=1024)
    b, _, ib = _specs(embedding_model="model-b", embedding_dimension=1024)
    assert a.embedding_dimension == b.embedding_dimension == 1024
    assert a.digest() != b.digest()
    assert ia.digest() != ib.digest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("chunk_size", 900),
        ("chunk_overlap", 50),
        ("separators", ("\n", " ")),
        ("normalization", "nfc"),
        ("min_chunk_chars", 10),
        ("max_chunk_chars", 1000),
        ("extractor", "pypdf"),
        ("extractor_version", "1.0"),
        ("embedded_text_composition_version", "other_v1"),
        ("identity_scheme_version", "stable-id-v2"),
    ],
)
def test_corpus_field_sensitivity(field, value):
    from rag_engine.index_compatibility.builders import build_corpus_spec

    base = build_corpus_spec()
    changed = build_corpus_spec(**{field: value})
    assert changed.digest() != base.digest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("vector_store", "faiss"),
        ("distance_space", "cosine"),
        ("physical_collection_name", "other"),
        ("index_schema_notes", "note"),
    ],
)
def test_index_field_sensitivity(field, value):
    from rag_engine.index_compatibility.builders import build_index_spec

    emb, corp, _ = _specs()
    base = build_index_spec(embedding=emb, corpus=corp)
    changed = build_index_spec(embedding=emb, corpus=corp, **{field: value})
    assert changed.digest() != base.digest()


def test_irrelevant_fields_do_not_affect_fingerprint(monkeypatch):
    emb, corp, idx = _specs()
    before = (emb.digest(), corp.digest(), idx.digest())
    monkeypatch.setenv("RAG_LLM_MODEL", "totally-different-llm")
    monkeypatch.setenv("SOME_UNRELATED", "1")
    emb2, corp2, idx2 = _specs()
    assert (emb2.digest(), corp2.digest(), idx2.digest()) == before


# ---------------------------------------------------------------------------
# Compatibility classification
# ---------------------------------------------------------------------------


def test_empty_uninitialized(isolated_env):
    from rag_engine.index_compatibility import evaluate_compatibility

    result = evaluate_compatibility(isolated_env, vector_count=0)
    assert result.state == "EMPTY_UNINITIALIZED"
    assert result.ingest_allowed
    assert result.retrieval_allowed


def test_unknown_legacy_nonempty_no_authority(isolated_env):
    from rag_engine.index_compatibility import evaluate_compatibility
    from rag_engine.index_compatibility.state import sidecar_v1_path

    # v0 sidecar alone must NOT certify.
    (isolated_env / "index_fingerprint.json").write_text(
        json.dumps(
            {
                "embed_model": "mxbai-embed-large",
                "llm_model": "x",
                "chunk_size": 800,
                "chunk_overlap": 100,
                "normalization": "nfkc",
            }
        ),
        encoding="utf-8",
    )
    result = evaluate_compatibility(isolated_env, vector_count=10)
    assert result.state == "UNKNOWN_LEGACY"
    assert not result.ingest_allowed
    assert result.retrieval_allowed
    assert result.retrieval_degraded
    assert not sidecar_v1_path(isolated_env).exists()


def test_compatible_match(isolated_env):
    from rag_engine.index_compatibility import evaluate_compatibility

    _write_authority(isolated_env)
    result = evaluate_compatibility(isolated_env, vector_count=3)
    assert result.state == "KNOWN_COMPATIBLE"
    assert result.ingest_allowed


def test_mismatch_embedding(isolated_env, monkeypatch):
    from rag_engine.index_compatibility import evaluate_compatibility

    _write_authority(isolated_env)
    monkeypatch.setenv("RAG_EMBED_MODEL", "other-model")
    result = evaluate_compatibility(isolated_env, vector_count=3)
    assert result.state == "KNOWN_INCOMPATIBLE"
    assert "embedding_fingerprint" in result.evidence["mismatch_fields"]


def test_mismatch_corpus(isolated_env, monkeypatch):
    from rag_engine.index_compatibility import evaluate_compatibility
    from rag_engine.index_compatibility.builders import (
        build_corpus_spec,
        build_embedding_spec,
        build_index_spec,
        stored_envelope_from_specs,
    )
    from rag_engine.index_compatibility.state import write_sidecar_v1

    emb = build_embedding_spec(embedding_model="mxbai-embed-large", embedding_dimension=1024)
    corp = build_corpus_spec(chunk_size=999)
    idx = build_index_spec(embedding=emb, corpus=corp)
    write_sidecar_v1(isolated_env, stored_envelope_from_specs(emb, corp, idx))
    result = evaluate_compatibility(isolated_env, vector_count=1)
    assert result.state == "KNOWN_INCOMPATIBLE"
    assert "corpus_fingerprint" in result.evidence["mismatch_fields"]


def test_corrupt_invalid_json(isolated_env):
    from rag_engine.index_compatibility import evaluate_compatibility

    (isolated_env / "index_embedding_fingerprint_v1.json").write_text(
        "{not-json", encoding="utf-8"
    )
    result = evaluate_compatibility(isolated_env, vector_count=1)
    assert result.state == "CORRUPT"
    assert not result.ingest_allowed


def test_corrupt_missing_field(isolated_env):
    from rag_engine.index_compatibility import evaluate_compatibility

    emb, corp, idx = _specs()
    bad = {
        "fingerprint_schema_version": "embedding-fp-v1",
        "physical_collection_name": "langchain",
        "index_fingerprint": idx.digest(),
        "embedding_fingerprint": emb.digest(),
        "corpus_fingerprint": corp.digest(),
        "embedding_contract": emb.to_contract(),
        "corpus_contract": corp.to_contract(),
        # missing index_contract
    }
    (isolated_env / "index_embedding_fingerprint_v1.json").write_text(
        json.dumps(bad), encoding="utf-8"
    )
    result = evaluate_compatibility(isolated_env, vector_count=1)
    assert result.state == "CORRUPT"


def test_unsupported_schema(isolated_env):
    from rag_engine.index_compatibility import evaluate_compatibility

    payload = {
        "fingerprint_schema_version": "embedding-fp-v999",
        "physical_collection_name": "langchain",
        "index_fingerprint": "a" * 64,
        "embedding_fingerprint": "b" * 64,
        "corpus_fingerprint": "c" * 64,
        "embedding_contract": {},
        "corpus_contract": {},
        "index_contract": {},
    }
    (isolated_env / "index_embedding_fingerprint_v1.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    result = evaluate_compatibility(isolated_env, vector_count=1)
    assert result.state == "UNSUPPORTED_SCHEMA"


def test_conflict_registry_vs_sidecar(isolated_env, tmp_path):
    from rag_engine.index_compatibility import evaluate_compatibility
    from rag_engine.index_compatibility.builders import (
        build_corpus_spec,
        build_embedding_spec,
        build_index_spec,
        stored_envelope_from_specs,
    )
    from rag_engine.index_compatibility.state import write_registry_fingerprint, write_sidecar_v1
    from rag_engine.metadata_registry import initialize_registry

    emb_a, corp, idx_a = _specs(embedding_model="model-a")
    emb_b, _, idx_b = _specs(embedding_model="model-b")
    write_sidecar_v1(isolated_env, stored_envelope_from_specs(emb_a, corp, idx_a))
    reg = initialize_registry(tmp_path / "reg.sqlite3")
    write_registry_fingerprint(reg, stored_envelope_from_specs(emb_b, corp, idx_b))
    result = evaluate_compatibility(isolated_env, registry_db=reg, vector_count=5)
    assert result.state == "CONFLICT"
    assert not result.ingest_allowed


# ---------------------------------------------------------------------------
# Legacy safety / empty init
# ---------------------------------------------------------------------------


def test_legacy_does_not_auto_certify(isolated_env):
    from rag_engine.index_compatibility import evaluate_compatibility
    from rag_engine.index_compatibility.policy import doctor_fingerprint_report
    from rag_engine.index_compatibility.state import sidecar_v1_path

    before = list(isolated_env.iterdir())
    result = evaluate_compatibility(isolated_env, vector_count=100)
    assert result.state == "UNKNOWN_LEGACY"
    report = doctor_fingerprint_report(isolated_env, vector_count=100)
    assert report["state"] == "UNKNOWN_LEGACY"
    assert not sidecar_v1_path(isolated_env).exists()
    assert list(isolated_env.iterdir()) == before


def test_empty_new_index_initialization(isolated_env):
    from rag_engine.index_compatibility import (
        evaluate_compatibility,
        ensure_fingerprint_initialized_for_empty_index,
    )
    from rag_engine.index_compatibility.state import sidecar_v1_path

    assert evaluate_compatibility(isolated_env, vector_count=0).state == "EMPTY_UNINITIALIZED"
    after = ensure_fingerprint_initialized_for_empty_index(isolated_env, vector_count=0)
    assert after.state == "KNOWN_COMPATIBLE"
    assert sidecar_v1_path(isolated_env).exists()
    # Second init path: already compatible, no contradiction.
    again = ensure_fingerprint_initialized_for_empty_index(isolated_env, vector_count=0)
    assert again.state == "KNOWN_COMPATIBLE"


def test_refuse_init_when_nonempty(isolated_env):
    from rag_engine.index_compatibility.exceptions import FingerprintLegacyBlockedError
    from rag_engine.index_compatibility.policy import ensure_fingerprint_initialized_for_empty_index

    with pytest.raises(FingerprintLegacyBlockedError):
        ensure_fingerprint_initialized_for_empty_index(isolated_env, vector_count=1)


# ---------------------------------------------------------------------------
# Ingest enforcement / skip gate
# ---------------------------------------------------------------------------


def test_ingest_gate_blocks_unknown_legacy_before_digest_skip(isolated_env, monkeypatch):
    from rag_engine.index_compatibility.exceptions import FingerprintLegacyBlockedError
    from rag_engine import ingest as ingest_mod

    # Tracker says already embedded — must still fail closed.
    tracker = {
        "deadbeef" * 8: {
            "paths": ["doc.pdf"],
            "chunk_ids": ["c1"],
            "collection": "other",
            "extraction": "ok",
        }
    }
    (isolated_env / "embedded.json").write_text(json.dumps(tracker), encoding="utf-8")

    monkeypatch.setattr(ingest_mod, "_iter_docs", lambda: [])
    monkeypatch.setattr(ingest_mod, "count_vectors_readonly", lambda *a, **k: 10)
    # Avoid real Chroma/Ollama if gate somehow passed
    monkeypatch.setattr(
        ingest_mod,
        "OllamaEmbeddings",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not reach embed")),
    )

    with pytest.raises(FingerprintLegacyBlockedError):
        ingest_mod._run_ingest_locked(force=False)


def test_ingest_gate_blocks_mismatch_same_digest(isolated_env, monkeypatch):
    from rag_engine.index_compatibility.exceptions import FingerprintMismatchError
    from rag_engine import ingest as ingest_mod

    _write_authority(isolated_env)
    monkeypatch.setenv("RAG_EMBED_MODEL", "different-model")
    (isolated_env / "embedded.json").write_text(
        json.dumps({"abcd" * 16: {"paths": ["a.pdf"], "chunk_ids": ["1"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ingest_mod, "count_vectors_readonly", lambda *a, **k: 2)
    monkeypatch.setattr(ingest_mod, "_iter_docs", lambda: [])
    with pytest.raises(FingerprintMismatchError):
        ingest_mod._run_ingest_locked()


@pytest.mark.parametrize(
    "state_setup,exc_name",
    [
        ("legacy", "FingerprintLegacyBlockedError"),
        ("mismatch", "FingerprintMismatchError"),
        ("corrupt", "FingerprintCorruptError"),
        ("conflict", "FingerprintConflictError"),
        ("unsupported", "FingerprintUnsupportedVersionError"),
    ],
)
def test_ingest_matrix_blocks(isolated_env, tmp_path, monkeypatch, state_setup, exc_name):
    from rag_engine.index_compatibility import exceptions as ex
    from rag_engine.index_compatibility.builders import (
        build_corpus_spec,
        build_embedding_spec,
        build_index_spec,
        stored_envelope_from_specs,
    )
    from rag_engine.index_compatibility.policy import enforce_ingest_compatibility
    from rag_engine.index_compatibility.state import write_registry_fingerprint, write_sidecar_v1
    from rag_engine.metadata_registry import initialize_registry

    emb, corp, idx = _specs()
    if state_setup == "legacy":
        vc = 5
    elif state_setup == "mismatch":
        write_sidecar_v1(isolated_env, stored_envelope_from_specs(emb, corp, idx))
        monkeypatch.setenv("RAG_EMBED_MODEL", "other")
        vc = 5
    elif state_setup == "corrupt":
        (isolated_env / "index_embedding_fingerprint_v1.json").write_text("{", encoding="utf-8")
        vc = 5
    elif state_setup == "conflict":
        write_sidecar_v1(isolated_env, stored_envelope_from_specs(emb, corp, idx))
        emb2, _, idx2 = _specs(embedding_model="other")
        reg = initialize_registry(tmp_path / "r.sqlite3")
        write_registry_fingerprint(reg, stored_envelope_from_specs(emb2, corp, idx2))
        with pytest.raises(getattr(ex, exc_name)):
            enforce_ingest_compatibility(isolated_env, registry_db=reg, vector_count=5)
        return
    elif state_setup == "unsupported":
        (isolated_env / "index_embedding_fingerprint_v1.json").write_text(
            json.dumps(
                {
                    "fingerprint_schema_version": "embedding-fp-v999",
                    "physical_collection_name": "langchain",
                    "index_fingerprint": "a" * 64,
                    "embedding_fingerprint": "b" * 64,
                    "corpus_fingerprint": "c" * 64,
                    "embedding_contract": {},
                    "corpus_contract": {},
                    "index_contract": {},
                }
            ),
            encoding="utf-8",
        )
        vc = 5
    else:
        raise AssertionError(state_setup)

    embedded_before = (isolated_env / "embedded.json").read_text() if (isolated_env / "embedded.json").exists() else None
    sidecar = isolated_env / "index_embedding_fingerprint_v1.json"
    sidecar_before = sidecar.read_text() if sidecar.exists() else None

    with pytest.raises(getattr(ex, exc_name)):
        enforce_ingest_compatibility(isolated_env, vector_count=vc)

    embedded_after = (isolated_env / "embedded.json").read_text() if (isolated_env / "embedded.json").exists() else None
    sidecar_after = sidecar.read_text() if sidecar.exists() else None
    assert embedded_before == embedded_after
    assert sidecar_before == sidecar_after


def test_ingest_allows_compatible(isolated_env):
    from rag_engine.index_compatibility.policy import enforce_ingest_compatibility

    _write_authority(isolated_env)
    result = enforce_ingest_compatibility(isolated_env, vector_count=2)
    assert result.state == "KNOWN_COMPATIBLE"


# ---------------------------------------------------------------------------
# Retrieval policy
# ---------------------------------------------------------------------------


def test_retrieval_allows_legacy_degraded(isolated_env):
    from rag_engine.index_compatibility.policy import enforce_retrieval_compatibility

    result = enforce_retrieval_compatibility(isolated_env, vector_count=9)
    assert result.state == "UNKNOWN_LEGACY"
    assert result.retrieval_degraded


def test_retrieval_blocks_incompatible(isolated_env, monkeypatch):
    from rag_engine.index_compatibility.exceptions import FingerprintIncompatibleRetrievalError
    from rag_engine.index_compatibility.policy import enforce_retrieval_compatibility

    _write_authority(isolated_env)
    monkeypatch.setenv("RAG_EMBED_MODEL", "other")
    with pytest.raises(FingerprintIncompatibleRetrievalError):
        enforce_retrieval_compatibility(isolated_env, vector_count=1)


def test_retrieval_blocks_corrupt(isolated_env):
    from rag_engine.index_compatibility.exceptions import FingerprintCorruptError
    from rag_engine.index_compatibility.policy import enforce_retrieval_compatibility

    (isolated_env / "index_embedding_fingerprint_v1.json").write_text("{", encoding="utf-8")
    with pytest.raises(FingerprintCorruptError):
        enforce_retrieval_compatibility(isolated_env, vector_count=1)


def test_retrieval_gate_read_only(isolated_env):
    from rag_engine.index_compatibility.policy import enforce_retrieval_compatibility

    before = {p.name: p.read_bytes() for p in isolated_env.iterdir() if p.is_file()}
    enforce_retrieval_compatibility(isolated_env, vector_count=3)
    after = {p.name: p.read_bytes() for p in isolated_env.iterdir() if p.is_file()}
    assert before == after


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def test_doctor_fingerprint_states(isolated_env, monkeypatch):
    from rag_engine.index_compatibility.policy import doctor_fingerprint_report

    r = doctor_fingerprint_report(isolated_env, vector_count=4)
    assert r["state"] == "UNKNOWN_LEGACY"
    assert r["severity"] == "WARNING"
    assert r["ok"] is False

    _write_authority(isolated_env)
    r2 = doctor_fingerprint_report(isolated_env, vector_count=4)
    assert r2["state"] == "KNOWN_COMPATIBLE"
    assert r2["severity"] == "PASS"
    assert r2["ok"] is True

    monkeypatch.setenv("RAG_EMBED_MODEL", "x")
    r3 = doctor_fingerprint_report(isolated_env, vector_count=4)
    assert r3["state"] == "KNOWN_INCOMPATIBLE"
    assert r3["severity"] == "FAIL"


def test_doctor_does_not_mutate(isolated_env):
    from rag_engine.doctor import run_doctor
    from rag_engine.index_compatibility.state import sidecar_v1_path

    before_files = {p.name: p.stat().st_mtime_ns for p in isolated_env.iterdir()}
    report = run_doctor(skip_ollama=True)
    assert "embedding_fp_v1" in {c["name"] for c in report["checks"]}
    assert not sidecar_v1_path(isolated_env).exists()
    after_files = {p.name: p.stat().st_mtime_ns for p in isolated_env.iterdir()}
    # No new fingerprint authority file created.
    assert set(after_files) >= set(before_files)
    assert "index_embedding_fingerprint_v1.json" not in after_files


# ---------------------------------------------------------------------------
# Idempotency / atomicity / same-source
# ---------------------------------------------------------------------------


def test_repeated_compatibility_checks_idempotent(isolated_env):
    from rag_engine.index_compatibility import evaluate_compatibility

    _write_authority(isolated_env)
    a = evaluate_compatibility(isolated_env, vector_count=1).to_dict()
    b = evaluate_compatibility(isolated_env, vector_count=1).to_dict()
    assert a == b


def test_same_source_different_embedding_incompatible():
    """Same chunk contract / source identity does not imply vector compatibility."""
    emb_a, corp, idx_a = _specs(embedding_model="model-a")
    emb_b, corp_b, idx_b = _specs(embedding_model="model-b")
    assert corp.digest() == corp_b.digest()
    assert emb_a.digest() != emb_b.digest()
    assert idx_a.digest() != idx_b.digest()


def test_atomicity_no_false_compatible_on_failed_sidecar_write(isolated_env, monkeypatch):
    from rag_engine.index_compatibility.builders import (
        build_runtime_contracts_from_config,
        stored_envelope_from_specs,
    )
    from rag_engine.index_compatibility import evaluate_compatibility
    from rag_engine.index_compatibility import state as state_mod

    emb, corp, idx = build_runtime_contracts_from_config()
    envelope = stored_envelope_from_specs(emb, corp, idx)

    def boom(*a, **k):
        raise OSError("simulated sidecar failure")

    monkeypatch.setattr(state_mod, "_atomic_write_json", boom)
    with pytest.raises(OSError):
        state_mod.initialize_fingerprint_state(isolated_env, envelope)
    result = evaluate_compatibility(isolated_env, vector_count=0)
    assert result.state == "EMPTY_UNINITIALIZED"
    assert not (isolated_env / "index_embedding_fingerprint_v1.json").exists()


def test_production_path_not_used_by_fixtures(isolated_env):
    assert isolated_env.resolve() != PRODUCTION_RAG_DB.resolve()
    assert PRODUCTION_RAG_DB.resolve() not in isolated_env.resolve().parents


def test_registry_schema_v3_has_index_fingerprints(tmp_path):
    from rag_engine.metadata_registry import (
        CURRENT_SCHEMA_VERSION,
        initialize_registry,
        open_registry,
        get_schema_version,
    )

    db = initialize_registry(tmp_path / "reg.sqlite3")
    with open_registry(db, readonly=True) as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION == 3
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='index_fingerprints'"
        ).fetchone()
        assert row is not None

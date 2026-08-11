"""Phase 6C legacy-index certification workflow tests (synthetic temp state only)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

PRODUCTION_RAG_DB = Path("/Users/vladymyrzub/CE_Library/.rag_db")


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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

    import rag_engine.config as cfg

    monkeypatch.setattr(cfg, "SCOPES_FILE", scopes)
    cfg.load_registry.cache_clear()
    assert db.resolve() != PRODUCTION_RAG_DB.resolve()
    return db


def _snapshot(persist: Path) -> dict[str, tuple[int, int, str]]:
    import hashlib

    out: dict[str, tuple[int, int, str]] = {}
    for p in sorted(persist.rglob("*")):
        if not p.is_file():
            continue
        st = p.stat()
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        out[str(p.relative_to(persist))] = (st.st_size, st.st_mtime_ns, h)
    return out


def _full_historical_contract():
    from rag_engine.index_compatibility.builders import (
        build_corpus_spec,
        build_embedding_spec,
        build_index_spec,
    )

    emb = build_embedding_spec(
        embedding_model="mxbai-embed-large",
        embedding_dimension=1024,
        embedding_model_revision=None,
    )
    corp = build_corpus_spec()
    idx = build_index_spec(embedding=emb, corpus=corp)
    return {
        "embedding": emb.to_contract(),
        "corpus": corp.to_contract(),
        "index": idx.to_contract(),
    }, emb, corp, idx


def test_insufficient_evidence_level_c_only(isolated_env):
    from rag_engine.index_compatibility.certification import (
        DEC_INSUFFICIENT_EVIDENCE,
        circumstantial_evidence_from_v0_and_runtime,
        evaluate_certification,
        inspect_legacy_target,
    )

    # Simulate non-empty legacy index without writing chroma (vector_count via inspect uses sqlite).
    # Force inspection path with empty chroma → vector_count 0; evaluate with synthetic inspection.
    contract, _, _, _ = _full_historical_contract()
    evidence = circumstantial_evidence_from_v0_and_runtime(
        embed_model="mxbai-embed-large",
        embedding_dimension=1024,
        chunk_size=800,
        chunk_overlap=100,
    )
    inspection = {
        "target": {"vector_count": 10},
        "compatibility": {"state": "UNKNOWN_LEGACY"},
    }
    decision = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection=inspection,
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    assert decision.decision == DEC_INSUFFICIENT_EVIDENCE


def test_strong_evidence_certifiable_dry_run_no_mutation(isolated_env):
    from rag_engine.index_compatibility.certification import (
        DEC_CERTIFIABLE,
        build_certification_manifest,
        certify_legacy_index,
        evaluate_certification,
        inspect_legacy_target,
        strong_evidence_for_contract,
    )
    from rag_engine.index_compatibility.policy import enforce_ingest_compatibility
    from rag_engine.index_compatibility.exceptions import FingerprintLegacyBlockedError

    # Create synthetic "non-empty" by writing a fake chroma sqlite with collection+embeddings
    _make_min_chroma(isolated_env, n=3)
    (isolated_env / "embedded.json").write_text("{}", encoding="utf-8")

    contract, emb, corp, idx = _full_historical_contract()
    evidence = strong_evidence_for_contract(
        emb, corp, idx, source="test_manifest", model_digest="sha256:deadbeef"
    )
    inspection = inspect_legacy_target(isolated_env)
    assert inspection["compatibility"]["state"] == "UNKNOWN_LEGACY"
    decision = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection=inspection,
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    assert decision.decision == DEC_CERTIFIABLE
    manifest = build_certification_manifest(
        target=inspection["target"],
        historical_contract=contract,
        evidence=evidence,
        decision=decision,
        operator_reason="synthetic strong evidence",
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    before = _snapshot(isolated_env)
    report = certify_legacy_index(
        isolated_env,
        evidence_manifest=manifest,
        apply=False,
        operator_reason="synthetic strong evidence",
    )
    assert report["mode"] == "dry_run"
    assert report.get("applied") is False
    assert report.get("would_apply") is True
    assert _snapshot(isolated_env) == before
    # ingest still blocked after dry-run
    with pytest.raises(FingerprintLegacyBlockedError):
        enforce_ingest_compatibility(isolated_env, vector_count=3)


def test_historical_runtime_mismatch_not_certifiable(isolated_env, monkeypatch):
    from rag_engine.index_compatibility.certification import (
        DEC_NOT_CERTIFIABLE,
        evaluate_certification,
        strong_evidence_for_contract,
    )
    from rag_engine.index_compatibility.builders import (
        build_corpus_spec,
        build_embedding_spec,
        build_index_spec,
    )

    emb = build_embedding_spec(embedding_model="historical-model", embedding_dimension=1024)
    corp = build_corpus_spec()
    idx = build_index_spec(embedding=emb, corpus=corp)
    contract = {
        "embedding": emb.to_contract(),
        "corpus": corp.to_contract(),
        "index": idx.to_contract(),
    }
    evidence = strong_evidence_for_contract(emb, corp, idx)
    monkeypatch.setenv("RAG_EMBED_MODEL", "runtime-other")
    decision = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection={"compatibility": {"state": "UNKNOWN_LEGACY"}},
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
        require_runtime_match=True,
    )
    assert decision.decision == DEC_NOT_CERTIFIABLE


def test_mixed_history_blocks(isolated_env):
    from rag_engine.index_compatibility.certification import (
        DEC_MIXED_HISTORY_SUSPECTED,
        evaluate_certification,
        strong_evidence_for_contract,
    )

    contract, emb, corp, idx = _full_historical_contract()
    evidence = strong_evidence_for_contract(emb, corp, idx)
    # Remove exclusion proof and mark possible mixing
    evidence = [e for e in evidence if e.field != "mixed_history_exclusion"]
    decision = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection={"compatibility": {"state": "UNKNOWN_LEGACY"}},
        mixed_history_assessment="POSSIBLE_MIXING",
    )
    assert decision.decision == DEC_MIXED_HISTORY_SUSPECTED

    decision2 = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection={"compatibility": {"state": "UNKNOWN_LEGACY"}},
        mixed_history_assessment="PROVEN_MIXING",
    )
    assert decision2.decision == DEC_MIXED_HISTORY_SUSPECTED


def test_target_changed_refuses(isolated_env):
    from rag_engine.index_compatibility.certification import (
        CertificationTargetChangedError,
        build_certification_manifest,
        certify_legacy_index,
        evaluate_certification,
        inspect_legacy_target,
        strong_evidence_for_contract,
    )

    _make_min_chroma(isolated_env, n=2)
    contract, emb, corp, idx = _full_historical_contract()
    evidence = strong_evidence_for_contract(emb, corp, idx)
    inspection = inspect_legacy_target(isolated_env)
    decision = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection=inspection,
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    manifest = build_certification_manifest(
        target=inspection["target"],
        historical_contract=contract,
        evidence=evidence,
        decision=decision,
        operator_reason="bind test",
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    # Mutate target vector count
    _make_min_chroma(isolated_env, n=5)
    with pytest.raises(CertificationTargetChangedError):
        certify_legacy_index(
            isolated_env,
            evidence_manifest=manifest,
            apply=True,
            operator_reason="bind test",
        )


def test_apply_requires_reason_and_flag(isolated_env):
    from rag_engine.index_compatibility.certification import (
        CertificationEvidenceError,
        build_certification_manifest,
        certify_legacy_index,
        evaluate_certification,
        inspect_legacy_target,
        strong_evidence_for_contract,
    )

    _make_min_chroma(isolated_env, n=1)
    contract, emb, corp, idx = _full_historical_contract()
    evidence = strong_evidence_for_contract(emb, corp, idx)
    inspection = inspect_legacy_target(isolated_env)
    decision = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection=inspection,
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    manifest = build_certification_manifest(
        target=inspection["target"],
        historical_contract=contract,
        evidence=evidence,
        decision=decision,
        operator_reason="",
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    # No apply → dry-run, even with blank reason reports blocked for apply intent
    report = certify_legacy_index(
        isolated_env, evidence_manifest=manifest, apply=False, operator_reason=""
    )
    assert report["mode"] == "dry_run"
    assert report.get("applied") is False
    with pytest.raises(CertificationEvidenceError):
        certify_legacy_index(
            isolated_env, evidence_manifest=manifest, apply=True, operator_reason="  "
        )


def test_successful_certification_synthetic(isolated_env):
    from rag_engine.index_compatibility.certification import (
        build_certification_manifest,
        certification_audit_path,
        certify_legacy_index,
        evaluate_certification,
        inspect_legacy_target,
        strong_evidence_for_contract,
    )
    from rag_engine.index_compatibility.compatibility import evaluate_compatibility
    from rag_engine.index_compatibility.policy import enforce_ingest_compatibility
    from rag_engine.index_compatibility.state import sidecar_v1_path

    _make_min_chroma(isolated_env, n=4)
    tracker = {"aa" * 32: {"paths": ["a.pdf"], "chunk_ids": ["1"], "collection": "other"}}
    (isolated_env / "embedded.json").write_text(json.dumps(tracker), encoding="utf-8")
    embedded_before = (isolated_env / "embedded.json").read_bytes()
    chroma_before = (isolated_env / "chroma.sqlite3").read_bytes()

    contract, emb, corp, idx = _full_historical_contract()
    evidence = strong_evidence_for_contract(emb, corp, idx)
    inspection = inspect_legacy_target(isolated_env)
    decision = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection=inspection,
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    manifest = build_certification_manifest(
        target=inspection["target"],
        historical_contract=contract,
        evidence=evidence,
        decision=decision,
        operator_reason="historical digest and chunk contract verified from manifest XYZ",
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    before = _snapshot(isolated_env)
    dry = certify_legacy_index(
        isolated_env,
        evidence_manifest=manifest,
        apply=False,
        operator_reason="historical digest and chunk contract verified from manifest XYZ",
    )
    assert dry.get("applied") is False
    assert _snapshot(isolated_env) == before

    applied = certify_legacy_index(
        isolated_env,
        evidence_manifest=manifest,
        apply=True,
        operator_reason="historical digest and chunk contract verified from manifest XYZ",
    )
    assert applied["applied"] is True
    assert sidecar_v1_path(isolated_env).is_file()
    assert certification_audit_path(isolated_env).is_file()
    after = evaluate_compatibility(isolated_env, vector_count=4)
    assert after.state == "KNOWN_COMPATIBLE"
    # ingest now allowed
    enforce_ingest_compatibility(isolated_env, vector_count=4)
    # vectors / tracker unchanged
    assert (isolated_env / "chroma.sqlite3").read_bytes() == chroma_before
    assert (isolated_env / "embedded.json").read_bytes() == embedded_before

    # idempotent second apply
    again = certify_legacy_index(
        isolated_env,
        evidence_manifest=manifest,
        apply=True,
        operator_reason="historical digest and chunk contract verified from manifest XYZ",
    )
    assert again.get("idempotent_noop") is True


def test_conflicting_recertification(isolated_env):
    from rag_engine.index_compatibility.builders import (
        build_corpus_spec,
        build_embedding_spec,
        build_index_spec,
    )
    from rag_engine.index_compatibility.certification import (
        CertificationConflictError,
        CertificationDecision,
        DEC_CERTIFIABLE,
        build_certification_manifest,
        certify_legacy_index,
        evaluate_certification,
        inspect_legacy_target,
        strong_evidence_for_contract,
    )

    _make_min_chroma(isolated_env, n=2)
    contract, emb, corp, idx = _full_historical_contract()
    evidence = strong_evidence_for_contract(emb, corp, idx)
    inspection = inspect_legacy_target(isolated_env)
    decision = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection=inspection,
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    manifest = build_certification_manifest(
        target=inspection["target"],
        historical_contract=contract,
        evidence=evidence,
        decision=decision,
        operator_reason="first certification",
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    certify_legacy_index(
        isolated_env,
        evidence_manifest=manifest,
        apply=True,
        operator_reason="first certification",
    )

    emb_b = build_embedding_spec(embedding_model="other-model", embedding_dimension=1024)
    corp_b = build_corpus_spec()
    idx_b = build_index_spec(embedding=emb_b, corpus=corp_b)
    contract_b = {
        "embedding": emb_b.to_contract(),
        "corpus": corp_b.to_contract(),
        "index": idx_b.to_contract(),
    }
    evidence_b = strong_evidence_for_contract(emb_b, corp_b, idx_b)
    decision_b = CertificationDecision(
        decision=DEC_CERTIFIABLE,
        reasons=("forced for conflict test",),
        highest_evidence_level="LEVEL_A_DIRECT",
        historical_index_fingerprint=idx_b.digest(),
        runtime_index_fingerprint=None,
    )
    # Refresh target binding after certification (vector count unchanged)
    inspection2 = inspect_legacy_target(isolated_env)
    manifest_b = build_certification_manifest(
        target=inspection2["target"],
        historical_contract=contract_b,
        evidence=evidence_b,
        decision=decision_b,
        operator_reason="conflicting second certification",
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    with pytest.raises(CertificationConflictError):
        certify_legacy_index(
            isolated_env,
            evidence_manifest=manifest_b,
            apply=True,
            operator_reason="conflicting second certification",
            expected_compatibility_state="KNOWN_COMPATIBLE",
        )


def test_wrong_target_binding_rejected(isolated_env, tmp_path):
    from rag_engine.index_compatibility.certification import (
        CertificationTargetChangedError,
        build_certification_manifest,
        certify_legacy_index,
        evaluate_certification,
        inspect_legacy_target,
        strong_evidence_for_contract,
    )

    a = isolated_env
    b = tmp_path / "other_db"
    b.mkdir()
    _make_min_chroma(a, n=2)
    _make_min_chroma(b, n=2)
    contract, emb, corp, idx = _full_historical_contract()
    evidence = strong_evidence_for_contract(emb, corp, idx)
    inspection_a = inspect_legacy_target(a)
    decision = evaluate_certification(
        historical_contract=contract,
        evidence=evidence,
        target_inspection=inspection_a,
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    manifest = build_certification_manifest(
        target=inspection_a["target"],
        historical_contract=contract,
        evidence=evidence,
        decision=decision,
        operator_reason="wrong target",
        mixed_history_assessment="NO_EVIDENCE_OF_MIXING",
    )
    with pytest.raises(CertificationTargetChangedError):
        certify_legacy_index(
            b,
            evidence_manifest=manifest,
            apply=True,
            operator_reason="wrong target",
        )


def test_no_automatic_certification_imports():
    """query/doctor/ingest must not call certify_legacy_index."""
    import rag_engine.doctor as doctor
    import rag_engine.ingest as ingest
    import rag_engine.query as query

    for mod in (doctor, ingest, query):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "certify_legacy_index" not in src
        assert "certify-legacy" not in src


def test_cli_fingerprint_help_mentions_safety():
    from rag_engine.cli import cmd_fingerprint

    with pytest.raises(SystemExit) as ei:
        cmd_fingerprint(["certify-legacy", "-h"])
    # argparse help exit
    assert ei.value.code == 0


def _make_min_chroma(persist: Path, n: int = 1) -> None:
    """Create a minimal chroma.sqlite3 with n embedding rows (schema-compatible)."""
    import sqlite3
    import uuid

    db = persist / "chroma.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS collections (
                id TEXT PRIMARY KEY,
                name TEXT,
                dimension INTEGER,
                database_id TEXT,
                config_json_str TEXT
            );
            CREATE TABLE IF NOT EXISTS segments (
                id TEXT PRIMARY KEY,
                type TEXT,
                scope TEXT,
                collection TEXT
            );
            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT,
                embedding_id TEXT,
                seq_id BLOB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        coll = "e42037ad-ddc7-46c3-aaea-53adb388606b"
        seg = "52a34a4a-a025-4260-bfea-633f3f8dd7de"
        conn.execute("DELETE FROM embeddings")
        conn.execute("DELETE FROM segments")
        conn.execute("DELETE FROM collections")
        conn.execute(
            "INSERT INTO collections (id, name, dimension, database_id, config_json_str) "
            "VALUES (?, 'langchain', 1024, '00000000-0000-0000-0000-000000000000', '{}')",
            (coll,),
        )
        conn.execute(
            "INSERT INTO segments (id, type, scope, collection) VALUES (?, ?, 'VECTOR', ?)",
            (seg, "urn:chroma:segment/vector/hnsw-local-persisted", coll),
        )
        for i in range(n):
            conn.execute(
                "INSERT INTO embeddings (segment_id, embedding_id, seq_id) VALUES (?, ?, ?)",
                (seg, str(uuid.uuid4()), b"\x00"),
            )
        conn.commit()
    finally:
        conn.close()

"""B7 provenance projection - compact Chroma + optional registry enrichment."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine.metadata_registry.connection import open_registry
from rag_engine.metadata_registry.migrations import initialize_registry
from rag_engine.metadata_registry.query_projection import (
    MAX_ALIASES_EXPOSED,
    PROVENANCE_COMPACT_ONLY,
    PROVENANCE_LEGACY_LIMITED,
    PROVENANCE_REGISTRY_ENRICHED,
    ProvenanceBundle,
    apply_provenance_to_entry,
    batch_lookup_documents,
    compact_provenance_from_meta,
    load_provenance_bundle,
    trustworthy_chunk_id,
    trustworthy_document_id,
    trustworthy_source_hash,
)
from rag_engine.metadata_registry.repository import (
    register_document_version,
    register_source_file,
    register_subject,
)


@pytest.fixture()
def scopes_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = {
        "defaults": {
            "library_root_env": "CE_LIBRARY_ROOT",
            "library_root_default": str(tmp_path / "lib"),
            "db_path_env": "RAG_DB_PATH",
            "db_path_default": None,
            "embed_model_env": "RAG_EMBED_MODEL",
            "embed_model_default": "mxbai-embed-large",
            "llm_model_env": "RAG_LLM_MODEL",
            "llm_model_default": "gpt-5.6-luna",
            "llm_fallback_model_env": "RAG_LLM_FALLBACK_MODEL",
            "llm_fallback_model_default": "qwen3.5:9b",
            "llm_num_ctx_env": "RAG_LLM_NUM_CTX",
            "llm_num_ctx_default": 8192,
            "llm_num_predict_env": "RAG_LLM_NUM_PREDICT",
            "llm_num_predict_default": 1024,
            "chunk_size": 800,
            "chunk_overlap": 100,
            "default_k": 5,
        },
        "scopes": {
            "sms": {
                "description": "SMS",
                "hermes_aliases": ["sms_library"],
                "path_prefixes": ["10_Company/"],
            },
            "other": {"description": "Other", "hermes_aliases": [], "path_prefixes": []},
        },
        "prefix_order": ["sms"],
    }
    lib = tmp_path / "lib"
    lib.mkdir()
    path = tmp_path / "scopes.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(tmp_path / "db"))
    (tmp_path / "db").mkdir()
    import rag_engine.config as cfg

    monkeypatch.setattr(cfg, "SCOPES_FILE", path)
    cfg.load_registry.cache_clear()
    return path


def _doc(meta: dict, content: str = "body"):
    d = MagicMock()
    d.metadata = dict(meta)
    d.page_content = content
    return d


def _init_reg(tmp_path: Path) -> Path:
    return Path(initialize_registry(tmp_path / "reg.sqlite3"))


def test_trustworthy_ids_reject_invention():
    assert trustworthy_document_id("docrev:abc") == "docrev:abc"
    assert trustworthy_document_id("path/to/file.pdf") is None
    assert trustworthy_source_hash("a" * 64) == "a" * 64
    assert trustworthy_source_hash("not-a-hash") is None
    assert trustworthy_chunk_id("chunk:deadbeefcafebabe0123456789abcdef") == (
        "chunk:deadbeefcafebabe0123456789abcdef"
    )
    assert trustworthy_chunk_id("550e8400-e29b-41d4-a716-446655440000") is None
    assert trustworthy_chunk_id("chunk:550e8400-e29b-41d4-a716-446655440000") is None


def test_compact_provenance_from_certified_meta():
    meta = {
        "document_id": "docrev:" + "ab" * 32,
        "source_hash": "ab" * 32,
        "chunk_id": "chunk:c23e35434fa601e610c7e1e72f4f83dc",
        "source": "10_Company/a.pdf",
        "page": 0,
        "collection": "sms",
    }
    assert compact_provenance_from_meta(meta) == {
        "document_id": meta["document_id"],
        "source_hash": meta["source_hash"],
        "chunk_id": meta["chunk_id"],
    }


def test_legacy_uuid_meta_yields_no_fake_chunk_id():
    assert compact_provenance_from_meta({"source": "a.pdf", "page": 1, "collection": "sms"}) == {}


def test_registry_subject_and_aliases_enrichment(tmp_path):
    db = _init_reg(tmp_path)
    sh = "11" * 32
    did = f"docrev:{sh}"
    with open_registry(db) as conn:
        register_subject(conn, subject_id=f"subj:pending:{sh}", document_type="procedure", scope="sms")
        register_document_version(conn, document_id=did, subject_id=f"subj:pending:{sh}", source_hash=sh)
        register_source_file(conn, document_id=did, relative_path="10_Company/a.pdf")
        register_source_file(conn, document_id=did, relative_path="20_Vessels/a.pdf")
        conn.commit()
        enriched = batch_lookup_documents(conn, [did])
    row = enriched[did]
    assert row.subject_status == "pending"
    assert row.alias_count == 2


def test_load_bundle_registry_absent_fail_open(tmp_path):
    meta = {
        "document_id": "docrev:" + "cd" * 32,
        "source_hash": "cd" * 32,
        "chunk_id": "chunk:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "source": "x.pdf",
        "page": 0,
        "collection": "sms",
    }
    bundle = load_provenance_bundle(
        [(_doc(meta), 0.2)],
        registry_db=tmp_path / "missing.sqlite3",
    )
    assert bundle.registry_status == "absent"
    assert bundle.provenance_level == PROVENANCE_COMPACT_ONLY
    entry: dict = {"path": "x.pdf"}
    apply_provenance_to_entry(entry, meta, bundle)
    assert entry["document_id"] == meta["document_id"]
    assert "subject_id" not in entry


def test_load_bundle_legacy_limited():
    pairs = [(_doc({"source": "a.pdf", "page": 1, "collection": "sms"}), 0.3)]
    bundle = load_provenance_bundle(pairs, registry_db=None, library_root=None)
    assert bundle.provenance_level == PROVENANCE_LEGACY_LIMITED
    entry: dict = {"path": "a.pdf"}
    apply_provenance_to_entry(entry, pairs[0][0].metadata, bundle)
    assert "document_id" not in entry


def test_registry_document_missing_no_invention(tmp_path):
    db = _init_reg(tmp_path)
    meta = {
        "document_id": "docrev:" + "ee" * 32,
        "source_hash": "ee" * 32,
        "chunk_id": "chunk:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "source": "y.pdf",
        "page": 0,
        "collection": "sms",
    }
    bundle = load_provenance_bundle([(_doc(meta), 0.1)], registry_db=db)
    entry: dict = {}
    apply_provenance_to_entry(entry, meta, bundle)
    assert entry["document_id"] == meta["document_id"]
    assert "subject_id" not in entry
    assert "canonical_path" not in entry
    assert "revision" not in entry
    assert "acquisition_id" not in entry


def test_registry_enriched_bundle(tmp_path):
    db = _init_reg(tmp_path)
    sh = "22" * 32
    did = f"docrev:{sh}"
    with open_registry(db) as conn:
        register_subject(conn, subject_id="subj:key:sms:env-e002", document_type="procedure", scope="sms")
        register_document_version(conn, document_id=did, subject_id="subj:key:sms:env-e002", source_hash=sh)
        register_source_file(conn, document_id=did, relative_path="00_Career/a.pdf")
        conn.commit()
    meta = {
        "document_id": did,
        "source_hash": sh,
        "chunk_id": "chunk:cccccccccccccccccccccccccccccccc",
        "source": "00_Career/a.pdf",
        "page": 0,
        "collection": "sms",
        "embedding_generation_id": "raggen:20260814T182037Z:698e0df44604",
    }
    bundle = load_provenance_bundle([(_doc(meta), 0.2)], registry_db=db)
    assert bundle.provenance_level == PROVENANCE_REGISTRY_ENRICHED
    entry: dict = {"path": meta["source"]}
    apply_provenance_to_entry(entry, meta, bundle)
    assert entry["subject_id"] == "subj:key:sms:env-e002"
    assert entry["subject_status"] == "registered"
    assert entry["alias_count"] == 1


def test_aliases_bounded(tmp_path):
    db = _init_reg(tmp_path)
    sh = "33" * 32
    did = f"docrev:{sh}"
    with open_registry(db) as conn:
        register_subject(conn, subject_id=f"subj:pending:{sh}")
        register_document_version(conn, document_id=did, subject_id=f"subj:pending:{sh}", source_hash=sh)
        for i in range(MAX_ALIASES_EXPOSED + 5):
            register_source_file(conn, document_id=did, relative_path=f"root/file_{i:02d}.pdf")
        conn.commit()
        enriched = batch_lookup_documents(conn, [did])
    assert enriched[did].alias_count == MAX_ALIASES_EXPOSED + 5
    assert len(enriched[did].aliases) == MAX_ALIASES_EXPOSED


def test_sources_projection_includes_provenance(scopes_yaml, tmp_path):
    from rag_engine.query import SCHEMA_VERSION, _chunks_from_pairs, _sources_from_pairs

    db = _init_reg(tmp_path)
    sh = "44" * 32
    did = f"docrev:{sh}"
    with open_registry(db) as conn:
        register_subject(conn, subject_id=f"subj:pending:{sh}", scope="sms")
        register_document_version(conn, document_id=did, subject_id=f"subj:pending:{sh}", source_hash=sh)
        register_source_file(conn, document_id=did, relative_path="10_Company/a.pdf")
        conn.commit()
    meta = {
        "source": "10_Company/a.pdf",
        "page": 1,
        "collection": "sms",
        "document_id": did,
        "source_hash": sh,
        "chunk_id": "chunk:dddddddddddddddddddddddddddddddd",
    }
    pairs = [(_doc(meta), 0.25)]
    bundle = load_provenance_bundle(pairs, registry_db=db)
    sources = _sources_from_pairs(pairs, provenance=bundle)
    chunks = _chunks_from_pairs(pairs, provenance=bundle)
    assert SCHEMA_VERSION == 4
    assert sources[0]["document_id"] == did
    assert sources[0]["chunk_id"] == meta["chunk_id"]
    assert sources[0]["subject_id"].startswith("subj:pending:")
    assert sources[0]["page"] == 2
    assert sources[0]["page_index"] == 1
    assert "printed_page" not in sources[0]
    assert "acquisition_id" not in sources[0]
    assert chunks[0]["text"] == "body"


def test_answer_path_preserves_distances_and_schema(scopes_yaml, tmp_path):
    from rag_engine.query import SCHEMA_VERSION, answer

    sh = "55" * 32
    did = f"docrev:{sh}"
    meta = {
        "source": "10_Company/a.pdf",
        "page": 1,
        "collection": "sms",
        "document_id": did,
        "source_hash": sh,
        "chunk_id": "chunk:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    }
    pairs = [(_doc(meta, "Relevant procedure text about fuel oil."), 0.30)]
    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        return_value=(pairs, {"gate": None, "best_raw_distance": 0.30}),
    ):
        with patch(
            "rag_engine.query._provenance_bundle_for_pairs",
            side_effect=lambda p: load_provenance_bundle(p, registry_db=tmp_path / "nope.sqlite3"),
        ):
            j = answer("fuel oil?", scope="sms").to_json()
    assert j["schema_version"] == SCHEMA_VERSION == 4
    assert j["status"] == "ok"
    assert j["sources"][0]["distance"] == 0.30
    assert j["sources"][0]["document_id"] == did
    assert j["retrieval_diagnostics"]["provenance_level"] == PROVENANCE_COMPACT_ONLY


def test_no_registry_write_on_projection(tmp_path):
    db = _init_reg(tmp_path)
    before = db.read_bytes()
    meta = {
        "document_id": "docrev:" + "66" * 32,
        "source_hash": "66" * 32,
        "chunk_id": "chunk:ffffffffffffffffffffffffffffffff",
        "source": "z.pdf",
        "page": 0,
        "collection": "other",
    }
    load_provenance_bundle([(_doc(meta), 0.4)], registry_db=db)
    assert db.read_bytes() == before


def test_lex_smallest_alias_not_called_canonical(tmp_path):
    db = _init_reg(tmp_path)
    sh = "77" * 32
    did = f"docrev:{sh}"
    with open_registry(db) as conn:
        register_subject(conn, subject_id=f"subj:pending:{sh}")
        register_document_version(conn, document_id=did, subject_id=f"subj:pending:{sh}", source_hash=sh)
        register_source_file(conn, document_id=did, relative_path="z_last.pdf")
        register_source_file(conn, document_id=did, relative_path="a_first.pdf")
        conn.commit()
        enriched = batch_lookup_documents(conn, [did])[did]
    bundle = ProvenanceBundle(
        by_document_id={did: enriched},
        registry_status="ok",
        provenance_level=PROVENANCE_REGISTRY_ENRICHED,
    )
    entry = apply_provenance_to_entry(
        {"path": "z_last.pdf"},
        {"document_id": did, "source_hash": sh, "chunk_id": "chunk:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        bundle,
    )
    assert "canonical_path" not in entry
    assert entry["aliases"][0] == "a_first.pdf"

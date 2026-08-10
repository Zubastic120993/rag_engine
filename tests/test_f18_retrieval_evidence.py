"""F-18: `sources: []` was emitted on any non-"ok" status, making "retrieval
found nothing" and "retrieval found weak matches" harder to distinguish.
Fixed additively with `retrieval_evidence` + `gate`.

ORCH_104: model-declined (`NOT_IN_CONTEXT`) no longer exists on the ask path.
Weak evidence that fails the final confidence gate remains distinguishable
from true zero-retrieval via `retrieval_evidence` + `gate`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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
            "vessels": {
                "description": "Vessels",
                "hermes_aliases": [],
                "path_prefixes": ["20_Vessels/"],
            },
            "other": {"description": "Other", "hermes_aliases": [], "path_prefixes": []},
        },
        "prefix_order": ["sms", "vessels"],
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


def _fake_doc(path="10_Company/a.pdf", page=1, collection="sms", content="Relevant text."):
    doc = MagicMock()
    doc.metadata = {"source": path, "page": page, "collection": collection}
    doc.page_content = content
    return doc


def _patch_retrieval(pairs, gate=None):
    diag = {"gate": gate}
    if pairs:
        diag["best_raw_distance"] = min(float(distance) for _doc, distance in pairs)
    return patch("rag_engine.query.retrieve_with_scores_and_diagnostics", return_value=(pairs, diag))


CORE_FIELDS = (
    "schema_version",
    "status",
    "query",
    "requested_scope",
    "resolved_scope",
    "coverage",
    "answer",
    "missing_information",
    "sources",
    "timings",
    "model",
    "scope",
)


def test_final_confidence_failed_carries_populated_evidence(scopes_yaml):
    """Weak retrieval that fails the confidence gate: sources empty, evidence kept."""
    from rag_engine.query import answer

    pairs = [
        (
            _fake_doc(
                path="00_Career/03_Engine_Knowledge/Training/guide.pdf",
                collection="maker-manuals",
            ),
            0.52,
        )
    ]
    diagnostics = {
        "gate": "final_confidence_failed",
        "score_floor": 0.38,
        "best_raw_distance": 0.52,
        "raw_count": 1,
        "post_admissibility_count": 1,
        "post_scope_count": 1,
        "post_rerank_count": 1,
        "post_dedupe_count": 1,
        "final_retained_count": 0,
        "final_confidence_passed": False,
    }
    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        return_value=(pairs, diagnostics),
    ):
        r = answer("q", scope="sms")

    assert r.status == "no_coverage"
    assert r.sources == []
    assert r.retrieval_evidence != []
    assert r.retrieval_evidence[0]["path"] == "00_Career/03_Engine_Knowledge/Training/guide.pdf"
    assert r.gate == "final_confidence_failed"

    j = r.to_json()
    assert j["sources"] == []
    assert j["retrieval_evidence"] == r.retrieval_evidence
    assert j["gate"] == "final_confidence_failed"


def test_true_zero_retrieval_has_empty_evidence_and_distinct_gate(scopes_yaml):
    from rag_engine.query import answer

    with _patch_retrieval([]):
        r = answer("q", scope="vessels")

    assert r.status == "no_coverage"
    assert r.sources == []
    assert r.retrieval_evidence == []
    assert r.gate == "no_retrieval"


def test_ok_result_core_fields_and_retrieval_package(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with patch("rag_engine.query._invoke_generation") as llm:
            r = answer("fuel oil?", scope="sms", scope_resolution_s=0.01)
            llm.assert_not_called()

    j = r.to_json()
    core = {k: j[k] for k in CORE_FIELDS}

    assert core["status"] == "ok"
    assert core["coverage"] == "full"
    assert core["answer"] is None
    assert core["missing_information"] is None
    assert core["sources"] == [
        {
            "path": "10_Company/a.pdf",
            "page": 2,
            "page_index": 1,
            "collection": "sms",
            "distance": 0.4,
            "authority_rank": 2,
            "machine_transcribed": False,
        }
    ]
    assert core["model"] is None
    assert core["scope"] == "sms"
    assert core["schema_version"] == 4

    assert j["retrieval_evidence"] == j["sources"]
    assert j["gate"] == "ok"
    assert j["generation_owner"] == "hermes"
    assert j["retrieved_chunks"]
    assert j["retrieval_context"]


def test_error_before_retrieval_has_empty_evidence(scopes_yaml):
    from rag_engine.query import answer

    r = answer("", scope="sms")
    assert r.status == "empty_question"
    assert r.retrieval_evidence == []
    assert r.gate == "empty_question"

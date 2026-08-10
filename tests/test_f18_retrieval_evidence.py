"""F-18: `sources: []` was emitted on any non-"ok" status, making "retrieval
found nothing" and "retrieval found weak matches and the model declined"
indistinguishable from the JSON contract alone. Fixed additively:
`retrieval_evidence` (always populated from what retrieval actually
returned) and `gate` (which internal branch produced a non-"ok" status) are
new fields; every existing field keeps its exact name, meaning, and
population rule. See F18_cli_hides_retrieval_evidence_20260727.md."""

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


def _patch_llm_response(text: str):
    return patch("rag_engine.query._invoke_generation", return_value=text)


def _patch_retrieval(pairs, gate=None):
    diag = {"gate": gate}
    if pairs:
        diag["best_raw_distance"] = min(float(distance) for _doc, distance in pairs)
    return patch("rag_engine.query.retrieve_with_scores_and_diagnostics", return_value=(pairs, diag))


# v3 fields only -- the exact set that existed before this repair. Used to
# prove the "ok" payload is byte-identical on every field that already
# existed, field by field, not just "the new keys are additive".
V3_FIELDS = (
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


def test_no_coverage_carries_populated_evidence(scopes_yaml):
    """The exact ambiguity F-18 was written about: a chunk was retrieved
    (weak match), the model declined via NOT_IN_CONTEXT, and the final
    status is no_coverage. `sources` still empties (existing rule,
    unchanged) but `retrieval_evidence` must carry what was actually
    found."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(path="10_Company/a.pdf", collection="sms"), 0.9)]
    with _patch_retrieval(pairs):
        with _patch_llm_response("NOT_IN_CONTEXT"):
            r = answer("q", scope="sms")

    assert r.status == "no_coverage"
    assert r.sources == []
    assert r.retrieval_evidence != []
    assert r.retrieval_evidence[0]["path"] == "10_Company/a.pdf"
    assert r.retrieval_evidence[0]["collection"] == "sms"
    assert "distance" in r.retrieval_evidence[0]
    assert r.gate == "refusal_or_weak_evidence"

    j = r.to_json()
    assert j["sources"] == []
    assert j["retrieval_evidence"] == r.retrieval_evidence
    assert j["gate"] == "refusal_or_weak_evidence"


def test_true_zero_retrieval_has_empty_evidence_and_distinct_gate(scopes_yaml):
    """The other half of the ambiguity: genuinely nothing retrieved. Same
    status (no_coverage), same empty `sources`, but `retrieval_evidence`
    stays empty too and `gate` names the different cause -- this is what
    makes the two no_coverage cases distinguishable now."""
    from rag_engine.query import answer

    with _patch_retrieval([]):
        r = answer("q", scope="vessels")

    assert r.status == "no_coverage"
    assert r.sources == []
    assert r.retrieval_evidence == []
    assert r.gate == "no_retrieval"


def test_ok_result_existing_fields_byte_identical(scopes_yaml):
    """Prove the additive claim directly: build the v3-only subset of the
    payload and compare it against a hand-built expected dict using the
    exact v3 shape -- not just "the keys are still there", but the actual
    values are unchanged by this change."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with _patch_llm_response("Follow the FO procedure before bunkering."):
            r = answer("fuel oil?", scope="sms", scope_resolution_s=0.01)

    j = r.to_json()
    v3_subset = {k: j[k] for k in V3_FIELDS}

    assert v3_subset["status"] == "ok"
    assert v3_subset["coverage"] == "full"
    assert v3_subset["answer"] == "Follow the FO procedure before bunkering."
    assert v3_subset["missing_information"] is None
    assert v3_subset["sources"] == [
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
    assert v3_subset["model"] is not None
    assert v3_subset["scope"] == "sms"

    # And the new fields are present alongside, not instead of, the old ones.
    assert set(j) - set(V3_FIELDS) == {"retrieval_evidence", "retrieval_diagnostics", "gate"}
    assert j["retrieval_evidence"] == j["sources"]
    assert j["gate"] == "ok"


def test_error_before_retrieval_has_empty_evidence(scopes_yaml):
    """empty_question never reaches retrieval at all -- retrieval_evidence
    must be empty, not None or omitted, and gate names the branch."""
    from rag_engine.query import answer

    r = answer("", scope="sms")
    assert r.status == "empty_question"
    assert r.retrieval_evidence == []
    assert r.gate == "empty_question"

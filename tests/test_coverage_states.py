"""Unit tests for retrieval-only ask path, coverage states, timings, and the
external --json contract (ORCH_104)."""

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


def _fake_doc(
    path: str = "10_Company/a.pdf",
    page: int = 1,
    collection: str = "sms",
    content: str = "Relevant procedure text about fuel oil.",
):
    doc = MagicMock()
    doc.metadata = {"source": path, "page": page, "collection": collection}
    doc.page_content = content
    return doc


def _patch_retrieval(pairs, gate=None):
    diag = {"gate": gate}
    if pairs:
        diag["best_raw_distance"] = min(float(distance) for _doc, distance in pairs)
    return patch("rag_engine.query.retrieve_with_scores_and_diagnostics", return_value=(pairs, diag))


def test_resolve_answer_model(scopes_yaml):
    from rag_engine.query import resolve_answer_model

    assert resolve_answer_model() == "gpt-5.6-luna"
    assert resolve_answer_model("custom:7b") == "custom:7b"


def test_retrieval_ok_returns_package_without_nl_answer(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with patch("rag_engine.query._invoke_generation") as llm:
            r = answer("fuel oil?", scope="sms", scope_resolution_s=0.01)
            llm.assert_not_called()

    assert r.status == "ok"
    assert r.coverage == "full"
    assert r.answer is None
    assert r.model is None
    assert r.missing_information is None
    assert len(r.sources) == 1
    assert len(r.retrieved_chunks) == 1
    assert "fuel oil" in (r.retrieval_context or "")
    j = r.to_json()
    assert j["schema_version"] == 4
    assert j["sources"]
    assert j["status"] == "ok"
    assert j["answer"] is None
    assert j["generation_owner"] == "hermes"
    assert j["retrieved_chunks"][0]["text"]
    assert j["document_names"] == ["a.pdf"]
    assert j["page_numbers"] == [2]
    assert j["timings"]["generation"] is None
    # ORCH_106: coverage=full is retrieval-package state only — not answer completeness.
    assert j["coverage"] == "full"
    assert r.answer is None  # ok + full coverage still allows null answer


def test_coverage_full_is_retrieval_package_state_not_answer_completeness(scopes_yaml):
    """coverage=full must not be interpreted as factual/answer completeness."""
    from rag_engine.query import answer

    # Non-clarifying query with admissible hits whose text does not contain
    # the numeric/procedural fact a caller might ask Hermes to extract.
    pairs = [(_fake_doc(content="General SMS housekeeping note with no pressure limit."), 0.3)]
    with _patch_retrieval(pairs):
        r = answer("fuel oil bunkering checklist?", scope="sms")

    assert r.status == "ok"
    assert r.coverage == "full"
    assert r.answer is None
    blob = " ".join(c.get("text", "") for c in r.to_json()["retrieved_chunks"]).lower()
    assert "bar" not in blob and "setpoint" not in blob
    j = r.to_json()
    # Runtime proof: full coverage + null answer is a valid ok package.
    assert j["coverage"] == "full" and j["answer"] is None and j["generation_owner"] == "hermes"


def test_prompt_builder_archived_still_documents_not_in_context(scopes_yaml):
    from rag_engine.query import _build_prompt

    prompt = _build_prompt("q?", "ctx", "sms")
    assert "JSON" not in prompt.replace("no JSON", "")
    assert "NOT_IN_CONTEXT" in prompt
    assert "plain" in prompt.lower()


def test_ask_path_never_calls_generation_even_with_strong_hits(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.30)]
    with _patch_retrieval(pairs):
        with patch("rag_engine.query._invoke_generation") as llm:
            r = answer("fuel oil bunkering checklist?", scope="sms")
            llm.assert_not_called()

    assert r.status == "ok"
    assert r.answer is None
    assert r.sources


def test_source_only_query_bypasses_llm_when_retrieval_is_strong(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(path="10_Company/manual.pdf", page=3), 0.30)]
    with _patch_retrieval(pairs):
        with patch("rag_engine.query._invoke_generation") as llm:
            r = answer("return source details only for the manual", scope="sms")

    llm.assert_not_called()
    assert r.status == "ok"
    assert r.coverage == "full"
    assert r.answer == "Relevant document found in scope sms. See listed source pages."
    assert len(r.sources) == 1
    assert r.sources[0]["path"] == "10_Company/manual.pdf"


def test_not_in_context_detection_is_first_line_only(scopes_yaml):
    from rag_engine.query import model_declared_not_in_context

    assert model_declared_not_in_context("NOT_IN_CONTEXT")
    assert model_declared_not_in_context("  NOT_IN_CONTEXT.")
    assert model_declared_not_in_context("\n\nNOT_IN_CONTEXT\nextra prose")
    assert not model_declared_not_in_context(
        "The manual says NOT_IN_CONTEXT is a token."
    )
    assert not model_declared_not_in_context("An answer.\nNOT_IN_CONTEXT")
    assert not model_declared_not_in_context("")


def test_no_coverage_empty_retrieval_skips_generation(scopes_yaml):
    from rag_engine.query import answer

    with _patch_retrieval([]):
        with patch("rag_engine.query._invoke_generation") as llm:
            r = answer("missing doc", scope="sms")
            llm.assert_not_called()
    assert r.status == "no_coverage"
    assert r.coverage == "none"


def test_invoke_generation_is_archived_and_raises(scopes_yaml):
    from rag_engine.query import _invoke_generation

    with pytest.raises(RuntimeError, match="retrieval-only"):
        _invoke_generation("gpt-5.6-luna", "prompt")


def test_retrieval_timeout_is_still_a_hard_error(scopes_yaml):
    from rag_engine.query import answer

    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        side_effect=TimeoutError("Ollama call timed out after 300.0s (RAG_OLLAMA_TIMEOUT)"),
    ):
        r = answer("q", scope="sms")

    assert r.status == "error"
    assert "timed out" in (r.error or "").lower()


def test_timing_fields_present_without_generation(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        r = answer("q", scope="sms", scope_resolution_s=0.012)

    t = r.timings
    assert set(t) == {
        "scope_resolution",
        "retrieval",
        "generation_primary",
        "generation_repair",
        "generation_fallback",
        "generation",
        "total",
    }
    assert t["scope_resolution"] == 0.012
    assert isinstance(t["retrieval"], float)
    assert t["generation_primary"] is None
    assert t["generation_repair"] is None
    assert t["generation_fallback"] is None
    assert t["generation"] is None
    assert isinstance(t["total"], float)
    assert t["retrieval"] == round(t["retrieval"], 3)
    assert t["total"] == round(t["total"], 3)
    j = r.to_json()
    assert j["timings"] == t


CONTRACT_KEYS = {
    "schema_version",
    "status",
    "query",
    "requested_scope",
    "resolved_scope",
    "coverage",
    "answer",
    "missing_information",
    "sources",
    "retrieved_chunks",
    "retrieval_context",
    "page_numbers",
    "document_names",
    "clarification_state",
    "retrieval_metadata",
    "generation_owner",
    "retrieval_evidence",
    "retrieval_diagnostics",
    "gate",
    "timings",
    "model",
    "scope",
}

TIMING_KEYS = {
    "scope_resolution",
    "retrieval",
    "generation_primary",
    "generation_repair",
    "generation_fallback",
    "generation",
    "total",
}


def test_json_contract_ok_payload(scopes_yaml):
    from rag_engine.query import SCHEMA_VERSION, answer

    pairs = [(_fake_doc(), 0.30)]
    with _patch_retrieval(pairs):
        j = answer("fuel oil?", scope="sms").to_json()

    assert set(j) == CONTRACT_KEYS
    assert j["schema_version"] == SCHEMA_VERSION == 4
    assert set(j["timings"]) == TIMING_KEYS
    assert isinstance(j["sources"], list)
    src = j["sources"][0]
    assert set(src) == {
        "path",
        "page",
        "page_index",
        "collection",
        "distance",
        "authority_rank",
        "machine_transcribed",
    }
    assert j["gate"] == "ok"
    assert j["retrieval_evidence"] == j["sources"]
    assert j["answer"] is None
    assert j["generation_owner"] == "hermes"
    assert j["retrieval_diagnostics"]["final_retained_count"] == len(j["sources"])


def test_json_contract_no_coverage_payload(scopes_yaml):
    from rag_engine.query import answer

    with _patch_retrieval([]):
        j = answer("q", scope="sms").to_json()

    assert set(j) == CONTRACT_KEYS | {"hint"}
    assert j["schema_version"] == 4
    assert j["status"] == "no_coverage"
    assert j["sources"] == []
    assert j["retrieved_chunks"] == []
    assert j["answer"] is None
    assert j["retrieval_evidence"] == []
    assert j["gate"] == "no_retrieval"


def test_answer_reports_no_retrieval_gate_with_zero_counts(scopes_yaml):
    from rag_engine.query import answer

    with _patch_retrieval([], gate="no_retrieval"):
        with patch("rag_engine.query._invoke_generation") as llm:
            r = answer("missing doc", scope="sms")
            llm.assert_not_called()

    assert r.status == "no_coverage"
    assert r.gate == "no_retrieval"
    assert r.retrieval_evidence == []
    assert r.to_json()["retrieval_diagnostics"]["raw_count"] == 0


def test_answer_reports_final_confidence_failed_with_retrieval_counts(scopes_yaml):
    from rag_engine.query import answer

    pairs = [
        (
            _fake_doc(
                path="00_Career/03_Engine_Knowledge/Training/guide.pdf",
                page=1,
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
        with patch("rag_engine.query._invoke_generation") as llm:
            r = answer("q", scope="sms")
            llm.assert_not_called()

    assert r.status == "no_coverage"
    assert r.gate == "final_confidence_failed"
    assert r.retrieval_evidence[0]["path"] == "00_Career/03_Engine_Knowledge/Training/guide.pdf"
    assert r.to_json()["retrieval_diagnostics"]["post_dedupe_count"] == 1


def test_json_contract_error_payload(scopes_yaml):
    from rag_engine.query import answer

    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        side_effect=RuntimeError("boom"),
    ):
        j = answer("q", scope="sms").to_json()

    assert set(j) == CONTRACT_KEYS | {"error"}
    assert j["status"] == "error"
    assert j["error"]
    assert j["gate"] == "retrieval_error"


def test_exit_codes_unchanged():
    from rag_engine.query import EXIT_ERROR, EXIT_NO_COVERAGE, EXIT_OK

    assert EXIT_OK == 0
    assert EXIT_ERROR == 1
    assert EXIT_NO_COVERAGE == 2


def test_openai_timeout_default_and_override(monkeypatch):
    from rag_engine.openai_generation import openai_timeout_s

    monkeypatch.delenv("RAG_OPENAI_TIMEOUT", raising=False)
    assert openai_timeout_s() == 60.0

    monkeypatch.setenv("RAG_OPENAI_TIMEOUT", "3.5")
    assert openai_timeout_s() == 3.5

"""Unit tests for plain-text generation, coverage states, timings, and the
external --json contract."""

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
            "llm_model_default": "qwen2.5:3b",
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


def _patch_llm_response(text: str):
    llm = MagicMock()
    llm.invoke.return_value = text
    return patch("rag_engine.query._get_llm", return_value=llm)


def _patch_retrieval(pairs, gate=None):
    diag = {"gate": gate}
    if pairs:
        diag["best_raw_distance"] = min(float(distance) for _doc, distance in pairs)
    return patch("rag_engine.query.retrieve_with_scores_and_diagnostics", return_value=(pairs, diag))


def test_resolve_answer_model(scopes_yaml):
    from rag_engine.query import resolve_answer_model

    assert resolve_answer_model() == "qwen2.5:3b"
    assert resolve_answer_model(use_fallback=True) == "qwen3.5:9b"
    assert resolve_answer_model("custom:7b", use_fallback=True) == "custom:7b"


def test_plain_text_answer_is_ok(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with _patch_llm_response("Follow the FO procedure before bunkering."):
            r = answer("fuel oil?", scope="sms", scope_resolution_s=0.01)

    assert r.status == "ok"
    assert r.coverage == "full"
    assert r.answer == "Follow the FO procedure before bunkering."
    assert r.missing_information is None
    assert len(r.sources) == 1
    j = r.to_json()
    assert j["schema_version"] == 3
    assert j["sources"]
    assert j["status"] == "ok"


def test_prompt_requests_plain_text_not_json(scopes_yaml):
    from rag_engine.query import _build_prompt

    prompt = _build_prompt("q?", "ctx", "sms")
    assert "JSON" not in prompt.replace("no JSON", "")
    assert "NOT_IN_CONTEXT" in prompt
    assert "plain" in prompt.lower()


def test_not_in_context_sentinel_is_no_coverage(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.9)]
    with _patch_retrieval(pairs):
        with _patch_llm_response("NOT_IN_CONTEXT"):
            r = answer("unrelated?", scope="sms")

    assert r.status == "no_coverage"
    assert r.coverage == "none"
    assert r.answer is None
    assert r.to_json()["sources"] == []
    assert r.hint


def test_source_only_query_bypasses_llm_when_retrieval_is_strong(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(path="10_Company/manual.pdf", page=3), 0.4)]
    with _patch_retrieval(pairs):
        with patch("rag_engine.query._get_llm") as llm:
            r = answer("return source details only for the manual", scope="sms")

    llm.assert_not_called()
    assert r.status == "ok"
    assert r.coverage == "full"
    assert r.answer == "Relevant document found in scope sms. See listed source pages."
    assert len(r.sources) == 1
    assert r.sources[0]["path"] == "10_Company/manual.pdf"


def test_not_in_context_with_single_source_consensus_preserves_sources(scopes_yaml):
    from rag_engine.query import answer

    pairs = [
        (_fake_doc(path="10_Company/manual.pdf", page=1), 0.4),
        (_fake_doc(path="10_Company/manual.pdf", page=2), 0.5),
        (_fake_doc(path="10_Company/manual.pdf", page=3), 0.6),
    ]
    with _patch_retrieval(pairs):
        with _patch_llm_response("NOT_IN_CONTEXT"):
            r = answer("What is the procedure detail?", scope="sms")

    assert r.status == "ok"
    assert r.coverage == "full"
    assert (
        r.answer
        == "Relevant source found; answer generation could not extract the requested detail. See the listed sources."
    )
    assert len(r.sources) == 3


def test_not_in_context_with_conflicting_sources_stays_no_coverage(scopes_yaml):
    from rag_engine.query import answer

    pairs = [
        (_fake_doc(path="10_Company/manual_a.pdf", page=1), 0.4),
        (_fake_doc(path="10_Company/manual_b.pdf", page=2), 0.5),
        (_fake_doc(path="10_Company/manual_a.pdf", page=3), 0.6),
    ]
    with _patch_retrieval(pairs):
        with _patch_llm_response("NOT_IN_CONTEXT"):
            r = answer("What is the procedure detail?", scope="sms")

    assert r.status == "no_coverage"
    assert r.coverage == "none"
    assert r.to_json()["sources"] == []


def test_not_in_context_detection_is_first_line_only(scopes_yaml):
    from rag_engine.query import model_declared_not_in_context

    assert model_declared_not_in_context("NOT_IN_CONTEXT")
    assert model_declared_not_in_context("  NOT_IN_CONTEXT.")
    assert model_declared_not_in_context("\n\nNOT_IN_CONTEXT\nextra prose")
    # The sentinel buried in prose must NOT trigger no_coverage
    assert not model_declared_not_in_context(
        "The manual says NOT_IN_CONTEXT is a token."
    )
    assert not model_declared_not_in_context("An answer.\nNOT_IN_CONTEXT")
    assert not model_declared_not_in_context("")


def test_no_coverage_empty_retrieval_skips_llm(scopes_yaml):
    from rag_engine.query import answer

    with _patch_retrieval([]):
        with patch("rag_engine.query._get_llm") as llm:
            r = answer("missing doc", scope="sms")
            llm.assert_not_called()
    assert r.status == "no_coverage"
    assert r.coverage == "none"


def test_empty_model_response_is_error_not_partial(scopes_yaml):
    """No salvage path: a broken generation is an honest error and never a
    partial_coverage downgrade."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with _patch_llm_response(""):
            r = answer("q", scope="sms")

    assert r.status == "error"
    assert r.error == "empty model response"
    assert r.answer is None


def test_single_generation_call_no_repair_loop(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    llm = MagicMock()
    llm.invoke.return_value = ""  # unusable output
    with _patch_retrieval(pairs):
        with patch("rag_engine.query._get_llm", return_value=llm):
            r = answer("q", scope="sms")

    assert llm.invoke.call_count == 1
    assert r.status == "error"


def test_heavy_fallback_model_never_touched_by_default(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    fallback_llm = MagicMock()
    fast_llm = MagicMock()
    fast_llm.invoke.return_value = "An answer."

    def _get_llm(model, num_ctx, num_predict):
        return fallback_llm if model == "qwen3.5:9b" else fast_llm

    with _patch_retrieval(pairs):
        with patch("rag_engine.query._get_llm", side_effect=_get_llm) as get_llm:
            r = answer("q", scope="sms")  # use_fallback defaults False

    models_requested = [c.args[0] for c in get_llm.call_args_list]
    assert "qwen3.5:9b" not in models_requested
    assert fallback_llm.invoke.call_count == 0
    assert r.status == "ok"
    assert r.model == "qwen2.5:3b"


def test_explicit_heavy_fallback_routes_primary(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    heavy_llm = MagicMock()
    heavy_llm.invoke.return_value = "Answer from the heavy model."
    fast_llm = MagicMock()
    fast_llm.invoke.return_value = "Answer from the fast model."

    def _get_llm(model, num_ctx, num_predict):
        return heavy_llm if model == "qwen3.5:9b" else fast_llm

    with _patch_retrieval(pairs):
        with patch("rag_engine.query._get_llm", side_effect=_get_llm):
            r = answer("q", scope="sms", use_fallback=True)

    assert r.status == "ok"
    assert r.model == "qwen3.5:9b"
    assert heavy_llm.invoke.call_count == 1
    assert fast_llm.invoke.call_count == 0
    assert r.timings["generation_repair"] is None
    assert r.timings["generation_fallback"] is None


def test_generation_timeout_is_error(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with patch("rag_engine.query._get_llm", return_value=MagicMock()):
            with patch(
                "rag_engine.query._run_with_timeout",
                side_effect=TimeoutError(
                    "Ollama call timed out after 8.0s (RAG_OLLAMA_GEN_TIMEOUT)"
                ),
            ):
                r = answer("q", scope="sms", scope_resolution_s=0.001)

    assert r.status == "error"
    assert "timed out" in (r.error or "").lower()
    assert r.timings["retrieval"] is not None
    assert r.timings["generation"] is not None
    assert r.timings["total"] is not None
    assert r.timings["scope_resolution"] == 0.001


def test_retrieval_timeout_is_still_a_hard_error(scopes_yaml):
    from rag_engine.query import answer

    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        side_effect=TimeoutError("Ollama call timed out after 300.0s (RAG_OLLAMA_TIMEOUT)"),
    ):
        r = answer("q", scope="sms")

    assert r.status == "error"
    assert "timed out" in (r.error or "").lower()


def test_fenced_answer_is_unwrapped(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with _patch_llm_response("```\nThe procedure requires two checks.\n```"):
            r = answer("q", scope="sms")

    assert r.status == "ok"
    assert r.answer == "The procedure requires two checks."


def test_timing_fields_present(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with _patch_llm_response("yes"):
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
    assert isinstance(t["generation_primary"], float)
    assert t["generation_repair"] is None  # no repair pass exists any more
    assert t["generation_fallback"] is None  # no chained fallback exists any more
    assert isinstance(t["generation"], float)
    assert isinstance(t["total"], float)
    # rounded to 3 decimals
    assert t["retrieval"] == round(t["retrieval"], 3)
    assert t["total"] == round(t["total"], 3)
    j = r.to_json()
    assert j["timings"] == t


# ---------------------------------------------------------------------------
# External --json contract: same fields, same schema_version, same exit codes.
# ---------------------------------------------------------------------------

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
    "retrieval_evidence",  # F-18: additive, always populated
    "gate",  # F-18: additive, non-None only on a non-"ok" status
    "timings",
    "model",
    "scope",  # legacy convenience field
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

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with _patch_llm_response("An answer."):
            j = answer("q", scope="sms").to_json()

    assert set(j) == CONTRACT_KEYS
    assert j["schema_version"] == SCHEMA_VERSION == 3
    assert set(j["timings"]) == TIMING_KEYS
    assert isinstance(j["sources"], list)
    src = j["sources"][0]
    assert set(src) == {
        "path",
        "page",
        "collection",
        "distance",
        "authority_rank",
        "machine_transcribed",
    }
    # F-18: "ok" is not a gated status — nothing to explain.
    assert j["gate"] is None
    assert j["retrieval_evidence"] == j["sources"]


def test_json_contract_no_coverage_payload(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.9)]
    with _patch_retrieval(pairs):
        with _patch_llm_response("NOT_IN_CONTEXT"):
            j = answer("q", scope="sms").to_json()

    # hint is additive on no_coverage — everything else identical
    assert set(j) == CONTRACT_KEYS | {"hint"}
    assert j["schema_version"] == 3
    assert j["status"] == "no_coverage"
    assert j["sources"] == []
    assert j["answer"] is None
    # F-18: this is exactly the case the finding was about — a chunk was
    # retrieved (weak match, model declined) and `sources` still empties
    # per the existing status rule, but `retrieval_evidence` now carries
    # what was actually found, and `gate` names why.
    assert j["retrieval_evidence"] != []
    assert j["retrieval_evidence"][0]["path"] == "10_Company/a.pdf"
    assert j["gate"] == "not_in_context_weak_evidence"


def test_json_contract_error_payload(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with _patch_retrieval(pairs):
        with _patch_llm_response(""):
            j = answer("q", scope="sms").to_json()

    assert set(j) == CONTRACT_KEYS | {"error"}
    assert j["status"] == "error"
    assert j["error"]
    assert j["gate"] == "empty_model_response"


def test_exit_codes_unchanged():
    from rag_engine.query import EXIT_ERROR, EXIT_NO_COVERAGE, EXIT_OK

    assert EXIT_OK == 0
    assert EXIT_ERROR == 1
    assert EXIT_NO_COVERAGE == 2


def test_ollama_gen_timeout_default_and_override(monkeypatch):
    from rag_engine.query import ollama_gen_timeout_s

    monkeypatch.delenv("RAG_OLLAMA_GEN_TIMEOUT", raising=False)
    assert ollama_gen_timeout_s() == 8.0

    monkeypatch.setenv("RAG_OLLAMA_GEN_TIMEOUT", "3.5")
    assert ollama_gen_timeout_s() == 3.5


def test_generation_call_uses_gen_timeout_not_retrieval_timeout(scopes_yaml, monkeypatch):
    """Every generation call must use the tight generation timeout, not the
    300s default reserved for retrieval."""
    from rag_engine.query import _invoke_llm

    monkeypatch.setenv("RAG_OLLAMA_GEN_TIMEOUT", "8")
    llm = MagicMock()
    llm.invoke.return_value = "ok"
    seen_timeout = {}

    def _fake_run_with_timeout(fn, timeout=None):
        seen_timeout["value"] = timeout
        return fn()

    with patch("rag_engine.query._get_llm", return_value=llm):
        with patch("rag_engine.query._run_with_timeout", side_effect=_fake_run_with_timeout):
            _invoke_llm("qwen2.5:3b", "prompt", None, None)

    assert seen_timeout["value"] == 8.0

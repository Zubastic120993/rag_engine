"""Unit tests for coverage states, timings, and generation controls."""

from __future__ import annotations

import json
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


def test_resolve_answer_model(scopes_yaml):
    from rag_engine.query import resolve_answer_model

    assert resolve_answer_model() == "qwen2.5:3b"
    assert resolve_answer_model(use_fallback=True) == "qwen3.5:9b"
    assert resolve_answer_model("custom:7b", use_fallback=True) == "custom:7b"


def test_full_coverage(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    payload = {
        "coverage": "full",
        "answer": "Follow the FO procedure.",
        "missing_information": None,
    }
    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with _patch_llm_response(json.dumps(payload)):
            r = answer("fuel oil?", scope="sms", scope_resolution_s=0.01)

    assert r.status == "ok"
    assert r.coverage == "full"
    assert r.answer == "Follow the FO procedure."
    assert r.missing_information is None
    assert len(r.sources) == 1
    j = r.to_json()
    assert j["schema_version"] == 2
    assert j["sources"]
    assert j["status"] == "ok"


def test_partial_coverage_keeps_sources(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.38)]
    payload = {
        "coverage": "partial",
        "answer": "Service Experience notes a cylinder issue.",
        "missing_information": "Exact torque value not in retrieved pages.",
    }
    # Incomplete answers that mention uncertainty must NOT become no_coverage
    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with _patch_llm_response(json.dumps(payload)):
            r = answer("cylinder wear?", scope="sms", scope_resolution_s=0.005)

    assert r.status == "partial_coverage"
    assert r.coverage == "partial"
    assert "cylinder" in (r.answer or "").lower()
    assert r.missing_information
    assert r.sources and r.sources[0]["path"].endswith("a.pdf")
    assert r.to_json()["sources"]


def test_partial_does_not_flip_on_i_do_not_know_phrase(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.5)]
    payload = {
        "coverage": "partial",
        "answer": "I do not know the exact serial, but the manual lists type ABC.",
        "missing_information": "serial number",
    }
    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with _patch_llm_response(json.dumps(payload)):
            r = answer("serial?", scope="sms")

    assert r.status == "partial_coverage"
    assert r.sources


def test_partial_coverage_cannot_have_null_answer():
    from rag_engine.query import normalize_coverage_payload

    with pytest.raises(ValueError, match="partial coverage requires non-empty answer"):
        normalize_coverage_payload(
            {
                "coverage": "partial",
                "answer": None,
                "missing_information": "wear limit",
            }
        )


def test_liner_wear_partial_keeps_supported_answer(scopes_yaml):
    from rag_engine.query import answer

    content = (
        "Service Experience: Liner and cylinder wear should be monitored during "
        "overhaul. Replace the liner when scuffing or excessive ovality is observed; "
        "inspect piston rings and groove clearance together with the cylinder liner."
    )
    pairs = [(_fake_doc(content=content), 0.35)]
    bad = {
        "coverage": "partial",
        "answer": None,
        "missing_information": "wear limit",
    }
    good = {
        "coverage": "partial",
        "answer": (
            "Service Experience advises monitoring liner and cylinder wear at "
            "overhaul and replacing the liner when scuffing or excessive ovality "
            "is observed."
        ),
        "missing_information": "Numeric wear limit not in retrieved pages.",
    }
    llm = MagicMock()
    llm.invoke.side_effect = [json.dumps(bad), json.dumps(good)]
    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", return_value=llm) as get_llm:
            r = answer("liner wear limit?", scope="sms")

    assert llm.invoke.call_count == 2
    assert get_llm.call_count >= 1
    assert r.status == "partial_coverage"
    assert r.answer
    assert "liner" in r.answer.lower() or "cylinder" in r.answer.lower()
    assert r.missing_information
    assert r.sources


def test_omd24_15ppm_not_unspecified(scopes_yaml):
    from rag_engine.query import answer, normalize_coverage_payload

    contradictory = {
        "coverage": "partial",
        "answer": (
            "The OMD-24 alarm activates when oil content in the water is above 15 ppm."
        ),
        "missing_information": "Exact alarm threshold not specified in the document.",
    }
    with pytest.raises(ValueError, match="internal contradiction"):
        normalize_coverage_payload(contradictory)

    content = (
        "OMD-24 oil content monitor: the alarm activates / indicator changes when "
        "oil content in the discharge water is above 15 ppm."
    )
    pairs = [(_fake_doc(content=content), 0.3)]
    corrected = {
        "coverage": "full",
        "answer": (
            "The OMD-24 alarm activates when oil content in the water is above 15 ppm."
        ),
        "missing_information": None,
    }
    llm = MagicMock()
    llm.invoke.side_effect = [json.dumps(contradictory), json.dumps(corrected)]
    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", return_value=llm):
            r = answer("OMD-24 alarm threshold?", scope="sms")

    assert r.status == "ok"
    assert r.answer
    assert "15" in r.answer and "ppm" in r.answer.lower()
    assert "unspecified" not in r.answer.lower()
    assert "not specified" not in r.answer.lower()
    assert llm.invoke.call_count == 2


def test_automatic_heavy_fallback_disabled_by_default(scopes_yaml):
    """Primary + one fast-model repair fail; qwen3.5:9b must NOT be touched
    unless --fallback (use_fallback=True) was explicitly passed."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    primary_llm = MagicMock()
    primary_llm.invoke.return_value = "not json at all"
    fallback_llm = MagicMock()
    fallback_llm.invoke.return_value = json.dumps(
        {"coverage": "full", "answer": "should never be reached", "missing_information": None}
    )

    def _get_llm(model, num_ctx, num_predict):
        if model == "qwen3.5:9b":
            return fallback_llm
        return primary_llm

    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", side_effect=_get_llm) as get_llm:
            r = answer("q", scope="sms")  # use_fallback defaults False

    models_requested = [c.args[0] for c in get_llm.call_args_list]
    assert "qwen3.5:9b" not in models_requested
    assert fallback_llm.invoke.call_count == 0
    # primary attempt + exactly one fast-model repair attempt, no loop
    assert primary_llm.invoke.call_count == 2
    # generation degrades gracefully rather than losing the retrieved evidence
    assert r.status == "partial_coverage"
    assert r.sources
    assert r.model != "qwen3.5:9b"


def test_no_recursive_retry_loop(scopes_yaml):
    """Exactly two generation calls total when everything fails — never more."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    llm = MagicMock()
    llm.invoke.return_value = ""  # empty response, every call

    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", return_value=llm):
            r = answer("q", scope="sms")

    assert llm.invoke.call_count == 2
    assert r.status in ("partial_coverage", "error")


def test_explicit_heavy_fallback_enabled(scopes_yaml):
    """use_fallback=True routes the primary attempt itself to the heavy model."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    valid = {
        "coverage": "full",
        "answer": "Answer from the explicitly requested heavy model.",
        "missing_information": None,
    }
    heavy_llm = MagicMock()
    heavy_llm.invoke.return_value = json.dumps(valid)
    fast_llm = MagicMock()
    fast_llm.invoke.return_value = json.dumps(valid)

    def _get_llm(model, num_ctx, num_predict):
        return heavy_llm if model == "qwen3.5:9b" else fast_llm

    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", side_effect=_get_llm):
            r = answer("q", scope="sms", use_fallback=True)

    assert r.status == "ok"
    assert r.model == "qwen3.5:9b"
    assert heavy_llm.invoke.call_count == 1
    assert fast_llm.invoke.call_count == 0  # primary succeeded, no repair needed
    assert r.timings["generation_repair"] is None
    assert r.timings["generation_fallback"] is None


def test_empty_primary_response_repaired_by_fast_model(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    good = {
        "coverage": "full",
        "answer": "Recovered by the repair attempt.",
        "missing_information": None,
    }
    llm = MagicMock()
    llm.invoke.side_effect = ["", json.dumps(good)]  # empty primary, valid repair

    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", return_value=llm):
            r = answer("q", scope="sms")

    assert llm.invoke.call_count == 2
    assert r.status == "ok"
    assert r.answer == "Recovered by the repair attempt."
    assert r.timings["generation_primary"] is not None
    assert r.timings["generation_repair"] is not None


def test_empty_repair_response_salvaged(scopes_yaml):
    """Primary empty, repair also empty -> deterministic salvage, not error."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    llm = MagicMock()
    llm.invoke.side_effect = ["", ""]

    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", return_value=llm):
            r = answer("q", scope="sms")

    assert llm.invoke.call_count == 2
    assert r.status == "partial_coverage"
    assert r.answer
    assert "fuel oil" in r.answer.lower()
    assert r.missing_information
    assert r.sources


def test_repair_timeout_still_salvages(scopes_yaml):
    """A repair attempt that times out is treated like any other failed
    attempt: no crash, no hang, graceful degradation to partial_coverage."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    llm = MagicMock()
    call_count = {"n": 0}

    def _invoke(prompt):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "not json"  # primary: malformed
        raise TimeoutError("Ollama call timed out after 8.0s (RAG_OLLAMA_GEN_TIMEOUT)")

    llm.invoke.side_effect = _invoke

    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", return_value=llm):
            r = answer("q", scope="sms")

    assert call_count["n"] == 2
    assert r.status == "partial_coverage"
    assert r.answer
    assert r.sources
    assert r.timings["generation_repair"] is not None


def test_no_coverage_from_model(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.9)]
    payload = {
        "coverage": "none",
        "answer": None,
        "missing_information": "Not in these chunks.",
    }
    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with _patch_llm_response(json.dumps(payload)):
            r = answer("unrelated?", scope="sms")

    assert r.status == "no_coverage"
    assert r.coverage == "none"
    assert r.answer is None
    assert r.to_json()["sources"] == []


def test_no_coverage_empty_retrieval(scopes_yaml):
    from rag_engine.query import answer

    with patch("rag_engine.query.retrieve_with_scores", return_value=[]):
        with patch("rag_engine.query._get_llm") as llm:
            r = answer("missing doc", scope="sms")
            llm.assert_not_called()
    assert r.status == "no_coverage"
    assert r.coverage == "none"


def test_malformed_model_json_salvaged_as_partial_coverage(scopes_yaml):
    """Primary and the one repair attempt both return non-JSON. Retrieval
    already succeeded, so this must degrade to a sourced partial_coverage
    answer rather than discarding the evidence behind status=error."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with _patch_llm_response("not json at all"):
            r = answer("q", scope="sms")

    assert r.status == "partial_coverage"
    assert r.coverage == "partial"
    assert r.answer
    assert r.sources
    assert r.missing_information
    assert "malformed" in r.missing_information.lower() or "unavailable" in r.missing_information.lower()
    assert r.timings["retrieval"] is not None
    assert r.timings["generation"] is not None
    assert r.timings["generation_primary"] is not None
    assert r.timings["generation_repair"] is not None
    assert r.timings["total"] is not None


def test_generation_completely_unsalvageable_returns_error(scopes_yaml):
    """If salvage genuinely produces nothing (context has no usable
    sentences), status=error is still the honest answer — no fake content."""
    from rag_engine.query import answer

    tiny = _fake_doc(content="x")  # too short for any sentence to qualify
    pairs = [(tiny, 0.4)]
    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with _patch_llm_response("not json at all"):
            r = answer("q", scope="sms")

    assert r.status == "error"
    assert r.error
    assert r.timings["generation"] is not None


def test_generation_timeout(scopes_yaml):
    """Every generation attempt times out (e.g. a hung Ollama call). With
    evidence retrieved, this degrades to partial_coverage — it must not hang
    and must not silently throw away the retrieved sources."""
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]

    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", return_value=MagicMock()):
            with patch(
                "rag_engine.query._run_with_timeout",
                side_effect=TimeoutError(
                    "Ollama call timed out after 8.0s (RAG_OLLAMA_GEN_TIMEOUT)"
                ),
            ):
                r = answer("q", scope="sms", scope_resolution_s=0.001)

    assert r.status == "partial_coverage"
    assert r.answer
    assert r.sources
    assert r.timings["retrieval"] is not None
    assert r.timings["generation"] is not None
    assert r.timings["total"] is not None
    assert r.timings["scope_resolution"] == 0.001


def test_retrieval_timeout_is_still_a_hard_error(scopes_yaml):
    """Distinct from generation timeout: if retrieval itself fails, there is
    no evidence to salvage from, so status=error is correct."""
    from rag_engine.query import answer

    with patch(
        "rag_engine.query.retrieve_with_scores",
        side_effect=TimeoutError("Ollama call timed out after 300.0s (RAG_OLLAMA_TIMEOUT)"),
    ):
        r = answer("q", scope="sms")

    assert r.status == "error"
    assert "timed out" in (r.error or "").lower()


def test_timing_fields_present(scopes_yaml):
    from rag_engine.query import answer

    pairs = [(_fake_doc(), 0.4)]
    payload = {"coverage": "full", "answer": "yes", "missing_information": None}
    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with _patch_llm_response(json.dumps(payload)):
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
    assert t["generation_repair"] is None  # primary succeeded, no repair call
    assert t["generation_fallback"] is None  # fallback never invoked
    assert isinstance(t["generation"], float)
    assert isinstance(t["total"], float)
    # rounded to 3 decimals
    assert t["retrieval"] == round(t["retrieval"], 3)
    assert t["total"] == round(t["total"], 3)
    j = r.to_json()
    assert j["timings"] == t


def test_parse_and_normalize_helpers():
    from rag_engine.query import normalize_coverage_payload, parse_model_json

    raw = '```json\n{"coverage": "PARTIAL", "answer": "a", "missing_information": "b"}\n```'
    parsed = parse_model_json(raw)
    norm = normalize_coverage_payload(parsed)
    assert norm["coverage"] == "partial"
    assert norm["status"] == "partial_coverage"
    assert norm["answer"] == "a"
    assert norm["missing_information"] == "b"


def test_liner_wear_query_returns_nonempty_supported_partial_answer(scopes_yaml):
    """The exact reported failing query. Generation is fully broken (every
    attempt malformed), but retrieval succeeded — must return a non-empty,
    sourced partial_coverage answer, never status=error, and never hang."""
    from rag_engine.query import answer

    content = (
        "Service Experience 2014: cylinder liner wear should be trended at "
        "every overhaul. Where measured wear exceeds 0.1 mm per 1000 running "
        "hours, inspect for scuffing, cold corrosion and abnormal ovality, "
        "and consider revised cylinder lubrication feed rate before the next "
        "scheduled overhaul."
    )
    pairs = [(_fake_doc(content=content), 0.35)]
    llm = MagicMock()
    llm.invoke.return_value = "not json at all"

    with patch("rag_engine.query.retrieve_with_scores", return_value=pairs):
        with patch("rag_engine.query._get_llm", return_value=llm):
            r = answer(
                "Service Experience 2014 cylinder liner wear exceeds 0.1 "
                "mm/1000 running hours recommended actions",
                scope="sms",
            )

    assert llm.invoke.call_count == 2  # primary + one repair, no loop
    assert r.status == "partial_coverage"
    assert r.answer
    assert "liner" in r.answer.lower() or "cylinder" in r.answer.lower()
    assert r.missing_information
    assert r.sources
    assert r.timings["total"] is not None


def test_deterministic_salvage_answer_pulls_sentences_from_context():
    from rag_engine.query import deterministic_salvage_answer

    parts = [
        "[source=a.pdf page=1 collection=sms]\n"
        "First relevant sentence about the procedure. Second sentence here.",
        "[source=b.pdf page=2 collection=sms]\nA third sentence from another chunk.",
    ]
    out = deterministic_salvage_answer(parts, max_sentences=2)
    assert out
    assert "First relevant sentence" in out
    assert out.count(".") <= 3  # roughly two sentences, not the whole context


def test_deterministic_salvage_answer_empty_on_no_usable_text():
    from rag_engine.query import deterministic_salvage_answer

    assert deterministic_salvage_answer([]) == ""
    assert deterministic_salvage_answer(["[source=a page=1 collection=x]\nx"]) == ""


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

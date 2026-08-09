from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES_PATH = Path(
    "/Users/vladymyrzub/Projects/ai-engineering-orchestrator/eval/clarification_first_fixtures_v1.json"
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
            "me-c": {
                "description": "Main engine",
                "hermes_aliases": [],
                "path_prefixes": ["20_Vessels/Gaschem_Europe/01_Manuals/01_Main_Engine/"],
            },
            "maker-manuals": {
                "description": "Maker manuals",
                "hermes_aliases": ["manual_library"],
                "path_prefixes": ["20_Vessels/Gaschem_Europe/01_Manuals/"],
            },
            "sms": {
                "description": "SMS",
                "hermes_aliases": [],
                "path_prefixes": ["10_Company/"],
            },
        },
        "prefix_order": ["me-c", "maker-manuals", "sms"],
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


def _fixtures() -> list[dict]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def _fixture(fid: str) -> dict:
    return next(item for item in _fixtures() if item["fixture_id"] == fid)


def _fake_doc(
    *,
    path: str,
    page: int,
    collection: str,
    content: str = "Relevant technical detail from the manual.",
):
    doc = MagicMock()
    doc.metadata = {"source": path, "page": page, "collection": collection}
    doc.page_content = content
    return doc


def _patch_llm_response(text: str):
    llm = MagicMock()
    llm.invoke.return_value = text
    return patch("rag_engine.query._get_llm", return_value=llm)


def _retrieval_payload_for_scope(scope: str):
    if scope == "me-c":
        return [
            (
                _fake_doc(
                    path="20_Vessels/Gaschem_Europe/01_Manuals/01_Main_Engine/VOLUME I.pdf",
                    page=42,
                    collection="me-c",
                    content="M42 tightening torque is 900 Nm.",
                ),
                0.31,
            )
        ]
    return [
        (
            _fake_doc(
                path="20_Vessels/Gaschem_Europe/01_Manuals/02_Auxiliary_Engines/OPERATION MANUAL_6EY22(A)LWS.pdf",
                page=73,
                collection="maker-manuals",
                content="Alarm setpoint is 7.5 bar.",
            ),
            0.28,
        )
    ]


def _run_answer(
    fixture: dict,
    *,
    llm_text: str = "Grounded technical answer.",
    verified_context: dict | None = None,
):
    from rag_engine.query import answer

    scope_calls: list[str | None] = []

    def fake_retrieve(question, scope=None, k=None):
        scope_calls.append(scope)
        if fixture["expected_final_status"] == "no_coverage":
            return [], {"gate": "no_retrieval"}
        pairs = _retrieval_payload_for_scope(fixture["expected_retrieval_target"])
        return pairs, {"gate": "ok", "best_raw_distance": min(score for _, score in pairs)}

    with patch("rag_engine.query.retrieve_with_scores_and_diagnostics", side_effect=fake_retrieve):
        with _patch_llm_response(llm_text):
            result = answer(
                fixture["question"],
                confirmation_text=fixture.get("user_confirmation"),
                verified_context=verified_context,
            )
    return result, scope_calls


@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda item: item["fixture_id"])
def test_clarification_first_fixture_pack_matches_expected_behavior(scopes_yaml, fixture):
    verified_context = None
    if fixture["fixture_id"] == "CF-014":
        verified_context = {"equipment": "main engine turbocharger", "scope": "me-c"}

    result, scope_calls = _run_answer(fixture, verified_context=verified_context)

    if fixture["expected_final_status"] == "clarification_required":
        assert result.status == "clarification_required"
        assert result.answer == fixture["expected_prompt"]
        assert scope_calls == []
    elif fixture["expected_final_status"] == "clarification_required_then_no_coverage_if_evidence_fails":
        assert result.status == "clarification_required"
        assert result.answer == fixture["expected_prompt"]
        assert scope_calls == []
    elif fixture["expected_final_status"] == "no_coverage":
        assert result.status == "no_coverage"
        assert result.resolved_scope == fixture["expected_retrieval_target"]
        assert scope_calls == [fixture["expected_retrieval_target"]]
    else:
        assert result.status == "ok"
        assert result.resolved_scope == fixture["expected_retrieval_target"]
        assert scope_calls == [fixture["expected_retrieval_target"]]


def test_incomplete_confirmation_requires_second_clarification(scopes_yaml):
    fixture = _fixture("CF-009")
    result, scope_calls = _run_answer(fixture)

    assert result.status == "clarification_required"
    assert result.answer == "Which main engine component do you mean?"
    assert scope_calls == []


def test_vague_query_then_confirmation_runs_fresh_retrieval_without_preconfirmation_reuse(scopes_yaml):
    from rag_engine.query import answer

    first_calls: list[str | None] = []
    second_calls: list[str | None] = []

    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        side_effect=lambda question, scope=None, k=None: first_calls.append(scope) or ([], {}),
    ):
        first = answer("What is the torque?")

    def fake_retrieve(question, scope=None, k=None):
        second_calls.append(scope)
        pairs = _retrieval_payload_for_scope("me-c")
        return pairs, {"gate": "ok", "best_raw_distance": min(score for _, score in pairs)}

    with patch("rag_engine.query.retrieve_with_scores_and_diagnostics", side_effect=fake_retrieve):
        with _patch_llm_response("M42 tightening torque is 900 Nm."):
            second = answer("What is the torque?", confirmation_text="MAN G50ME-C")

    assert first.status == "clarification_required"
    assert first_calls == []
    assert second.status == "ok"
    assert second.resolved_scope == "me-c"
    assert second_calls == ["me-c"]


def test_clarification_status_uses_no_coverage_exit_code_in_cli(scopes_yaml, capsys):
    import rag_engine.cli as cli
    from rag_engine.query import AskResult, EXIT_NO_COVERAGE

    clarification = AskResult(
        status="clarification_required",
        query="What is the torque?",
        requested_scope=None,
        resolved_scope=None,
        answer="Which equipment/component do you mean?",
        coverage="none",
        gate="clarification_required",
    )

    with patch.object(cli, "resolve_scope", return_value=None), patch.object(
        cli, "answer", return_value=clarification
    ), patch.object(cli, "log_ask_event"):
        code = cli.cmd_ask(["What is the torque?"])

    assert code == EXIT_NO_COVERAGE

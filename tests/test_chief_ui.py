"""Lightweight UI/integration tests for AI Chief Engineer Alpha."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
            "openai_api_key_env": "OPENAI_API_KEY",
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
        },
        "prefix_order": ["me-c", "maker-manuals"],
    }
    lib = tmp_path / "lib"
    lib.mkdir()
    db = tmp_path / "db"
    db.mkdir()
    path = tmp_path / "scopes.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(db))

    import rag_engine.config as cfg

    monkeypatch.setattr(cfg, "SCOPES_FILE", path)
    cfg.load_registry.cache_clear()
    return path


def _result(**kwargs):
    base = {
        "status": "ok",
        "query": "q",
        "requested_scope": None,
        "resolved_scope": "me-c",
        "answer": None,
        "sources": [],
        "retrieved_chunks": [],
        "retrieval_context": None,
        "hint": None,
        "error": None,
        "gate": None,
        "model": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_source_page_rendering(scopes_yaml):
    from rag_engine.chief_ui import format_sources_copy_text, format_sources_markdown

    sources = [
        {
            "path": "20_Vessels/.../M 1.3.pdf",
            "page": 39,  # 0-based stored → viewer p.40
            "collection": "me-c",
        }
    ]
    md = format_sources_markdown(sources, root=Path("/tmp"))
    assert "M 1.3.pdf — p.40" in md
    assert "scope: `me-c`" in md
    assert "path: `20_Vessels/.../M 1.3.pdf`" in md
    copy_text = format_sources_copy_text(sources)
    assert "M 1.3.pdf — p.40" in copy_text
    assert "scope=me-c" in copy_text


def test_retrieval_only_ok_without_nl_answer(scopes_yaml):
    from rag_engine.chief_ui import ask

    fake = _result(
        status="ok",
        answer=None,
        sources=[
            {
                "path": "manuals/M 1.3.pdf",
                "page": 39,
                "collection": "me-c",
            }
        ],
        gate="ok",
        retrieval_context="[source=manuals/M 1.3.pdf page=40]\nTorque 900 Nm.",
        retrieved_chunks=[{"path": "manuals/M 1.3.pdf", "page": 40, "text": "Torque 900 Nm."}],
    )
    with patch("rag_engine.chief_ui.answer", return_value=fake) as mocked:
        payload = ask("What is the M 1.3 exhaust valve torque?")
    mocked.assert_called_once()
    assert payload["status"] == "ok"
    assert "Hermes-owned" in payload["answer"]
    assert payload["generation_owner"] == "hermes"
    assert "M 1.3.pdf — p.40" in payload["sources_md"]
    assert payload["clarification_required"] is False


def test_explicit_m13_question_renders_ok(scopes_yaml):
    from rag_engine.chief_ui import ask

    fake = _result(
        status="ok",
        answer="Torque is 900 Nm.",
        sources=[
            {
                "path": "manuals/M 1.3.pdf",
                "page": 39,
                "collection": "me-c",
            }
        ],
        gate="ok",
    )
    with patch("rag_engine.chief_ui.answer", return_value=fake) as mocked:
        payload = ask("What is the M 1.3 exhaust valve torque?")
    mocked.assert_called_once()
    assert payload["status"] == "ok"
    assert payload["answer"] == "Torque is 900 Nm."
    assert "M 1.3.pdf — p.40" in payload["sources_md"]
    assert payload["clarification_required"] is False


def test_explicit_yanmar_question_renders_ok(scopes_yaml):
    from rag_engine.chief_ui import ask

    fake = _result(
        status="ok",
        answer="Yanmar alarm setpoint is 7.5 bar.",
        resolved_scope="maker-manuals",
        sources=[
            {
                "path": "manuals/OPERATION MANUAL_6EY22(A)LWS.pdf",
                "page": 72,
                "collection": "maker-manuals",
            }
        ],
    )
    with patch("rag_engine.chief_ui.answer", return_value=fake):
        payload = ask("Yanmar 6EY22 alarm setpoint?")
    assert payload["status"] == "ok"
    assert "7.5 bar" in payload["answer"]
    assert "p.73" in payload["sources_md"]


def test_vague_torque_triggers_clarification(scopes_yaml):
    from rag_engine.chief_ui import ask

    fake = _result(
        status="clarification_required",
        answer="Which equipment/component do you mean?",
        sources=[],
        gate="clarification_required",
    )
    with patch("rag_engine.chief_ui.answer", return_value=fake) as mocked:
        payload = ask("What is the torque?")
    assert mocked.call_args.kwargs.get("confirmation_text") is None
    assert payload["status"] == "clarification_required"
    assert payload["clarification_required"] is True
    assert "equipment/component" in payload["clarification_prompt"]
    assert payload["pending_question"] == "What is the torque?"
    assert payload["sources_md"] == "_No sources._"


def test_clarification_continue_passes_confirmation_text(scopes_yaml):
    from rag_engine.chief_ui import ask

    fake = _result(
        status="ok",
        answer="MAN M 1.3 torque is 900 Nm.",
        sources=[
            {"path": "manuals/M 1.3.pdf", "page": 39, "collection": "me-c"},
        ],
    )
    with patch("rag_engine.chief_ui.answer", return_value=fake) as mocked:
        payload = ask("What is the torque?", confirmation_text="MAN")
    assert mocked.call_args.kwargs["confirmation_text"] == "MAN"
    assert payload["status"] == "ok"
    assert payload["clarification_required"] is False
    assert "900 Nm" in payload["answer"]


def test_no_coverage_case(scopes_yaml):
    from rag_engine.chief_ui import ask

    fake = _result(
        status="no_coverage",
        answer=None,
        sources=[],
        hint="Try rag-engine explain-scope / scope-stats.",
        gate="refusal_or_weak_evidence",
    )
    with patch("rag_engine.chief_ui.answer", return_value=fake):
        payload = ask("What is the quantum flux capacitor torque?")
    assert payload["status"] == "no_coverage"
    assert "I do not know" in payload["answer"]
    assert "explain-scope" in payload["answer"]


def test_retrieval_error_message_surfaces(scopes_yaml):
    from rag_engine.chief_ui import ask, health_snapshot

    fake = _result(
        status="error",
        error="Ollama call timed out after 300.0s (RAG_OLLAMA_TIMEOUT)",
        gate="retrieval_timeout",
    )
    with patch("rag_engine.chief_ui.answer", return_value=fake):
        payload = ask("M 1.3 torque?")
    assert payload["status"] == "error"
    assert "timed out" in payload["answer"].lower()

    with patch("rag_engine.chief_ui._check_ollama_embed", return_value=(True, "ok")):
        snap = health_snapshot()
    names = {c["name"] for c in snap["checks"]}
    assert "generation_owner" in names
    assert "openai_api_key_configured" not in names


def test_provider_error_surfaces_status(scopes_yaml):
    from rag_engine.chief_ui import ask

    fake = _result(
        status="error",
        error="Rate limit exceeded",
        gate="retrieval_error",
    )
    with patch("rag_engine.chief_ui.answer", return_value=fake):
        payload = ask("M 1.3 torque?")
    assert payload["status"] == "error"
    assert payload["answer"] == "Rate limit exceeded"


def test_invalid_api_key_error_is_sanitized(scopes_yaml):
    from rag_engine.chief_ui import ask

    fake = _result(
        status="error",
        error=(
            "Error code: 401 - {'error': {'message': "
            "'Incorrect API key provided: sk-proj-ABCDEFG1234567890. "
            "You can find your API key at ...'}}"
        ),
        gate="retrieval_error",
    )
    with patch("rag_engine.chief_ui.answer", return_value=fake):
        payload = ask("M 1.3 torque?")
    assert payload["status"] == "error"
    assert "sk-proj" not in payload["answer"]
    assert "API key rejected" in payload["answer"]


def test_health_snapshot_flags(scopes_yaml, monkeypatch: pytest.MonkeyPatch):
    from rag_engine.chief_ui import format_health_markdown, health_snapshot

    with patch("rag_engine.chief_ui._check_ollama_embed", return_value=(True, "mxbai ok")):
        snap = health_snapshot()
    names = {c["name"]: c["ok"] for c in snap["checks"]}
    assert names["rag_engine_reachable"] is True
    assert names["generation_owner"] is True
    assert names["embedding_backend_available"] is True
    md = format_health_markdown(snap)
    assert "rag_engine_reachable" in md
    assert "hermes" in md.lower()


def test_build_app_has_alpha_controls(scopes_yaml):
    from app import build_app

    demo = build_app()
    assert demo is not None
    # Blocks constructed without launching a server.
    assert getattr(demo, "title", None) == "AI Chief Engineer"

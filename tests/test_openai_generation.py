from __future__ import annotations

import os
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
            "sms": {
                "description": "SMS",
                "hermes_aliases": ["sms_library"],
                "path_prefixes": ["10_Company/"],
            }
        },
        "prefix_order": ["sms"],
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


def test_openai_success_returns_text_and_usage(monkeypatch: pytest.MonkeyPatch):
    from rag_engine import openai_generation as gen

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    gen.clear_caches()
    response = SimpleNamespace(
        output_text="Grounded answer.",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18),
        id="resp_123",
    )
    fake_client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: response))

    with patch.object(gen, "_get_client", return_value=fake_client):
        result = gen.invoke_openai_response("gpt-5.6-luna", "prompt")

    assert result.text == "Grounded answer."
    assert result.usage == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    assert result.response_id == "resp_123"


def test_missing_api_key_is_reported_without_network(monkeypatch: pytest.MonkeyPatch):
    from rag_engine import openai_generation as gen

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    gen.clear_caches()

    with pytest.raises(gen.OpenAIMisconfiguredError, match="OPENAI_API_KEY is not set"):
        gen.invoke_openai_response("gpt-5.6-luna", "prompt")


def test_provider_failure_redacts_api_key(monkeypatch: pytest.MonkeyPatch):
    from rag_engine import openai_generation as gen

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    gen.clear_caches()

    class ExplodingResponses:
        def create(self, **kwargs):
            raise RuntimeError(f"provider failed for {os.environ['OPENAI_API_KEY']}")

    fake_client = SimpleNamespace(responses=ExplodingResponses())
    with patch.object(gen, "_get_client", return_value=fake_client):
        with pytest.raises(gen.OpenAIGenerationError) as excinfo:
            gen.invoke_openai_response("gpt-5.6-luna", "prompt")

    message = str(excinfo.value)
    assert "sk-secret-value" not in message
    assert "[REDACTED]" in message


def test_doctor_distinguishes_openai_generation_and_ollama_embeddings(
    scopes_yaml: Path, monkeypatch: pytest.MonkeyPatch
):
    from rag_engine.doctor import run_doctor

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    report = run_doctor(skip_ollama=True)
    checks = {item["name"]: item for item in report["checks"]}

    assert checks["openai_sdk_available"]["ok"] is True
    assert checks["openai_api_key_present"]["ok"] is True
    assert checks["openai_generation_ready"]["ok"] is True
    assert checks["ollama_embedding_reachable"]["detail"] == "skipped"
    assert checks["embed_model_available"]["detail"] == "skipped"

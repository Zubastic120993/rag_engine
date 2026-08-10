from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


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


def _run_lightweight_cli(command: list[str]) -> dict:
    script = f"""
import io
import json
import sys
import rag_engine.cli as cli
mods_before = {{name: name in sys.modules for name in ('rag_engine.query', 'rag_engine.openai_generation')}}
buf = io.StringIO()
stdout = sys.stdout
sys.stdout = buf
try:
    code = cli.main({command!r})
finally:
    sys.stdout = stdout
mods_after = {{name: name in sys.modules for name in ('rag_engine.query', 'rag_engine.openai_generation')}}
print(json.dumps({{'code': code, 'before': mods_before, 'after': mods_after, 'stdout': buf.getvalue()}}))
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("VIRTUAL_ENV", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip())


@pytest.mark.parametrize(
    "command",
    [
        ["paths"],
        ["list-scopes", "--json"],
        ["scope-stats", "--json"],
        ["doctor", "--skip-ollama", "--json"],
    ],
)
def test_lightweight_cli_commands_do_not_import_generation_stack(scopes_yaml: Path, command: list[str]):
    payload = _run_lightweight_cli(command)
    assert payload["code"] in (0, 1)
    assert payload["before"]["rag_engine.query"] is False
    assert payload["before"]["rag_engine.openai_generation"] is False
    assert payload["after"]["rag_engine.query"] is False
    assert payload["after"]["rag_engine.openai_generation"] is False

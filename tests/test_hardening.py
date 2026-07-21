"""Unit tests — no real CE_Library or live Ollama required."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# Ensure package importable
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
            "me-c": {
                "description": "ME-C",
                "hermes_aliases": [],
                "path_prefixes": [],
                "path_hints": ["ME-C", "G50ME"],
            },
            "sms": {
                "description": "SMS",
                "hermes_aliases": ["sms_library"],
                "path_prefixes": ["10_Company/"],
            },
            "wiki": {
                "description": "Wiki",
                "hermes_aliases": ["ce_wiki", "wiki_library"],
                "path_prefixes": ["90_CE_Wiki/"],
                "include_extensions": [".md"],
            },
            "maker-manuals": {
                "description": "Maker",
                "hermes_aliases": ["manual_library", "maker_manual"],
                "path_prefixes": ["00_Career/03_Engine_Knowledge/"],
                "path_hints": ["/MANUAL"],
            },
            "regulatory": {
                "description": "IMO",
                "hermes_aliases": ["imo_library", "statutory"],
                "path_prefixes": ["00_Career/02_Statutory/"],
                "path_hints": ["MARPOL"],
            },
            "inspection": {
                "description": "SIRE",
                "hermes_aliases": ["sire_library"],
                "path_prefixes": ["00_Career/02_Statutory/SIRE_OCIMF/"],
                "path_hints": ["SIRE"],
            },
            "vessels": {
                "description": "Vessels",
                "hermes_aliases": [
                    "manual_library_gaschem_europe",
                    "manual_library_gaschem_africa",
                ],
                "path_prefixes": ["20_Vessels/"],
            },
            "career": {
                "description": "Career",
                "hermes_aliases": [],
                "path_prefixes": ["00_Career/"],
            },
            "rules": {
                "description": "Rules",
                "hermes_aliases": [],
                "path_prefixes": ["99_Rules/"],
            },
            "other": {"description": "Other", "hermes_aliases": [], "path_prefixes": []},
        },
        "prefix_order": [
            "wiki",
            "sms",
            "inspection",
            "regulatory",
            "maker-manuals",
            "career",
            "vessels",
            "rules",
        ],
    }
    lib = tmp_path / "lib"
    lib.mkdir()
    path = tmp_path / "scopes.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(tmp_path / "db"))
    (tmp_path / "db").mkdir()

    import rag_engine.config as cfg
    import rag_engine.scope_rules as sr

    monkeypatch.setattr(cfg, "SCOPES_FILE", path)
    cfg.load_registry.cache_clear()
    return path


def test_registry_loads(scopes_yaml):
    from rag_engine.config import known_scopes, list_scopes

    assert "sms" in known_scopes()
    assert any(r["name"] == "vessels" for r in list_scopes())


def test_alias_resolution(scopes_yaml):
    from rag_engine.config import resolve_scope

    assert resolve_scope("sms_library") == "sms"
    assert resolve_scope("sire_library") == "inspection"
    assert resolve_scope("manual_library") == "maker-manuals"
    assert resolve_scope("manual_library_gaschem_europe") == "vessels"
    assert resolve_scope("imo_library") == "regulatory"
    assert resolve_scope("me-c") == "me-c"


def test_duplicate_alias_rejected(tmp_path, monkeypatch):
    from rag_engine.scope_rules import RegistryError, validate_registry

    bad = {
        "scopes": {
            "a": {"hermes_aliases": ["x"]},
            "b": {"hermes_aliases": ["x"]},
        }
    }
    with pytest.raises(RegistryError, match="Duplicate"):
        validate_registry(bad)


def test_alias_scope_collision_rejected():
    from rag_engine.scope_rules import RegistryError, validate_registry

    bad = {
        "scopes": {
            "sms": {"hermes_aliases": []},
            "other": {"hermes_aliases": ["sms"]},
        }
    }
    with pytest.raises(RegistryError, match="collides"):
        validate_registry(bad)


def test_path_to_scope_deterministic(scopes_yaml):
    from rag_engine.config import collection_from_relpath
    from rag_engine.scope_rules import explain_path_assignment

    cases = [
        ("90_CE_Wiki/foo_SIRE.md", "wiki"),
        ("10_Company/x.pdf", "sms"),
        ("00_Career/02_Statutory/SIRE_OCIMF/a.pdf", "inspection"),
        ("00_Career/02_Statutory/MARPOL/a.pdf", "regulatory"),
        ("00_Career/03_Engine_Knowledge/MAN_G50ME-C/x.pdf", "me-c"),
        ("00_Career/03_Engine_Knowledge/OWS/manual.pdf", "maker-manuals"),
        ("20_Vessels/Gaschem_Europe/a.pdf", "vessels"),
    ]
    for path, expect in cases:
        assert collection_from_relpath(path) == expect, path
        assert explain_path_assignment(path)["scope"] == expect


def test_should_skip_dir_prunes_tool_and_venv_paths():
    """The rag_engine repo now lives inside library_root (Tools/rag_engine/).
    Its own source tree, and either of its two virtualenvs, must be pruned
    from the ingest walk — never indexed as corpus content."""
    from rag_engine.config import should_skip_dir

    assert should_skip_dir("/Users/x/CE_Library/Tools/rag_engine/rag_engine")
    assert should_skip_dir("/Users/x/CE_Library/Tools/rag_engine/venv/lib")
    assert should_skip_dir("/Users/x/CE_Library/Tools/rag_engine/rag_env/lib")
    assert should_skip_dir("/Users/x/CE_Library/Tools/rag_engine/.git/objects")
    # Genuine corpus paths must not be pruned by the new entries.
    assert not should_skip_dir("/Users/x/CE_Library/20_Vessels/Gaschem_Europe")
    assert not should_skip_dir("/Users/x/CE_Library/90_CE_Wiki/Equipment")


def test_nfkc_query_same_as_ingest_and_idempotent():
    from rag_engine.text import normalize_text

    # micro sign U+00B5 → Greek mu U+03BC under NFKC
    raw = "temp \u00b5m"
    once = normalize_text(raw)
    twice = normalize_text(once)
    assert once == twice
    assert "\u00b5" not in once
    assert "\u03bc" in once
    # ingest and query share the same function
    import rag_engine.ingest as ingest
    import rag_engine.query as query

    assert ingest.normalize_text is normalize_text
    assert query.normalize_text is normalize_text


def test_json_contract_schema_version_and_scopes(scopes_yaml):
    from rag_engine.query import AskResult, SCHEMA_VERSION

    assert SCHEMA_VERSION == 2

    r = AskResult(
        status="ok",
        query="q",
        requested_scope="sms_library",
        resolved_scope="sms",
        answer="a",
        coverage="full",
        sources=[{"path": "p", "page": 0, "collection": "sms", "score": 0.1}],
        timings={
            "scope_resolution": 0.001,
            "retrieval": 0.1,
            "generation": 1.2,
            "total": 1.301,
        },
        model="qwen2.5:3b",
    )
    j = r.to_json()
    assert j["schema_version"] == SCHEMA_VERSION
    assert j["requested_scope"] == "sms_library"
    assert j["resolved_scope"] == "sms"
    assert j["status"] == "ok"
    assert j["coverage"] == "full"
    assert j["answer"] == "a"
    assert j["sources"][0]["path"] == "p"
    assert j["timings"]["retrieval"] == 0.1
    assert j["model"] == "qwen2.5:3b"

    partial = AskResult(
        status="partial_coverage",
        query="q",
        requested_scope="sms_library",
        resolved_scope="sms",
        answer="partial answer",
        coverage="partial",
        missing_information="torque value missing",
        sources=[{"path": "p2", "page": 2, "collection": "sms", "score": 0.3}],
    )
    jp = partial.to_json()
    assert jp["status"] == "partial_coverage"
    assert jp["sources"][0]["path"] == "p2"
    assert jp["missing_information"] == "torque value missing"

    nc = AskResult(
        status="no_coverage",
        query="q",
        requested_scope="vessels",
        resolved_scope="vessels",
        answer=None,
        coverage="none",
        sources=[{"path": "should_clear", "page": 1, "collection": "vessels", "score": 0.2}],
        hint="hint",
    )
    j2 = nc.to_json()
    assert j2["answer"] is None
    assert j2["sources"] == []
    assert j2["coverage"] == "none"
    assert j2["hint"]


def test_cli_json_stdout_exactly_one_document(scopes_yaml, capsys):
    from rag_engine.cli import _print_json

    _print_json({"schema_version": 2, "status": "ok"})
    out = capsys.readouterr().out
    assert out.count("{") >= 1
    # exactly one JSON value
    parsed = json.loads(out.strip())
    assert parsed["status"] == "ok"
    # no trailing junk
    assert out.strip() == json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def test_exit_codes_mapping():
    from rag_engine.query import EXIT_ERROR, EXIT_NO_COVERAGE, EXIT_OK

    assert EXIT_OK == 0
    assert EXIT_ERROR == 1
    assert EXIT_NO_COVERAGE == 2


def test_no_coverage_does_not_expose_training_answer(scopes_yaml):
    """When retrieval empty, answer is null — no LLM call."""
    from rag_engine.query import answer

    with patch("rag_engine.query.retrieve_with_scores", return_value=[]):
        with patch("rag_engine.query._get_llm") as llm:
            r = answer(
                "anything",
                scope="vessels",
                requested_scope="manual_library_gaschem_europe",
                scope_resolution_s=0.002,
            )
            llm.assert_not_called()
            assert r.status == "no_coverage"
            assert r.coverage == "none"
            assert r.answer is None
            assert r.sources == []
            assert r.requested_scope == "manual_library_gaschem_europe"
            assert r.resolved_scope == "vessels"
            assert r.hint
            assert "scope_resolution" in r.timings
            assert r.timings["scope_resolution"] == 0.002
            assert r.timings["generation"] is None
            assert r.timings["total"] is not None


def test_suggest_scopes_opt_in(scopes_yaml):
    from rag_engine.query import answer

    fake_doc = MagicMock()
    fake_doc.metadata = {"source": "00_Career/03_Engine_Knowledge/OWS/x.pdf", "page": 1, "collection": "maker-manuals"}
    with patch("rag_engine.query.retrieve_with_scores") as ret:
        def side_effect(q, scope=None, k=None):
            if scope == "vessels":
                return []
            if scope == "maker-manuals":
                return [(fake_doc, 0.4)]
            return []

        ret.side_effect = side_effect
        r = answer("OWS", scope="vessels", suggest_scopes=True)
        assert r.status == "no_coverage"
        assert r.answer is None
        assert "maker-manuals" in (r.hint or "")


def test_viewer_page_zero_based():
    from rag_engine.pdf_links import viewer_page

    assert viewer_page(0) == 1
    assert viewer_page(570) == 571
    assert viewer_page(None) is None


def test_safe_pdf_url_and_traversal(tmp_path, monkeypatch):
    from rag_engine.pdf_links import resolve_library_file, safe_pdf_file_url

    root = tmp_path / "lib"
    root.mkdir()
    pdf = root / "a" / "doc.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")

    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF")

    url = safe_pdf_file_url("a/doc.pdf", stored_page=0, root=root)
    assert url.startswith("/file=")
    assert "#page=1" in url
    assert "doc.pdf" in url

    with pytest.raises(ValueError, match="escapes|not found"):
        resolve_library_file(str(outside), root=root)

    # symlink escape
    link = root / "link.pdf"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        resolve_library_file("link.pdf", root=root)


def test_fingerprint_mismatch_detection(tmp_path, monkeypatch, scopes_yaml):
    from rag_engine import fingerprint as fp

    monkeypatch.setenv("RAG_EMBED_MODEL", "mxbai-embed-large")
    stored = {
        "embed_model": "nomic-embed-text",
        "llm_model": "qwen3.5:9b",
        "chunk_size": 800,
        "chunk_overlap": 100,
        "normalization": "nfkc",
    }
    live = fp.live_fingerprint()
    cmp = fp.compare_fingerprint(stored=stored, live=live)
    assert cmp["status"] == "MISMATCH"
    assert "embed_model" in cmp["diffs"]
    assert not cmp["match"]

    cmp2 = fp.compare_fingerprint(stored=live, live=live)
    assert cmp2["match"]


def test_doctor_skip_ollama_runs(scopes_yaml, tmp_path, monkeypatch):
    from rag_engine.doctor import run_doctor

    # no chroma — expect some FAILs but function returns structure
    report = run_doctor(skip_ollama=True)
    assert report["status"] in ("PASS", "FAIL")
    assert "checks" in report
    names = {c["name"] for c in report["checks"]}
    assert "library_root_exists" in names
    assert "index_fingerprint" in names


def test_explain_alias(scopes_yaml):
    from rag_engine.scope_rules import explain_alias

    info = explain_alias("manual_library_gaschem_europe")
    assert info["resolved_scope"] == "vessels"
    assert info["rule"] == "hermes_alias"
    info2 = explain_alias("manual_library")
    assert info2["resolved_scope"] == "maker-manuals"

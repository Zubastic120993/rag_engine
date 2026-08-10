"""ORCH_102B — unified human page citation contract tests."""

from __future__ import annotations

import sys
from pathlib import Path
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
                "path_prefixes": ["00_Career/"],
            },
            "sms": {
                "description": "SMS",
                "hermes_aliases": ["sms_library"],
                "path_prefixes": ["10_Company/"],
            },
        },
        "prefix_order": ["me-c", "sms"],
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


class _FakeDoc:
    def __init__(self, source: str, page, collection: str = "me-c", content: str = "chunk"):
        self.metadata = {"source": source, "page": page, "collection": collection}
        self.page_content = content


def test_viewer_page_required_cases():
    from rag_engine.pdf_links import parse_page_index, viewer_page

    assert viewer_page(0) == 1
    assert viewer_page(39) == 40
    assert viewer_page(100) == 101
    assert viewer_page(None) is None
    assert viewer_page(-1) is None
    assert viewer_page(-5) is None
    assert viewer_page("not-a-page") is None
    assert viewer_page("?") is None
    assert viewer_page("") is None
    assert parse_page_index(39) == 39
    assert parse_page_index(-1) is None


def test_m13_regression_stored_39_human_40():
    from rag_engine.pdf_links import citation_page_fields, viewer_page

    stored = 39
    assert viewer_page(stored, source=".../M 1.3.pdf") == 40
    assert citation_page_fields(stored, source="manuals/M 1.3.pdf") == {
        "page": 40,
        "page_index": 39,
    }


def test_markdown_non_pdf_no_extra_increment():
    from rag_engine.pdf_links import citation_page_fields, viewer_page

    assert viewer_page(1, source="90_CE_Wiki/note.md") == 1
    assert citation_page_fields(1, source="90_CE_Wiki/note.md") == {
        "page": 1,
        "page_index": 1,
    }


def test_sources_from_pairs_emits_human_page_and_page_index():
    from rag_engine import query

    pairs = [
        (_FakeDoc("00_Career/manuals/M 1.3.pdf", page=39), 0.2),
        (_FakeDoc("00_Career/manuals/M 1.3.pdf", page=0), 0.3),
    ]
    sources = query._sources_from_pairs(pairs)
    assert sources[0]["page"] == 40
    assert sources[0]["page_index"] == 39
    assert sources[1]["page"] == 1
    assert sources[1]["page_index"] == 0


def test_sources_from_pairs_opaque_page_preserved():
    from rag_engine import query

    sources = query._sources_from_pairs(
        [(_FakeDoc("manuals/x.pdf", page="?"), 0.4)]
    )
    assert sources[0]["page"] == "?"
    assert "page_index" not in sources[0]


def test_sources_from_pairs_does_not_mutate_document_metadata():
    from rag_engine import query

    doc = _FakeDoc("manuals/M 1.3.pdf", page=39)
    _ = query._sources_from_pairs([(doc, 0.1)])
    # enrich_metadata may add keys, but stored page index must remain 0-based.
    assert doc.metadata["page"] == 39


def test_gradio_no_double_increment_with_normalized_sources():
    from rag_engine.chief_ui import format_sources_copy_text, format_sources_markdown

    normalized = [
        {
            "path": "20_Vessels/.../M 1.3.pdf",
            "page": 40,
            "page_index": 39,
            "collection": "me-c",
        }
    ]
    md = format_sources_markdown(normalized, root=Path("/tmp"))
    assert "M 1.3.pdf — p.40" in md
    assert "p.41" not in md
    copy_text = format_sources_copy_text(normalized)
    assert "M 1.3.pdf — p.40" in copy_text


def test_gradio_legacy_zero_based_page_still_converts_once():
    from rag_engine.chief_ui import format_sources_copy_text, format_sources_markdown

    legacy = [
        {
            "path": "20_Vessels/.../M 1.3.pdf",
            "page": 39,  # legacy stored index, no page_index field
            "collection": "me-c",
        }
    ]
    md = format_sources_markdown(legacy, root=Path("/tmp"))
    assert "M 1.3.pdf — p.40" in md
    assert format_sources_copy_text(legacy).count("p.40") == 1


def test_cli_human_page_in_text_output(capsys, scopes_yaml):
    from rag_engine import cli
    from rag_engine.query import AskResult

    result = AskResult(
        status="ok",
        query="q",
        requested_scope="me-c",
        resolved_scope="me-c",
        answer="Torque is 900 Nm.",
        sources=[
            {
                "path": "manuals/M 1.3.pdf",
                "page": 40,
                "page_index": 39,
                "collection": "me-c",
                "distance": 0.2,
            }
        ],
        gate="ok",
    )

    with patch.object(cli, "resolve_scope", return_value="me-c"), patch.object(
        cli, "answer", return_value=result
    ), patch.object(cli, "log_ask_event"):
        code = cli.cmd_ask(["--scope", "me-c", "torque question"])
    assert code == 0
    out = capsys.readouterr().out
    assert "p.40" in out
    assert "p.39" not in out


def test_retrieval_context_uses_human_page(scopes_yaml):
    from rag_engine.query import answer

    doc = _FakeDoc(
        "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf",
        page=39,
        content="Exhaust valve spindle torque 2623.",
    )

    pairs = [(doc, 0.2)]
    diag = {
        "score_floor": 1.0,
        "best_raw_distance": 0.2,
        "raw_count": 1,
        "post_admissibility_count": 1,
        "post_scope_count": 1,
        "post_rerank_count": 1,
        "post_dedupe_count": 1,
        "final_retained_count": 1,
        "final_confidence_passed": True,
        "gate": "ok",
    }
    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        return_value=(pairs, diag),
    ), patch(
        "rag_engine.query._apply_final_confidence_gate",
        side_effect=lambda pairs, diagnostics=None: (pairs, diagnostics or diag),
    ), patch(
        "rag_engine.query._invoke_generation"
    ) as llm, patch(
        "rag_engine.query.retrieval_is_conservative_success", return_value=False
    ), patch(
        "rag_engine.query.is_source_only_query", return_value=False
    ):
        r = answer("M 1.3 exhaust valve torque?", scope="me-c")
        llm.assert_not_called()
    assert r.status == "ok"
    assert r.answer is None
    assert r.sources[0]["page"] == 40
    assert r.sources[0]["page_index"] == 39
    assert "page=40" in (r.retrieval_context or "")
    assert "page=39" not in (r.retrieval_context or "")

"""F-02: zero-chunk extraction must be an explicit, counted, queryable tracker
state — not a silent "ingested fine" entry indistinguishable from success —
and a loader exception must be counted separately from a loader that succeeds
but yields nothing."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

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
            "chunk_size": 800,
            "chunk_overlap": 100,
            "default_k": 5,
        },
        "scopes": {
            "maker-manuals": {
                "description": "Maker",
                "hermes_aliases": [],
                "path_prefixes": ["00_Career/03_Engine_Knowledge/"],
            },
            "other": {"description": "Other", "hermes_aliases": [], "path_prefixes": []},
        },
        "prefix_order": ["maker-manuals"],
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


def test_extraction_state_treats_missing_field_as_unknown():
    from rag_engine.ingest import extraction_state

    # The 1,673 pre-F-02 entries: no key at all. Must read as "unknown",
    # never inferred as "ok" just because the entry looks otherwise normal.
    assert extraction_state({"paths": ["x.pdf"], "chunk_ids": ["a", "b"]}) == "unknown"
    assert extraction_state({}) == "unknown"
    assert extraction_state({"extraction": "ok"}) == "ok"
    assert extraction_state({"extraction": "empty"}) == "empty"


def test_ingest_new_hash_records_empty_extraction_state_and_reports_it(scopes_yaml, monkeypatch):
    from rag_engine import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "_embed_chunks", lambda db, path, source_rel: ([], "maker-manuals", 0))
    monkeypatch.setattr(ingest_mod, "_verify_ids", lambda db, ids: True)

    tracker: dict = {}
    path_to_hash: dict = {}
    db = MagicMock()

    msg, n = ingest_mod._ingest_new_hash(
        db, tracker, path_to_hash, Path("/fake/scanned.pdf"), "scanned.pdf", "deadbeef", False
    )

    assert n == 0
    assert "empty extraction" in msg
    # Not silently marked complete: the tracker entry exists (so it isn't
    # retried forever) but is explicitly queryable as empty, not "ok".
    assert tracker["deadbeef"]["chunk_ids"] == []
    assert tracker["deadbeef"]["extraction"] == "empty"
    assert ingest_mod.extraction_state(tracker["deadbeef"]) == "empty"


def test_ingest_new_hash_normal_document_unaffected_plus_new_field(scopes_yaml, monkeypatch):
    from rag_engine import ingest as ingest_mod

    monkeypatch.setattr(
        ingest_mod, "_embed_chunks", lambda db, path, source_rel: (["id1", "id2"], "maker-manuals", 2)
    )
    monkeypatch.setattr(ingest_mod, "_verify_ids", lambda db, ids: True)

    tracker: dict = {}
    path_to_hash: dict = {}
    db = MagicMock()

    msg, n = ingest_mod._ingest_new_hash(
        db, tracker, path_to_hash, Path("/fake/manual.pdf"), "manual.pdf", "cafebabe", False
    )

    assert n == 2
    assert "empty" not in msg
    entry = tracker["cafebabe"]
    # Same shape as before this repair, plus exactly one new field.
    assert set(entry.keys()) == {"paths", "chunk_ids", "ingested_at", "collection", "extraction"}
    assert entry["paths"] == ["manual.pdf"]
    assert entry["chunk_ids"] == ["id1", "id2"]
    assert entry["collection"] == "maker-manuals"
    assert entry["extraction"] == "ok"


def test_embed_chunks_raises_extraction_error_distinct_from_empty(scopes_yaml, monkeypatch):
    from rag_engine import ingest as ingest_mod

    def boom(path):
        raise ValueError("encrypted or corrupt")

    monkeypatch.setattr(ingest_mod, "_load_documents", boom)
    db = MagicMock()

    with pytest.raises(ingest_mod.ExtractionError):
        ingest_mod._embed_chunks(db, Path("/fake/bad.pdf"), "bad.pdf")


def test_run_ingest_reports_zero_chunk_and_extraction_error_counts_separately(
    scopes_yaml, monkeypatch, capsys
):
    from rag_engine import ingest as ingest_mod

    root = ingest_mod.library_root()
    ok_path = root / "ok.pdf"
    ok_path.write_bytes(b"%PDF-1.4\nok\n")
    empty_path = root / "empty.pdf"
    empty_path.write_bytes(b"%PDF-1.4\nempty\n")
    bad_path = root / "bad.pdf"
    bad_path.write_bytes(b"%PDF-1.4\nbad\n")

    monkeypatch.setattr(
        ingest_mod,
        "_iter_docs",
        lambda: [(ok_path, "ok.pdf"), (empty_path, "empty.pdf"), (bad_path, "bad.pdf")],
    )
    monkeypatch.setattr(ingest_mod, "Chroma", lambda **kw: MagicMock())
    monkeypatch.setattr(ingest_mod, "OllamaEmbeddings", lambda **kw: MagicMock())

    def fake_ingest_new_hash(db, tracker, path_to_hash, path, rel, digest, force):
        if rel == "bad.pdf":
            raise ingest_mod.ExtractionError("boom: encrypted")
        n = 0 if rel == "empty.pdf" else 5
        tracker[digest] = {
            "paths": [rel],
            "chunk_ids": [f"id{i}" for i in range(n)],
            "collection": "other",
            "ingested_at": "2026-07-27T00:00:00+00:00",
            "extraction": "ok" if n else "empty",
        }
        path_to_hash[rel] = digest
        return f"{n:4d} chunks  [other]  NEW  {rel}", n

    monkeypatch.setattr(ingest_mod, "_ingest_new_hash", fake_ingest_new_hash)

    ingest_mod._run_ingest_locked()

    out = capsys.readouterr().out
    assert "zero-chunk extraction: 1" in out
    assert "extraction errors: 1" in out
    assert "new this run: 2" in out
    assert "EXTRACTION_ERROR" in out

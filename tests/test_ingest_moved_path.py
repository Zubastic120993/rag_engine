"""_attach_path must rewrite Chroma's per-chunk source/collection metadata
when a tracked file's content reappears at a new path and every previously
known path for that content is gone from disk (a rename/move) — not just
bump the tracker's paths list while Chroma keeps citing the stale path."""

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


def _touch(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("content", encoding="utf-8")


def test_attach_path_rewrites_chroma_metadata_on_rename(scopes_yaml, monkeypatch):
    from rag_engine.config import library_root
    from rag_engine.ingest import _attach_path

    root = library_root()
    old_rel = "00_Career/03_Engine_Knowledge/OWS/manual.pdf"
    new_rel = "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/manual.pdf"
    # Only the new path exists on disk — old_rel was moved away.
    _touch(root, new_rel)

    digest = "deadbeef"
    chunk_ids = ["id1", "id2", "id3"]
    tracker = {
        digest: {
            "paths": [old_rel],
            "chunk_ids": chunk_ids,
            "collection": "maker-manuals",
        }
    }
    path_to_hash = {old_rel: digest}
    db = MagicMock()

    msg = _attach_path(db, tracker, path_to_hash, new_rel, digest)

    assert "RENAME" in msg
    assert "DEDUPE" not in msg
    # Old path pruned, only the live path remains.
    assert tracker[digest]["paths"] == [new_rel]
    # Chroma was patched in place — no delete, no re-embed.
    db._collection.update.assert_called_once()
    _, kwargs = db._collection.update.call_args
    assert kwargs["ids"] == chunk_ids
    assert all(m["source"] == new_rel for m in kwargs["metadatas"])
    assert all(m["collection"] == "maker-manuals" for m in kwargs["metadatas"])
    assert path_to_hash[new_rel] == digest


def test_attach_path_leaves_stale_path_when_chunk_ids_empty(scopes_yaml):
    """A digest with no recorded chunk_ids can't be repaired in Chroma — the
    stale path must NOT be pruned (that would make doctor's orphan_sources
    silently go green while nothing was actually fixed), and the message
    must say RENAME, not SKIP, so it stays visible in sync output."""
    from rag_engine.config import library_root
    from rag_engine.ingest import _attach_path

    root = library_root()
    old_rel = "00_Career/03_Engine_Knowledge/OWS/manual.pdf"
    new_rel = "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/manual.pdf"
    _touch(root, new_rel)  # old_rel gone, only new_rel live

    digest = "deadbeef"
    tracker = {
        digest: {
            "paths": [old_rel],
            "chunk_ids": [],
            "collection": "maker-manuals",
        }
    }
    path_to_hash = {old_rel: digest}
    db = MagicMock()

    msg = _attach_path(db, tracker, path_to_hash, new_rel, digest)

    assert "RENAME" in msg
    assert "SKIP" not in msg
    db._collection.update.assert_not_called()
    # Stale path retained, not pruned — nothing was actually repaired.
    assert set(tracker[digest]["paths"]) == {old_rel, new_rel}


def test_attach_path_genuine_dedupe_does_not_touch_chroma(scopes_yaml):
    """Same content still lives at the old path too — not a rename, so the
    existing chunk metadata must be left alone (ambiguous canonical source)."""
    from rag_engine.config import library_root
    from rag_engine.ingest import _attach_path

    root = library_root()
    old_rel = "00_Career/03_Engine_Knowledge/OWS/manual.pdf"
    new_rel = "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/manual.pdf"
    _touch(root, old_rel)
    _touch(root, new_rel)

    digest = "deadbeef"
    tracker = {
        digest: {
            "paths": [old_rel],
            "chunk_ids": ["id1"],
            "collection": "maker-manuals",
        }
    }
    path_to_hash = {old_rel: digest}
    db = MagicMock()

    msg = _attach_path(db, tracker, path_to_hash, new_rel, digest)

    assert "DEDUPE/RENAME" in msg
    assert set(tracker[digest]["paths"]) == {old_rel, new_rel}
    db._collection.update.assert_not_called()


def test_attach_path_skip_when_path_already_known(scopes_yaml):
    from rag_engine.config import library_root
    from rag_engine.ingest import _attach_path

    root = library_root()
    rel = "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/manual.pdf"
    _touch(root, rel)

    digest = "deadbeef"
    tracker = {
        digest: {
            "paths": [rel],
            "chunk_ids": ["id1"],
            "collection": "maker-manuals",
        }
    }
    path_to_hash = {rel: digest}
    db = MagicMock()

    msg = _attach_path(db, tracker, path_to_hash, rel, digest)

    assert "SKIP" in msg
    db._collection.update.assert_not_called()
    assert tracker[digest]["paths"] == [rel]

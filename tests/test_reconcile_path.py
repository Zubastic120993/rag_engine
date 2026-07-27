from __future__ import annotations

import json
import sys
from pathlib import Path

import chromadb
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def reconcile_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    old_rel = "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/FORUM_Two_Stroke_Operation/MOP Description LGIP 1903-5.pdf"
    new_rel = "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/FORUM_Two_Stroke_Operation/MAN_ME-C-LGIP_MOP_Description_0210-0010-0035_2020-09-01.pdf"
    library = tmp_path / "lib"
    db = tmp_path / "db"
    library.mkdir()
    db.mkdir()
    scopes = {
        "defaults": {
            "library_root_env": "CE_LIBRARY_ROOT",
            "library_root_default": str(library),
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
                "path_prefixes": ["00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/"],
                "path_hints": ["ME-C", "LGIP"],
            },
            "other": {"description": "Other", "hermes_aliases": [], "path_prefixes": []},
        },
        "prefix_order": ["me-c"],
    }
    scopes_path = tmp_path / "scopes.yaml"
    scopes_path.write_text(yaml.dump(scopes), encoding="utf-8")
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(library))
    monkeypatch.setenv("RAG_DB_PATH", str(db))

    import rag_engine.config as cfg
    import rag_engine.scope_rules as sr

    monkeypatch.setattr(cfg, "SCOPES_FILE", scopes_path)
    cfg.load_registry.cache_clear()
    sr.load_registry.cache_clear()

    new_abs = library / new_rel
    new_abs.parent.mkdir(parents=True, exist_ok=True)
    content = b"%PDF-1.4\nLGIP MOP\n"
    new_abs.write_bytes(content)

    digest = __import__("hashlib").sha256(content).hexdigest()
    ids = ["id-1", "id-2", "id-3"]
    tracker = {
        digest: {
            "paths": [old_rel],
            "chunk_ids": ids,
            "collection": "me-c",
            "ingested_at": "2026-07-27T00:00:00+00:00",
        }
    }
    (db / "embedded.json").write_text(json.dumps(tracker, indent=2), encoding="utf-8")
    (db / "chroma.sqlite3").touch()

    from rag_engine.config import chroma_client_settings

    client = chromadb.PersistentClient(path=str(db), settings=chroma_client_settings())
    col = client.get_or_create_collection("langchain")
    col.add(
        ids=ids,
        documents=["chunk one", "chunk two", "chunk three"],
        metadatas=[
            {"source": old_rel, "collection": "me-c", "page": 0, "tag": "a"},
            {"source": old_rel, "collection": "me-c", "page": 1, "tag": "b"},
            {"source": old_rel, "collection": "me-c", "page": 2, "tag": "c"},
        ],
        embeddings=[[0.1, 0.2], [0.2, 0.3], [0.3, 0.4]],
    )

    yield {
        "library": library,
        "db": db,
        "old_rel": old_rel,
        "new_rel": new_rel,
        "digest": digest,
        "ids": ids,
        "scopes_path": scopes_path,
    }


def _client(env):
    from rag_engine.config import chroma_client_settings

    client = chromadb.PersistentClient(path=str(env["db"]), settings=chroma_client_settings())
    return client, client.get_collection("langchain")


def test_successful_dry_run(reconcile_env):
    from rag_engine.reconcile_path import EXIT_OK, STATUS_DRY_RUN_OK, run_reconcile_path

    payload, code = run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_OK
    assert payload["status"] == STATUS_DRY_RUN_OK
    assert payload["validation"]["tracker_ids_match_chroma_ids"] is True
    assert payload["chroma"]["old_source_count"] == 3
    assert payload["chroma"]["new_source_count"] == 0


def test_successful_commit_using_temporary_tracker_and_chroma(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "PASS", "checks": []})
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {"me-c": {"document_count": 1}}, "total_chunks": 111878})

    payload, code = rp.run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True)
    assert code == rp.EXIT_OK
    assert payload["status"] == rp.STATUS_COMMITTED_AND_VERIFIED
    tracker = json.loads((reconcile_env["db"] / "embedded.json").read_text(encoding="utf-8"))
    assert tracker[reconcile_env["digest"]]["paths"] == [reconcile_env["new_rel"]]
    client, col = _client(reconcile_env)
    assert len(col.get(where={"source": reconcile_env["old_rel"]}, include=["metadatas"])["ids"]) == 0
    new_raw = col.get(where={"source": reconcile_env["new_rel"]}, include=["metadatas"])
    assert new_raw["ids"] == reconcile_env["ids"]
    assert [m["page"] for m in new_raw["metadatas"]] == [0, 1, 2]
    assert [m["tag"] for m in new_raw["metadatas"]] == ["a", "b", "c"]
    assert all(m["collection"] == "me-c" for m in new_raw["metadatas"])
    assert payload["artifacts"]["rollback_json"]
    assert client is not None


def test_total_chunks_guard_passes_when_pre_equals_post(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "PASS", "checks": []})
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {}, "total_chunks": 42})

    payload, code = rp.run_reconcile_path(
        reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True
    )
    assert code == rp.EXIT_OK
    assert payload["status"] == rp.STATUS_COMMITTED_AND_VERIFIED


def test_total_chunks_guard_fails_when_pre_differs_from_post(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "PASS", "checks": []})
    counts = iter([111878, 111877])  # pre-mutation, then post-mutation — one chunk vanished
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {}, "total_chunks": next(counts)})

    payload, code = rp.run_reconcile_path(
        reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True
    )
    assert code == rp.EXIT_FAILED_ROLLED_BACK
    assert payload["status"] == rp.STATUS_FAILED_ROLLED_BACK
    assert "pre=111878" in payload["message"]
    assert "post=111877" in payload["message"]
    _, col = _client(reconcile_env)
    assert len(col.get(where={"source": reconcile_env["old_rel"]}, include=["metadatas"])["ids"]) == 3
    assert len(col.get(where={"source": reconcile_env["new_rel"]}, include=["metadatas"])["ids"]) == 0


def test_total_chunks_guard_fails_closed_when_pre_value_unobtainable(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "PASS", "checks": []})

    def assert_raises_and_no_mutation():
        with pytest.raises(rp.ReconcileFailure):
            rp.run_reconcile_path(
                reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True
            )
        _, col = _client(reconcile_env)
        assert len(col.get(where={"source": reconcile_env["old_rel"]}, include=["metadatas"])["ids"]) == 3
        assert len(col.get(where={"source": reconcile_env["new_rel"]}, include=["metadatas"])["ids"]) == 0
        tracker = json.loads((reconcile_env["db"] / "embedded.json").read_text(encoding="utf-8"))
        assert tracker[reconcile_env["digest"]]["paths"] == [reconcile_env["old_rel"]]

    # Branch 1: scope_stats() reports an error.
    monkeypatch.setattr(rp, "scope_stats", lambda: {"error": "chroma unavailable", "scopes": {}})
    assert_raises_and_no_mutation()

    # Branch 2: scope_stats() succeeds but omits "total_chunks" entirely.
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {}})
    assert_raises_and_no_mutation()


def test_total_chunks_guard_independent_of_corpus_size(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "PASS", "checks": []})
    # Far from the old hardcoded 111878 literal — the pre-repair guard would have
    # raised here even though nothing changed between pre and post.
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {}, "total_chunks": 250000})

    payload, code = rp.run_reconcile_path(
        reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True
    )
    assert code == rp.EXIT_OK
    assert payload["status"] == rp.STATUS_COMMITTED_AND_VERIFIED
    assert payload["scope_stats"]["total_chunks"] == 250000


def test_old_path_still_exists(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, STATUS_REFUSED, run_reconcile_path

    (reconcile_env["library"] / reconcile_env["old_rel"]).write_text("old", encoding="utf-8")
    payload, code = run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_REFUSED
    assert payload["status"] == STATUS_REFUSED
    assert "old_path still exists" in payload["message"]


def test_new_path_missing(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, run_reconcile_path

    (reconcile_env["library"] / reconcile_env["new_rel"]).unlink()
    payload, code = run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_REFUSED
    assert "new_path does not exist" in payload["message"]


def test_sha_mismatch(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, run_reconcile_path

    payload, code = run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], "0" * 64, commit=False)
    assert code == EXIT_REFUSED
    assert "expected_sha256 mismatch" in payload["message"]


def test_tracker_old_entry_missing(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, run_reconcile_path

    (reconcile_env["db"] / "embedded.json").write_text("{}", encoding="utf-8")
    payload, code = run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_REFUSED
    assert "tracker old entry is ambiguous" in payload["message"]


def test_tracker_new_entry_conflict(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, run_reconcile_path

    tracker = json.loads((reconcile_env["db"] / "embedded.json").read_text(encoding="utf-8"))
    tracker["another"] = {"paths": [reconcile_env["new_rel"]], "chunk_ids": ["z"], "collection": "me-c"}
    (reconcile_env["db"] / "embedded.json").write_text(json.dumps(tracker), encoding="utf-8")
    payload, code = run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_REFUSED
    assert "tracker new path already exists" in payload["message"]


def test_chroma_old_source_missing(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, run_reconcile_path

    _, col = _client(reconcile_env)
    col.delete(ids=reconcile_env["ids"])
    payload, code = run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_REFUSED
    assert "Chroma old source count is zero" in payload["message"]


def test_chroma_new_source_conflict(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, run_reconcile_path

    _, col = _client(reconcile_env)
    col.add(ids=["new-id"], documents=["x"], metadatas=[{"source": reconcile_env["new_rel"], "collection": "me-c", "page": 0}], embeddings=[[0.9, 0.9]])
    payload, code = run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_REFUSED
    assert "Chroma new source already has chunks" in payload["message"]


def test_lock_acquisition_failure(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp
    from rag_engine.lock import IngestLockError

    monkeypatch.setattr(rp, "ingest_lock", lambda timeout_s=0: (_ for _ in ()).throw(IngestLockError("locked")))
    payload, code = rp.run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True)
    assert code == rp.EXIT_REFUSED
    assert payload["status"] == rp.STATUS_REFUSED
    assert "locked" in payload["message"]


def test_failure_after_chroma_update_triggers_rollback(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "PASS", "checks": []})
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {}, "total_chunks": 111878})
    real_write = rp._write_tracker_atomic

    def fail_once(tracker):
        raise RuntimeError("tracker write failed")

    monkeypatch.setattr(rp, "_write_tracker_atomic", fail_once)
    payload, code = rp.run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True)
    assert code == rp.EXIT_FAILED_ROLLED_BACK
    assert payload["status"] == rp.STATUS_FAILED_ROLLED_BACK
    _, col = _client(reconcile_env)
    assert len(col.get(where={"source": reconcile_env["old_rel"]}, include=["metadatas"])["ids"]) == 3
    assert len(col.get(where={"source": reconcile_env["new_rel"]}, include=["metadatas"])["ids"]) == 0
    tracker = json.loads((reconcile_env["db"] / "embedded.json").read_text(encoding="utf-8"))
    assert tracker[reconcile_env["digest"]]["paths"] == [reconcile_env["old_rel"]]
    monkeypatch.setattr(rp, "_write_tracker_atomic", real_write)


def test_failure_after_tracker_update_triggers_rollback(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "FAIL", "checks": [{"name": "doctor", "ok": False}]})
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {}, "total_chunks": 111878})
    payload, code = rp.run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True)
    assert code == rp.EXIT_FAILED_ROLLED_BACK
    assert payload["status"] == rp.STATUS_FAILED_ROLLED_BACK
    _, col = _client(reconcile_env)
    assert len(col.get(where={"source": reconcile_env["old_rel"]}, include=["metadatas"])["ids"]) == 3
    assert len(col.get(where={"source": reconcile_env["new_rel"]}, include=["metadatas"])["ids"]) == 0
    tracker = json.loads((reconcile_env["db"] / "embedded.json").read_text(encoding="utf-8"))
    assert tracker[reconcile_env["digest"]]["paths"] == [reconcile_env["old_rel"]]


def test_chunk_ids_unchanged(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "PASS", "checks": []})
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {}, "total_chunks": 111878})
    payload, _ = rp.run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True)
    assert payload["chroma_before"]["ids_sha256"] == payload["chroma_after"]["ids_sha256"]


def test_metadata_other_than_source_remains_unchanged(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "PASS", "checks": []})
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {}, "total_chunks": 111878})
    rp.run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True)
    _, col = _client(reconcile_env)
    raw = col.get(where={"source": reconcile_env["new_rel"]}, include=["metadatas"])
    assert [m["tag"] for m in raw["metadatas"]] == ["a", "b", "c"]
    assert [m["page"] for m in raw["metadatas"]] == [0, 1, 2]


def test_dry_run_performs_no_writes(reconcile_env):
    from rag_engine.reconcile_path import run_reconcile_path

    before_tracker = (reconcile_env["db"] / "embedded.json").read_text(encoding="utf-8")
    before_sqlite = (reconcile_env["db"] / "chroma.sqlite3").read_bytes()
    run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert (reconcile_env["db"] / "embedded.json").read_text(encoding="utf-8") == before_tracker
    assert (reconcile_env["db"] / "chroma.sqlite3").read_bytes() == before_sqlite


def test_malformed_tracker(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, run_reconcile_path

    (reconcile_env["db"] / "embedded.json").write_text("{bad", encoding="utf-8")
    payload, code = run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_REFUSED
    assert "malformed tracker JSON" in payload["message"]


def test_absolute_path_rejected(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, run_reconcile_path

    payload, code = run_reconcile_path("/absolute/path.pdf", reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_REFUSED
    assert "relative CE_Library path" in payload["message"]


def test_dotdot_path_rejected(reconcile_env):
    from rag_engine.reconcile_path import EXIT_REFUSED, run_reconcile_path

    payload, code = run_reconcile_path("../escape.pdf", reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert code == EXIT_REFUSED
    assert "must not contain .." in payload["message"]


def test_json_output_contract(reconcile_env, capsys):
    import json as pyjson
    from rag_engine.reconcile_path import cmd_reconcile_path, STATUS_DRY_RUN_OK

    code = cmd_reconcile_path([
        "--old-path", reconcile_env["old_rel"],
        "--new-path", reconcile_env["new_rel"],
        "--expected-sha256", reconcile_env["digest"],
        "--json",
    ])
    out = capsys.readouterr().out.strip()
    payload = pyjson.loads(out)
    assert code == 0
    assert payload["status"] == STATUS_DRY_RUN_OK
    assert payload["old_path"] == reconcile_env["old_rel"]
    assert payload["new_path"] == reconcile_env["new_rel"]


def test_exit_code_contract(reconcile_env, monkeypatch):
    import rag_engine.reconcile_path as rp

    ok_payload, ok_code = rp.run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert ok_payload["status"] == rp.STATUS_DRY_RUN_OK
    assert ok_code == rp.EXIT_OK

    refused_payload, refused_code = rp.run_reconcile_path("/bad", reconcile_env["new_rel"], reconcile_env["digest"], commit=False)
    assert refused_payload["status"] == rp.STATUS_REFUSED
    assert refused_code == rp.EXIT_REFUSED

    monkeypatch.setattr(rp, "run_doctor", lambda: {"status": "FAIL", "checks": []})
    monkeypatch.setattr(rp, "scope_stats", lambda: {"scopes": {}, "total_chunks": 111878})
    failed_payload, failed_code = rp.run_reconcile_path(reconcile_env["old_rel"], reconcile_env["new_rel"], reconcile_env["digest"], commit=True)
    assert failed_payload["status"] == rp.STATUS_FAILED_ROLLED_BACK
    assert failed_code == rp.EXIT_FAILED_ROLLED_BACK

"""Phase 4 reconciliation tests — temporary fixtures only; no production mutation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from rag_engine.metadata_registry import (
    initialize_registry,
    open_registry,
    register_chunk,
    register_document_version,
    register_source_file,
    register_subject,
    register_vector_mapping,
    registry_transaction,
)
from rag_engine.reconciliation import (
    ReconciliationState,
    ReasonCode,
    load_chroma_snapshot_readonly,
    load_tracker_readonly,
    reconcile,
    summarize_reconciliation,
)
from rag_engine.reconciliation.chroma_reader import ChromaReadError
from rag_engine.reconciliation.engine import reconcile_paths
from rag_engine.reconciliation.report import results_to_jsonable
from rag_engine.reconciliation.tracker_reader import TrackerReadError, audit_tracker
from rag_engine.stable_identity import (
    IDENTITY_SCHEME_VERSION,
    chunk_id,
    chunking_fingerprint,
    default_chunking_contract,
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_pending,
)

ROOT = Path(__file__).resolve().parents[1]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_tracker(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _make_chroma_sqlite(
    path: Path,
    records: list[dict],
    *,
    collection_name: str = "langchain",
) -> Path:
    """Create a minimal Chroma-like sqlite for identity metadata tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                dimension INTEGER,
                database_id TEXT,
                config_json_str TEXT
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                type TEXT,
                scope TEXT,
                collection TEXT REFERENCES collections(id)
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL,
                seq_id BLOB NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (segment_id, embedding_id)
            );
            CREATE TABLE embedding_metadata (
                id INTEGER REFERENCES embeddings(id),
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (id, key)
            );
            """
        )
        coll_id = str(uuid.uuid4())
        seg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO collections (id, name) VALUES (?, ?)",
            (coll_id, collection_name),
        )
        conn.execute(
            "INSERT INTO segments (id, type, scope, collection) VALUES (?, ?, ?, ?)",
            (seg_id, "vector", "VECTOR", coll_id),
        )
        for i, rec in enumerate(records):
            eid = rec["id"]
            conn.execute(
                "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) "
                "VALUES (?, ?, ?, ?)",
                (i + 1, seg_id, eid, b"\x00"),
            )
            if "source" in rec and rec["source"] is not None:
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                    (i + 1, "source", rec["source"]),
                )
            if "page" in rec and rec["page"] is not None:
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, int_value) VALUES (?, ?, ?)",
                    (i + 1, "page", int(rec["page"])),
                )
            if "collection" in rec and rec["collection"] is not None:
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                    (i + 1, "collection", rec["collection"]),
                )
        conn.commit()
    finally:
        conn.close()
    return path


def _file_stat(path: Path) -> dict:
    st = path.stat()
    return {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


def test_tracker_loads_and_audit(tmp_path: Path) -> None:
    digest = _sha(b"doc-a")
    cid = str(uuid.uuid4())
    path = _write_tracker(
        tmp_path / "embedded.json",
        {
            digest: {
                "paths": ["a/b.pdf", "a/b-copy.pdf"],
                "chunk_ids": [cid],
                "collection": "maker",
            },
            _sha(b"empty"): {
                "paths": ["empty.pdf"],
                "chunk_ids": [],
                "collection": "maker",
            },
        },
    )
    before = _file_stat(path)
    records = load_tracker_readonly(path)
    after = _file_stat(path)
    assert before == after
    assert len(records) == 2
    audit = audit_tracker(records)
    assert audit["multi_path_digests"] == 1
    assert audit["zero_chunk_records"] == 1
    assert audit["tracker_chunk_ids"] == 1


def test_malformed_tracker_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(TrackerReadError):
        load_tracker_readonly(path)


def test_duplicate_tracker_chunk_ids_detected(tmp_path: Path) -> None:
    cid = str(uuid.uuid4())
    d1, d2 = _sha(b"1"), _sha(b"2")
    path = _write_tracker(
        tmp_path / "embedded.json",
        {
            d1: {"paths": ["a.pdf"], "chunk_ids": [cid], "collection": "x"},
            d2: {"paths": ["b.pdf"], "chunk_ids": [cid], "collection": "x"},
        },
    )
    chroma = _make_chroma_sqlite(
        tmp_path / "chroma.sqlite3",
        [{"id": cid, "source": "a.pdf", "page": 0, "collection": "x"}],
    )
    tracker = load_tracker_readonly(path)
    ch = load_chroma_snapshot_readonly(chroma)
    results, _ = reconcile(tracker=tracker, chroma=ch, hash_existing_sources=False)
    dup = [r for r in results if r.state == ReconciliationState.DUPLICATE_ACTIVE]
    assert dup
    assert ReasonCode.DUPLICATE_CHUNK_OWNERSHIP in dup[0].reason_codes


# ---------------------------------------------------------------------------
# Chroma snapshot
# ---------------------------------------------------------------------------


def test_chroma_snapshot_preserves_ids(tmp_path: Path) -> None:
    cid = str(uuid.uuid4())
    db = _make_chroma_sqlite(
        tmp_path / "chroma.sqlite3",
        [{"id": cid, "source": "docs/a.pdf", "page": 2, "collection": "sms"}],
    )
    before = _file_stat(db)
    records = load_chroma_snapshot_readonly(db)
    after = _file_stat(db)
    assert before == after
    assert cid in records
    assert records[cid].source_path == "docs/a.pdf"
    assert records[cid].page == 2


def test_chroma_missing_source_metadata(tmp_path: Path) -> None:
    cid = str(uuid.uuid4())
    db = _make_chroma_sqlite(tmp_path / "chroma.sqlite3", [{"id": cid}])
    records = load_chroma_snapshot_readonly(db)
    assert records[cid].source_path is None


def test_chroma_readonly_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ChromaReadError):
        load_chroma_snapshot_readonly(tmp_path / "nope.sqlite3")


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


def test_match(tmp_path: Path) -> None:
    data = b"match-bytes"
    digest = source_hash_from_bytes(data)
    lib = tmp_path / "lib"
    rel = "docs/match.pdf"
    (lib / "docs").mkdir(parents=True)
    (lib / rel).write_bytes(data)
    c0, c1 = str(uuid.uuid4()), str(uuid.uuid4())
    tracker = load_tracker_readonly(
        _write_tracker(
            tmp_path / "embedded.json",
            {
                digest: {
                    "paths": [rel],
                    "chunk_ids": [c0, c1],
                    "collection": "maker",
                }
            },
        )
    )
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "chroma.sqlite3",
            [
                {"id": c0, "source": rel, "page": 0, "collection": "maker"},
                {"id": c1, "source": rel, "page": 1, "collection": "maker"},
            ],
        )
    )
    results, summary = reconcile(
        tracker=tracker, chroma=chroma, library_root=lib, hash_existing_sources=True
    )
    tracker_results = [r for r in results if r.unit_kind == "tracker_digest"]
    assert len(tracker_results) == 1
    assert tracker_results[0].state == ReconciliationState.MATCH
    assert tracker_results[0].document_id == document_id_from_bytes(data)
    assert summary.by_state["MATCH"] >= 1


def test_hash_mismatch(tmp_path: Path) -> None:
    historical = b"old-bytes"
    current = b"new-bytes"
    digest = source_hash_from_bytes(historical)
    lib = tmp_path / "lib"
    rel = "docs/x.pdf"
    (lib / "docs").mkdir(parents=True)
    (lib / rel).write_bytes(current)
    cid = str(uuid.uuid4())
    tracker = load_tracker_readonly(
        _write_tracker(
            tmp_path / "t.json",
            {digest: {"paths": [rel], "chunk_ids": [cid], "collection": "c"}},
        )
    )
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "c.sqlite3",
            [{"id": cid, "source": rel, "page": 0, "collection": "c"}],
        )
    )
    results, _ = reconcile(
        tracker=tracker, chroma=chroma, library_root=lib, hash_existing_sources=True
    )
    tr = [r for r in results if r.unit_kind == "tracker_digest"][0]
    assert tr.state == ReconciliationState.HASH_MISMATCH
    assert tr.historical_source_hash == digest
    assert tr.current_observed_source_hash == source_hash_from_bytes(current)


def test_chunk_count_mismatch(tmp_path: Path) -> None:
    digest = _sha(b"ccm")
    c0, c1 = str(uuid.uuid4()), str(uuid.uuid4())
    tracker = load_tracker_readonly(
        _write_tracker(
            tmp_path / "t.json",
            {digest: {"paths": ["a.pdf"], "chunk_ids": [c0, c1], "collection": "c"}},
        )
    )
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "c.sqlite3",
            [{"id": c0, "source": "a.pdf", "page": 0, "collection": "c"}],
        )
    )
    results, _ = reconcile(tracker=tracker, chroma=chroma, hash_existing_sources=False)
    tr = [r for r in results if r.unit_kind == "tracker_digest"][0]
    assert tr.state == ReconciliationState.CHUNK_COUNT_MISMATCH
    assert ReasonCode.MISSING_CHROMA_ID in tr.reason_codes


def test_metadata_mismatch_path_drift(tmp_path: Path) -> None:
    digest = _sha(b"meta")
    cid = str(uuid.uuid4())
    tracker = load_tracker_readonly(
        _write_tracker(
            tmp_path / "t.json",
            {
                digest: {
                    "paths": ["old/path.pdf"],
                    "chunk_ids": [cid],
                    "collection": "c",
                }
            },
        )
    )
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "c.sqlite3",
            [{"id": cid, "source": "new/path.pdf", "page": 0, "collection": "c"}],
        )
    )
    results, _ = reconcile(tracker=tracker, chroma=chroma, hash_existing_sources=False)
    tr = [r for r in results if r.unit_kind == "tracker_digest"][0]
    assert tr.state == ReconciliationState.METADATA_MISMATCH
    assert ReasonCode.PATH_DRIFT in tr.reason_codes


def test_chroma_only(tmp_path: Path) -> None:
    orphan = str(uuid.uuid4())
    tracker = load_tracker_readonly(_write_tracker(tmp_path / "t.json", {}))
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "c.sqlite3",
            [{"id": orphan, "source": "orphan.pdf", "page": 0, "collection": "c"}],
        )
    )
    results, summary = reconcile(
        tracker=tracker, chroma=chroma, hash_existing_sources=False
    )
    assert any(r.state == ReconciliationState.CHROMA_ONLY for r in results)
    assert summary.by_state["CHROMA_ONLY"] == 1


def test_registry_only(tmp_path: Path) -> None:
    data = b"registry-only-bytes"
    digest = source_hash_from_bytes(data)
    doc = document_id_from_bytes(data)
    sid = subject_id_pending(digest)
    fp = chunking_fingerprint(default_chunking_contract())
    c0 = chunk_id(doc, fp, 0)
    db = tmp_path / "registry" / "metadata_registry_v1.sqlite3"
    initialize_registry(db)
    with open_registry(db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            register_document_version(
                conn, document_id=doc, subject_id=sid, source_hash=digest
            )
            register_source_file(conn, document_id=doc, relative_path="only/reg.pdf")
            register_chunk(
                conn,
                chunk_id=c0,
                document_id=doc,
                chunking_fingerprint=fp,
                ordinal=0,
            )
            register_vector_mapping(
                conn,
                chunk_id=c0,
                chroma_embedding_id=str(uuid.uuid4()),
                mapping_status="legacy_uuid",
            )
    tracker = load_tracker_readonly(_write_tracker(tmp_path / "t.json", {}))
    chroma = load_chroma_snapshot_readonly(_make_chroma_sqlite(tmp_path / "c.sqlite3", []))
    from rag_engine.reconciliation.registry_snapshot import (
        load_registry_snapshot_readonly,
    )

    registry = load_registry_snapshot_readonly(db)
    results, summary = reconcile(
        tracker=tracker,
        chroma=chroma,
        registry=registry,
        hash_existing_sources=False,
    )
    assert summary.by_state["REGISTRY_ONLY"] >= 1
    assert any(r.state == ReconciliationState.REGISTRY_ONLY for r in results)


def test_unknown_missing_source_file(tmp_path: Path) -> None:
    data = b"missing-file"
    digest = source_hash_from_bytes(data)
    lib = tmp_path / "lib"
    lib.mkdir()
    cid = str(uuid.uuid4())
    rel = "gone.pdf"
    tracker = load_tracker_readonly(
        _write_tracker(
            tmp_path / "t.json",
            {digest: {"paths": [rel], "chunk_ids": [cid], "collection": "c"}},
        )
    )
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "c.sqlite3",
            [{"id": cid, "source": rel, "page": 0, "collection": "c"}],
        )
    )
    results, _ = reconcile(
        tracker=tracker, chroma=chroma, library_root=lib, hash_existing_sources=True
    )
    tr = [r for r in results if r.unit_kind == "tracker_digest"][0]
    assert tr.state == ReconciliationState.UNKNOWN
    assert ReasonCode.MISSING_SOURCE_FILE in tr.reason_codes


def test_stable_chunk_mapping_when_provable(tmp_path: Path) -> None:
    data = b"stable-map"
    digest = source_hash_from_bytes(data)
    doc = document_id_from_bytes(data)
    fp = chunking_fingerprint(default_chunking_contract())
    c0 = str(uuid.uuid4())
    expected_stable = chunk_id(doc, fp, 0)
    tracker = load_tracker_readonly(
        _write_tracker(
            tmp_path / "t.json",
            {digest: {"paths": ["a.pdf"], "chunk_ids": [c0], "collection": "c"}},
        )
    )
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "c.sqlite3",
            [{"id": c0, "source": "a.pdf", "page": 0, "collection": "c"}],
        )
    )
    results, summary = reconcile(
        tracker=tracker,
        chroma=chroma,
        hash_existing_sources=False,
        historical_chunking_fingerprint=fp,
    )
    tr = [r for r in results if r.unit_kind == "tracker_digest"][0]
    assert tr.stable_chunk_ids[0] == expected_stable
    assert tr.stable_chunk_ids[0] != c0
    assert summary.stable_chunk_ids_proven == 1
    assert ReasonCode.STABLE_CHUNK_PROVEN in tr.reason_codes


def test_do_not_guess_stable_chunk_id_from_defaults(tmp_path: Path) -> None:
    data = b"no-guess"
    digest = source_hash_from_bytes(data)
    c0 = str(uuid.uuid4())
    tracker = load_tracker_readonly(
        _write_tracker(
            tmp_path / "t.json",
            {digest: {"paths": ["a.pdf"], "chunk_ids": [c0], "collection": "c"}},
        )
    )
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "c.sqlite3",
            [{"id": c0, "source": "a.pdf", "page": 0, "collection": "c"}],
        )
    )
    # Provide index fingerprint (partial) but NOT explicit historical fingerprint.
    results, summary = reconcile(
        tracker=tracker,
        chroma=chroma,
        hash_existing_sources=False,
        index_fingerprint={
            "embed_model": "mxbai-embed-large",
            "chunk_size": 800,
            "chunk_overlap": 100,
            "normalization": "nfkc",
        },
        historical_chunking_fingerprint=None,
    )
    tr = [r for r in results if r.unit_kind == "tracker_digest"][0]
    assert tr.stable_chunk_ids == (None,)
    assert ReasonCode.FINGERPRINT_UNKNOWN in tr.reason_codes
    assert summary.stable_chunk_ids_proven == 0
    assert tr.evidence["fingerprint_evidence"]["used_current_defaults"] is False
    # Ensure we did not silently compute default fingerprint mapping
    default_fp = chunking_fingerprint(default_chunking_contract())
    guessed = chunk_id(document_id_from_bytes(data), default_fp, 0)
    assert guessed not in tr.stable_chunk_ids


def test_readonly_paths_unchanged(tmp_path: Path) -> None:
    data = b"ro"
    digest = source_hash_from_bytes(data)
    lib = tmp_path / "lib"
    rel = "x.pdf"
    lib.mkdir()
    (lib / rel).write_bytes(data)
    cid = str(uuid.uuid4())
    tpath = _write_tracker(
        tmp_path / "embedded.json",
        {digest: {"paths": [rel], "chunk_ids": [cid], "collection": "c"}},
    )
    cpath = _make_chroma_sqlite(
        tmp_path / "chroma.sqlite3",
        [{"id": cid, "source": rel, "page": 0, "collection": "c"}],
    )
    before_t, before_c = _file_stat(tpath), _file_stat(cpath)
    reconcile_paths(
        tracker_path=tpath,
        chroma_sqlite_path=cpath,
        library_root=lib,
        hash_existing_sources=True,
    )
    assert _file_stat(tpath) == before_t
    assert _file_stat(cpath) == before_c
    # No journal/WAL left beside sources
    for p in tmp_path.iterdir():
        assert not str(p).endswith("-journal")
        assert not str(p).endswith("-wal")
        assert not str(p).endswith("-shm")


def test_determinism_repeat(tmp_path: Path) -> None:
    digest = _sha(b"det")
    cid = str(uuid.uuid4())
    tracker = load_tracker_readonly(
        _write_tracker(
            tmp_path / "t.json",
            {digest: {"paths": ["a.pdf"], "chunk_ids": [cid], "collection": "c"}},
        )
    )
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "c.sqlite3",
            [{"id": cid, "source": "a.pdf", "page": 0, "collection": "c"}],
        )
    )
    r1, s1 = reconcile(tracker=tracker, chroma=chroma, hash_existing_sources=False)
    r2, s2 = reconcile(tracker=tracker, chroma=chroma, hash_existing_sources=False)
    j1 = results_to_jsonable(r1, s1, meta={"t": "x"})
    j2 = results_to_jsonable(r2, s2, meta={"t": "x"})
    # Drop generated_at timestamp
    j1["meta"].pop("generated_at", None)
    j2["meta"].pop("generated_at", None)
    assert j1 == j2
    assert summarize_reconciliation(r1) == summarize_reconciliation(r2)


def test_import_boundary_subprocess() -> None:
    script = r"""
import sys
before = set(sys.modules)
import rag_engine.reconciliation as rec
after = set(sys.modules)
loaded = sorted(after - before)
forbidden = [n for n in loaded if any(x in n.lower() for x in ('chromadb','openai','langchain'))]
print('IMPORTED_OK')
print('FORBIDDEN', ','.join(forbidden))
from pathlib import Path
print('RAG_STATE', Path('/Users/vladymyrzub/CE_Library/.rag_state').exists())
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "IMPORTED_OK" in proc.stdout
    assert "FORBIDDEN \n" in proc.stdout or proc.stdout.strip().endswith("FORBIDDEN")
    assert "RAG_STATE False" in proc.stdout


def test_precedence_hash_over_metadata(tmp_path: Path) -> None:
    historical = b"old"
    current = b"new"
    digest = source_hash_from_bytes(historical)
    lib = tmp_path / "lib"
    (lib / "docs").mkdir(parents=True)
    rel = "docs/a.pdf"
    (lib / rel).write_bytes(current)
    cid = str(uuid.uuid4())
    tracker = load_tracker_readonly(
        _write_tracker(
            tmp_path / "t.json",
            {digest: {"paths": [rel], "chunk_ids": [cid], "collection": "c"}},
        )
    )
    # Path also drifts
    chroma = load_chroma_snapshot_readonly(
        _make_chroma_sqlite(
            tmp_path / "c.sqlite3",
            [{"id": cid, "source": "other/path.pdf", "page": 0, "collection": "c"}],
        )
    )
    results, _ = reconcile(
        tracker=tracker, chroma=chroma, library_root=lib, hash_existing_sources=True
    )
    tr = [r for r in results if r.unit_kind == "tracker_digest"][0]
    assert tr.state == ReconciliationState.HASH_MISMATCH

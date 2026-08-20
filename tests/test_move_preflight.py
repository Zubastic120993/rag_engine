"""Isolated tests for read-only pre-move evidence collection (Phase E0)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from rag_engine.library_state.contract import CLASS_INDEXED_OK, INTENT_PLAN_MOVE, OP_NO_OP
from rag_engine.library_state.move_approval import (
    MoveApprovalContext,
    compute_approval_digest,
    plan_digest,
    validate_move_approval,
)
from rag_engine.library_state.move_preflight import (
    PreMoveEvidenceError,
    collect_pre_move_evidence,
)
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
from rag_engine.stable_identity import (
    chunk_id,
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
)

OLD = "00_Career/03_Engine_Knowledge/MAN_Academy/MAN_ME-C_LGIP_old_name.pdf"
NEW = "00_Career/03_Engine_Knowledge/MAN_Academy/MAN_ME-C_LGIP_new_name.pdf"

BYTES_A = b"%PDF-1.4\nMAN Academy LGIP A\n"
HASH_A = source_hash_from_bytes(BYTES_A)
DOC_A = document_id_from_bytes(BYTES_A)
FP = "ab" * 32
ID1 = chunk_id(DOC_A, FP, 0)
ID2 = chunk_id(DOC_A, FP, 1)
CHUNK_IDS = (ID1, ID2)
SUBJECT = subject_id_from_key("maker_doc", "man-academy-lgip")

ISSUED_AT = "2026-08-19T12:00:00Z"
EXPIRES_AT = "2026-08-19T12:15:00Z"
NOW_VALID = "2026-08-19T12:05:00Z"
OLD_SOURCE_FILE_ID = "sf-old-00000001"
NEW_SOURCE_FILE_ID = "sf-new-00000002"


def _write(lib: Path, rel: str, data: bytes) -> Path:
    path = lib / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_chroma_sqlite(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE segments (
                id TEXT PRIMARY KEY, type TEXT, scope TEXT,
                collection TEXT REFERENCES collections(id)
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL, seq_id BLOB NOT NULL,
                UNIQUE (segment_id, embedding_id)
            );
            CREATE TABLE embedding_metadata (
                id INTEGER REFERENCES embeddings(id), key TEXT NOT NULL,
                string_value TEXT, int_value INTEGER, float_value REAL,
                bool_value INTEGER, PRIMARY KEY (id, key)
            );
            """
        )
        coll_id = str(uuid.uuid4())
        seg_id = str(uuid.uuid4())
        conn.execute("INSERT INTO collections (id, name) VALUES (?, ?)", (coll_id, "langchain"))
        conn.execute(
            "INSERT INTO segments (id, type, scope, collection) VALUES (?, ?, ?, ?)",
            (seg_id, "vector", "VECTOR", coll_id),
        )
        for i, rec in enumerate(records):
            conn.execute(
                "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) VALUES (?, ?, ?, ?)",
                (i + 1, seg_id, rec["id"], b"\x00"),
            )
            if rec.get("source") is not None:
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                    (i + 1, "source", rec["source"]),
                )
        conn.commit()
    finally:
        conn.close()


def _seed_registry(db: Path, *, aliases: list[str]) -> None:
    initialize_registry(db)
    conn = open_registry(db)
    try:
        with registry_transaction(conn):
            register_subject(conn, subject_id=SUBJECT)
            register_document_version(
                conn, document_id=DOC_A, subject_id=SUBJECT, source_hash=HASH_A
            )
            for alias in aliases:
                register_source_file(
                    conn,
                    document_id=DOC_A,
                    relative_path=alias,
                    source_hash=HASH_A,
                    collection="maker-manuals",
                )
            for ordinal, cid in enumerate(CHUNK_IDS):
                register_chunk(
                    conn,
                    chunk_id=cid,
                    document_id=DOC_A,
                    chunking_fingerprint=FP,
                    ordinal=ordinal,
                )
            for cid in CHUNK_IDS:
                register_vector_mapping(
                    conn,
                    chunk_id=cid,
                    chroma_embedding_id=cid,
                    mapping_status="native_chunk_id",
                )
    finally:
        conn.close()


def _write_tracker(path: Path, *, paths: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        HASH_A: {
            "paths": paths,
            "chunk_ids": list(CHUNK_IDS),
            "collection": "maker-manuals",
            "document_id": DOC_A,
        }
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(*, library: Path, persist: Path, registry: Path) -> dict:
    files: dict[str, str] = {}
    for rel in (OLD, NEW):
        p = library / rel
        if p.is_file():
            files[str(p)] = _file_sha(p)
    for path in (registry, persist / "embedded.json", persist / "chroma.sqlite3"):
        if path.is_file():
            files[str(path)] = _file_sha(path)
    return files


def _seed_indexed_source(env: dict) -> None:
    _write(env["library"], OLD, BYTES_A)
    _seed_registry(env["registry"], aliases=[OLD])
    _write_tracker(env["tracker"], paths=[OLD])
    _make_chroma_sqlite(
        env["chroma"],
        [{"id": cid, "source": OLD} for cid in CHUNK_IDS],
    )


@pytest.fixture()
def env(tmp_path: Path):
    library = tmp_path / "lib"
    persist = tmp_path / "persist"
    registry = tmp_path / "reg" / "metadata_registry_v1.sqlite3"
    library.mkdir()
    persist.mkdir()
    registry.parent.mkdir()
    return {
        "library": library,
        "persist": persist,
        "registry": registry,
        "tracker": persist / "embedded.json",
        "chroma": persist / "chroma.sqlite3",
    }


def _collect(env: dict, **overrides):
    params = {
        "library_root": env["library"],
        "persist_dir": env["persist"],
        "registry_db": env["registry"],
        "tracker_path": env["tracker"],
        "source_path": OLD,
        "destination_path": NEW,
    }
    params.update(overrides)
    return collect_pre_move_evidence(**params)


def test_valid_indexed_source_and_absent_destination(env) -> None:
    _seed_indexed_source(env)
    before = _snapshot(library=env["library"], persist=env["persist"], registry=env["registry"])
    evidence = _collect(env)
    after = _snapshot(library=env["library"], persist=env["persist"], registry=env["registry"])
    assert after == before
    assert evidence.source_path == OLD
    assert evidence.destination_path == NEW
    assert evidence.document_id == DOC_A
    assert evidence.source_hash == HASH_A
    assert evidence.source_file_sha256 == HASH_A
    assert evidence.resolver_classification == CLASS_INDEXED_OK
    assert evidence.proposed_operation == OP_NO_OP
    assert tuple(sorted(evidence.approved_vector_ids)) == tuple(sorted(CHUNK_IDS))
    assert evidence.plan.intent == INTENT_PLAN_MOVE
    assert evidence.plan_digest == plan_digest(evidence.plan)
    assert not (env["library"] / NEW).exists()


def test_destination_already_exists_blocks(env) -> None:
    _seed_indexed_source(env)
    _write(env["library"], NEW, BYTES_A)
    with pytest.raises(PreMoveEvidenceError, match="destination path must not exist"):
        _collect(env)


def test_missing_source_blocks(env) -> None:
    _seed_registry(env["registry"], aliases=[OLD])
    with pytest.raises(PreMoveEvidenceError, match="regular file"):
        _collect(env)


def test_destination_traversal_blocks(env) -> None:
    _seed_indexed_source(env)
    with pytest.raises(PreMoveEvidenceError, match="traversal"):
        _collect(env, destination_path="../escape.pdf")


def test_destination_absolute_path_blocks(env) -> None:
    _seed_indexed_source(env)
    with pytest.raises(PreMoveEvidenceError, match="must not be absolute"):
        _collect(env, destination_path="/tmp/abs.pdf")


def test_destination_nul_blocks(env) -> None:
    _seed_indexed_source(env)
    with pytest.raises(PreMoveEvidenceError, match="NUL"):
        _collect(env, destination_path="bad\0name.pdf")


def test_destination_symlink_escape_blocks(env, tmp_path: Path) -> None:
    _seed_indexed_source(env)
    outside = tmp_path / "outside"
    outside.mkdir()
    link_parent = env["library"] / "00_Career/03_Engine_Knowledge/MAN_Academy/linkdir"
    link_parent.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link_parent)
    escape_dest = "00_Career/03_Engine_Knowledge/MAN_Academy/linkdir/escape.pdf"
    with pytest.raises(PreMoveEvidenceError, match="outside library_root"):
        _collect(env, destination_path=escape_dest)


def test_source_not_indexed_ok_blocks(env) -> None:
    _write(env["library"], OLD, BYTES_A)
    initialize_registry(env["registry"])
    with pytest.raises(PreMoveEvidenceError, match="INDEXED_OK"):
        _collect(env)


def test_compatibility_conflict_blocks(env, monkeypatch) -> None:
    _seed_indexed_source(env)
    try:
        from rag_engine.index_compatibility.builders import stored_envelope_from_specs
        from rag_engine.index_compatibility.state import write_sidecar_v1

        emb, corp, idx = _make_compat_specs("preflight-model-a", env["library"])
        write_sidecar_v1(env["persist"], stored_envelope_from_specs(emb, corp, idx))
    except Exception:
        pytest.skip("fingerprint spec builders unavailable in this environment")
    monkeypatch.setenv("RAG_EMBED_MODEL", "preflight-model-b")
    with pytest.raises(PreMoveEvidenceError, match="compatibility conflict"):
        _collect(env)


def _make_compat_specs(model: str, library: Path):
    from rag_engine.index_compatibility.builders import (
        build_corpus_spec,
        build_embedding_spec,
        build_index_spec,
    )

    emb = build_embedding_spec(embedding_model=model, chunk_size=512, chunk_overlap=50)
    corp = build_corpus_spec(library_root=str(library))
    idx = build_index_spec(embedding_spec=emb, corpus_spec=corp)
    return emb, corp, idx


def test_evidence_collection_makes_no_writes(env) -> None:
    _seed_indexed_source(env)
    before = _snapshot(library=env["library"], persist=env["persist"], registry=env["registry"])
    _collect(env)
    after = _snapshot(library=env["library"], persist=env["persist"], registry=env["registry"])
    assert after == before
    assert not (env["library"] / NEW).exists()


def test_valid_pre_move_approval_validates_against_evidence(env) -> None:
    _seed_indexed_source(env)
    evidence = _collect(env)
    context = MoveApprovalContext(
        affected_source_file_ids=(OLD_SOURCE_FILE_ID, NEW_SOURCE_FILE_ID),
        registry_db_path=str(env["registry"].resolve()),
        library_root=str(env["library"].resolve()),
        persist_dir=str(env["persist"].resolve()),
    )
    item = evidence.plan.classifications[0]
    artifact = {
        "schema_version": 1,
        "approval_id": "move-approval-preflight-001",
        "operation": "MOVE",
        "intent": INTENT_PLAN_MOVE,
        "request_id": evidence.request_id,
        "plan_digest": evidence.plan_digest,
        "source_path": OLD,
        "destination_path": NEW,
        "document_id": item.document_id,
        "source_hash": item.source_hash,
        "resolver_classification": evidence.resolver_classification,
        "proposed_operation": evidence.proposed_operation,
        "expected_embedding_action": "NONE",
        "expected_new_vectors": 0,
        "affected_source_file_ids": list(context.affected_source_file_ids),
        "registry_db_path": context.registry_db_path,
        "library_root": context.library_root,
        "persist_dir": context.persist_dir,
        "issued_at": ISSUED_AT,
        "expires_at": EXPIRES_AT,
    }
    artifact["approval_digest"] = compute_approval_digest(artifact)
    result = validate_move_approval(
        artifact,
        evidence.plan,
        source_path=OLD,
        destination_path=NEW,
        context=context,
        pre_move_evidence=evidence,
        now_utc=NOW_VALID,
    )
    assert result.resolver_classification == CLASS_INDEXED_OK
    assert result.proposed_operation == OP_NO_OP

"""Isolated tests for read-only quarantine-delete preflight (DELETE Phase A)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from rag_engine.governed_delete.quarantine_preflight import (
    QuarantineDeletePreflightError,
    collect_quarantine_delete_evidence,
)
from rag_engine.library_state.contract import (
    CLASS_EXACT_DUPLICATE,
    INTENT_PLAN_DELETE,
    OP_RETIREMENT_PROPOSAL,
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
TARGET = "00_Career/03_Engine_Knowledge/MAN_Academy/copy/MAN_ME-C_LGIP_new_name.pdf"
UNPROVEN_RETAINED = "00_Career/03_Engine_Knowledge/MAN_Academy/unregistered_copy.pdf"
NEW = "00_Career/03_Engine_Knowledge/MAN_Academy/MAN_ME-C_LGIP_new_name.pdf"
QUARANTINE = "_Quarantine/pending/MAN_ME-C_LGIP_duplicate.pdf"
QUARANTINE_PARENT = "_Quarantine/pending"

BYTES_A = b"%PDF-1.4\nMAN Academy LGIP A\n"
BYTES_B = b"%PDF-1.4\nMAN Academy LGIP B variant\n"
HASH_A = source_hash_from_bytes(BYTES_A)
DOC_A = document_id_from_bytes(BYTES_A)
FP = "ab" * 32
ID1 = chunk_id(DOC_A, FP, 0)
ID2 = chunk_id(DOC_A, FP, 1)
CHUNK_IDS = (ID1, ID2)
SUBJECT = subject_id_from_key("maker_doc", "man-academy-lgip")


def _write(lib: Path, rel: str, data: bytes) -> Path:
    path = lib / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_chroma_sqlite(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
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
    dirs: dict[str, list[str] | None] = {}

    def add_file(path: Path) -> None:
        if path.is_file():
            files[str(path)] = _file_sha(path)

    def add_dir(path: Path) -> None:
        if path.is_dir():
            dirs[str(path)] = sorted(p.name for p in path.iterdir())
        else:
            dirs[str(path)] = None

    for rel in (OLD, TARGET, NEW, QUARANTINE, UNPROVEN_RETAINED):
        add_file(library / rel)
    add_dir(library / QUARANTINE_PARENT)
    add_dir(library)
    add_file(registry)
    add_dir(registry.parent)
    for name in ("embedded.json", "chroma.sqlite3", "ingest.lock", "certified_append.lock"):
        add_file(persist / name)
    add_dir(persist)
    move_lock = persist / "governed_single_file_move.lock"
    add_file(move_lock)
    journal_dir = persist / "governed_single_file_move_journal"
    add_dir(journal_dir)
    if persist.is_dir():
        for p in sorted(persist.rglob("*")):
            if p.is_file():
                add_file(p)
            elif p.is_dir():
                add_dir(p)
    return {"files": files, "dirs": dirs}


def _seed_exact_duplicate_env(env: dict) -> None:
    _write(env["library"], OLD, BYTES_A)
    _write(env["library"], TARGET, BYTES_A)
    (env["library"] / QUARANTINE_PARENT).mkdir(parents=True, exist_ok=True)
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


def _collect(
    env: dict,
    *,
    target_path: str = TARGET,
    retained_path: str = OLD,
    quarantine_path: str = QUARANTINE,
):
    return collect_quarantine_delete_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        target_path=target_path,
        retained_path=retained_path,
        quarantine_path=quarantine_path,
    )


def test_valid_exact_duplicate_with_explicit_retained_path_succeeds(env) -> None:
    _seed_exact_duplicate_env(env)
    evidence = _collect(env)
    assert evidence.target_path == TARGET
    assert evidence.retained_path == OLD
    assert evidence.quarantine_path == QUARANTINE
    assert evidence.resolver_classification == CLASS_EXACT_DUPLICATE
    assert evidence.proposed_operation == OP_RETIREMENT_PROPOSAL
    assert OLD in evidence.retained_aliases
    assert set(evidence.approved_vector_ids) == set(CHUNK_IDS)
    assert evidence.target_file_sha256 == HASH_A


def test_target_not_exact_duplicate_blocks(env) -> None:
    library = env["library"]
    persist = env["persist"]
    registry = env["registry"].parent / "other_registry.sqlite3"
    _write(library, NEW, BYTES_A)
    _write(library, OLD, BYTES_A)
    (library / QUARANTINE_PARENT).mkdir(parents=True, exist_ok=True)
    initialize_registry(registry)
    with pytest.raises(QuarantineDeletePreflightError, match="EXACT_DUPLICATE"):
        collect_quarantine_delete_evidence(
            library_root=library,
            persist_dir=persist,
            registry_db=registry,
            tracker_path=persist / "embedded.json",
            target_path=NEW,
            retained_path=OLD,
            quarantine_path=QUARANTINE,
        )


def test_last_live_retainer_not_proven_blocks(env) -> None:
    _write(env["library"], OLD, BYTES_A)
    _write(env["library"], UNPROVEN_RETAINED, BYTES_A)
    (env["library"] / QUARANTINE_PARENT).mkdir(parents=True, exist_ok=True)
    _seed_registry(env["registry"], aliases=[OLD])
    _write_tracker(env["tracker"], paths=[OLD])
    _make_chroma_sqlite(env["chroma"], [{"id": cid, "source": OLD} for cid in CHUNK_IDS])
    with pytest.raises(
        QuarantineDeletePreflightError,
        match="EXACT_DUPLICATE|retained_aliases is empty",
    ):
        _collect(env, target_path=OLD, retained_path=UNPROVEN_RETAINED)


def test_retained_path_not_in_resolver_proven_aliases_blocks(env) -> None:
    _seed_exact_duplicate_env(env)
    _write(env["library"], UNPROVEN_RETAINED, BYTES_A)
    with pytest.raises(QuarantineDeletePreflightError, match="retained aliases"):
        _collect(env, retained_path=UNPROVEN_RETAINED)


def test_retained_bytes_differ_blocks(env) -> None:
    _seed_exact_duplicate_env(env)
    _write(env["library"], OLD, BYTES_B)
    with pytest.raises(QuarantineDeletePreflightError, match="SHA-256"):
        _collect(env)


def test_target_retained_quarantine_path_collision_blocks(env) -> None:
    _seed_exact_duplicate_env(env)
    with pytest.raises(QuarantineDeletePreflightError, match="distinct"):
        _collect(env, retained_path=TARGET)
    with pytest.raises(QuarantineDeletePreflightError, match="distinct"):
        _collect(env, quarantine_path=OLD)


def test_quarantine_path_exists_blocks(env) -> None:
    _seed_exact_duplicate_env(env)
    _write(env["library"], QUARANTINE, BYTES_A)
    with pytest.raises(QuarantineDeletePreflightError, match="must not exist"):
        _collect(env)


@pytest.mark.parametrize(
    "bad_path,field",
    [
        ("/abs/target.pdf", "target_path"),
        ("../escape.pdf", "target_path"),
        ("bad\0name.pdf", "target_path"),
        ("  spaced.pdf", "target_path"),
    ],
)
def test_traversal_absolute_nul_and_whitespace_block(
    env, bad_path: str, field: str
) -> None:
    _seed_exact_duplicate_env(env)
    kwargs = {
        "target_path": TARGET,
        "retained_path": OLD,
        "quarantine_path": QUARANTINE,
        field: bad_path,
    }
    with pytest.raises(QuarantineDeletePreflightError):
        _collect(env, **kwargs)


def test_symlink_target_blocks(env) -> None:
    _seed_exact_duplicate_env(env)
    link = env["library"] / TARGET
    if link.exists():
        link.unlink()
    real = _write(env["library"], "manuals/hidden_real.pdf", BYTES_A)
    os.symlink(real, link)
    with pytest.raises(QuarantineDeletePreflightError, match="symlink"):
        _collect(env)


def test_symlink_escape_via_quarantine_parent_blocks(env) -> None:
    _seed_exact_duplicate_env(env)
    outside = env["library"].parent / "outside_quarantine"
    outside.mkdir(exist_ok=True)
    link_parent = env["library"] / QUARANTINE_PARENT
    if link_parent.exists():
        for child in link_parent.iterdir():
            child.unlink()
        link_parent.rmdir()
    os.symlink(outside, link_parent)
    with pytest.raises(QuarantineDeletePreflightError, match="outside library_root"):
        _collect(env)


def test_id_mismatch_blocks(env) -> None:
    _seed_exact_duplicate_env(env)
    conn = sqlite3.connect(str(env["registry"]))
    try:
        conn.execute("DELETE FROM chunk_vector_map WHERE chroma_embedding_id = ?", (ID1,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(
        QuarantineDeletePreflightError,
        match="vector evidence|EXACT_DUPLICATE|unresolved store evidence",
    ):
        _collect(env)


def test_compatibility_conflict_blocks(env, monkeypatch) -> None:
    _seed_exact_duplicate_env(env)
    try:
        from rag_engine.index_compatibility.builders import stored_envelope_from_specs
        from rag_engine.index_compatibility.state import write_sidecar_v1

        emb, corp, idx = _make_compat_specs("preflight-model-a", env["library"])
        write_sidecar_v1(env["persist"], stored_envelope_from_specs(emb, corp, idx))
    except Exception:
        pytest.skip("fingerprint spec builders unavailable in this environment")
    monkeypatch.setenv("RAG_EMBED_MODEL", "preflight-model-b")
    with pytest.raises(QuarantineDeletePreflightError, match="compatibility conflict"):
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


def test_preflight_makes_no_writes(env) -> None:
    _seed_exact_duplicate_env(env)
    before = _snapshot(library=env["library"], persist=env["persist"], registry=env["registry"])
    _collect(env)
    after = _snapshot(library=env["library"], persist=env["persist"], registry=env["registry"])
    assert after == before
    assert not (env["library"] / QUARANTINE).exists()

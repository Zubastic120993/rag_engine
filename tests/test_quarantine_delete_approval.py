"""Isolated unit tests for quarantine-delete approval-artifact validator."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from copy import deepcopy
from pathlib import Path

import pytest

from rag_engine.governed_delete.quarantine_approval import (
    QuarantineDeleteApprovalContext,
    QuarantineDeleteApprovalValidationError,
    compute_approval_digest,
    plan_digest,
    validate_quarantine_delete_approval,
)
from rag_engine.governed_delete.quarantine_preflight import collect_quarantine_delete_evidence
from rag_engine.library_state.contract import (
    CLASS_EXACT_DUPLICATE,
    CLASS_INDEXED_OK,
    EMBEDDING_NONE,
    INTENT_PLAN_DELETE,
    INTENT_PLAN_MOVE,
    OP_NO_OP,
    OP_RETIREMENT_PROPOSAL,
)
from rag_engine.library_state.move_approval import compute_approval_digest as compute_move_approval_digest
from rag_engine.library_state.resolver import resolve_library_state
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
NEW = "00_Career/03_Engine_Knowledge/MAN_Academy/MAN_ME-C_LGIP_new_name.pdf"
QUARANTINE = "_Quarantine/pending/MAN_ME-C_LGIP_duplicate.pdf"
QUARANTINE_PARENT = "_Quarantine/pending"

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

    for rel in (OLD, TARGET, NEW, QUARANTINE):
        add_file(library / rel)
    add_dir(library)
    add_file(registry)
    add_dir(registry.parent)
    for name in ("embedded.json", "chroma.sqlite3", "ingest.lock", "certified_append.lock"):
        add_file(persist / name)
    add_dir(persist)
    if persist.is_dir():
        for p in sorted(persist.rglob("*")):
            if p.is_file():
                add_file(p)
            elif p.is_dir():
                add_dir(p)
    return {"files": files, "dirs": dirs}


def _seed_delete_env(env: dict) -> None:
    _write(env["library"], OLD, BYTES_A)
    _write(env["library"], TARGET, BYTES_A)
    (env["library"] / QUARANTINE_PARENT).mkdir(parents=True, exist_ok=True)
    _seed_registry(env["registry"], aliases=[OLD])
    _write_tracker(env["tracker"], paths=[OLD])
    _make_chroma_sqlite(env["chroma"], [{"id": cid, "source": OLD} for cid in CHUNK_IDS])


@pytest.fixture()
def env(tmp_path: Path):
    library = tmp_path / "lib"
    persist = tmp_path / "persist"
    registry = tmp_path / "reg" / "metadata_registry_v1.sqlite3"
    library.mkdir()
    persist.mkdir()
    registry.parent.mkdir()
    base = {
        "library": library,
        "persist": persist,
        "registry": registry,
        "tracker": persist / "embedded.json",
        "chroma": persist / "chroma.sqlite3",
    }
    _seed_delete_env(base)
    return base


@pytest.fixture()
def delete_plan(env: dict):
    return resolve_library_state(
        INTENT_PLAN_DELETE,
        [TARGET],
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
    )


@pytest.fixture()
def preflight_evidence(env: dict):
    return collect_quarantine_delete_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        target_path=TARGET,
        retained_path=OLD,
        quarantine_path=QUARANTINE,
    )


@pytest.fixture()
def delete_context(env: dict) -> QuarantineDeleteApprovalContext:
    return QuarantineDeleteApprovalContext(
        registry_db_path=str(env["registry"].resolve()),
        library_root=str(env["library"].resolve()),
        persist_dir=str(env["persist"].resolve()),
        tracker_path=str(env["tracker"].resolve()),
    )


def _build_artifact(
    plan,
    *,
    preflight_evidence,
    context: QuarantineDeleteApprovalContext,
    approval_id: str = "quarantine-delete-approval-0001",
    issued_at: str = ISSUED_AT,
    expires_at: str = EXPIRES_AT,
) -> dict:
    artifact = {
        "schema_version": 1,
        "approval_id": approval_id,
        "operation": "QUARANTINE_DELETE",
        "intent": INTENT_PLAN_DELETE,
        "request_id": plan.request_id,
        "plan_digest": plan_digest(plan),
        "target_path": preflight_evidence.target_path,
        "retained_path": preflight_evidence.retained_path,
        "quarantine_path": preflight_evidence.quarantine_path,
        "document_id": preflight_evidence.document_id,
        "source_hash": preflight_evidence.source_hash,
        "resolver_classification": preflight_evidence.resolver_classification,
        "proposed_operation": preflight_evidence.proposed_operation,
        "expected_embedding_action": EMBEDDING_NONE,
        "expected_new_vectors": 0,
        "approved_vector_ids": list(preflight_evidence.approved_vector_ids),
        "retained_aliases": list(preflight_evidence.retained_aliases),
        "registry_db_path": context.registry_db_path,
        "library_root": context.library_root,
        "persist_dir": context.persist_dir,
        "tracker_path": context.tracker_path,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    artifact["approval_digest"] = compute_approval_digest(artifact)
    return artifact


def _validate_with_snapshot(
    artifact: dict,
    plan,
    *,
    env: dict,
    context: QuarantineDeleteApprovalContext,
    preflight_evidence,
    now_utc: str = NOW_VALID,
):
    before = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    result = validate_quarantine_delete_approval(
        artifact,
        plan,
        target_path=preflight_evidence.target_path,
        retained_path=preflight_evidence.retained_path,
        quarantine_path=preflight_evidence.quarantine_path,
        context=context,
        preflight_evidence=preflight_evidence,
        now_utc=now_utc,
    )
    after = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    assert after == before, "validator mutated isolated fixture stores"
    return result


def test_valid_artifact_accepted(env, delete_plan, delete_context, preflight_evidence) -> None:
    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    result = _validate_with_snapshot(
        artifact,
        delete_plan,
        env=env,
        context=delete_context,
        preflight_evidence=preflight_evidence,
    )
    assert result.approval_id == "quarantine-delete-approval-0001"
    assert result.request_id == delete_plan.request_id
    assert result.plan_digest == plan_digest(delete_plan)
    assert result.target_path == TARGET
    assert result.retained_path == OLD
    assert result.quarantine_path == QUARANTINE
    assert result.resolver_classification == CLASS_EXACT_DUPLICATE
    assert result.proposed_operation == OP_RETIREMENT_PROPOSAL


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "deadbeef" * 8),
        ("plan_digest", "0" * 64),
        ("document_id", "docrev:" + "0" * 64),
        ("source_hash", "1" * 64),
        ("retained_path", NEW),
        ("quarantine_path", OLD),
    ],
)
def test_binding_mismatch_rejected(
    env,
    delete_plan,
    delete_context,
    preflight_evidence,
    field: str,
    value: str,
) -> None:
    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    artifact[field] = value
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="mismatch"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
        )


def test_wrong_classification_rejected(env, delete_plan, delete_context, preflight_evidence) -> None:
    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    artifact["resolver_classification"] = CLASS_INDEXED_OK
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="mismatch"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
        )


def test_wrong_operation_and_embedding_rejected(env, delete_plan, delete_context, preflight_evidence) -> None:
    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    artifact["proposed_operation"] = OP_NO_OP
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="mismatch"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
        )

    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    artifact["expected_new_vectors"] = 1
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="expected_new_vectors"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
        )


def test_retained_alias_and_vector_id_mismatch_rejected(
    env, delete_plan, delete_context, preflight_evidence
) -> None:
    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    artifact["retained_aliases"] = [NEW]
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="retained_aliases mismatch"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
        )

    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    artifact["approved_vector_ids"] = [ID1]
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="approved_vector_ids mismatch"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
        )


def test_invalid_timestamps_and_lifetime_rejected(
    env, delete_plan, delete_context, preflight_evidence
) -> None:
    artifact = _build_artifact(
        delete_plan,
        preflight_evidence=preflight_evidence,
        context=delete_context,
        issued_at="2026-08-19T12:00:00Z",
        expires_at="2026-08-19T11:59:59Z",
    )
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="strictly after issued_at"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
            now_utc="2026-08-19T11:00:00Z",
        )

    artifact = _build_artifact(
        delete_plan,
        preflight_evidence=preflight_evidence,
        context=delete_context,
        issued_at="2026-08-19T12:00:00Z",
        expires_at="2026-08-20T12:01:00Z",
    )
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="24 hours"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
        )

    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="expired"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
            now_utc="2026-08-19T12:16:00Z",
        )


@pytest.mark.parametrize(
    "bad_digest",
    ["F" * 64, "abc123", " " + ("a" * 64), ("a" * 64) + " ", "g" * 64],
)
def test_malformed_approval_digest_rejected(
    env,
    delete_plan,
    delete_context,
    preflight_evidence,
    bad_digest: str,
) -> None:
    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    artifact["approval_digest"] = bad_digest
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="approval_digest"):
        _validate_with_snapshot(
            artifact,
            delete_plan,
            env=env,
            context=delete_context,
            preflight_evidence=preflight_evidence,
        )


def test_unknown_and_missing_fields_rejected(env, delete_plan, delete_context, preflight_evidence) -> None:
    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    artifact["unexpected_field"] = "x"
    artifact["approval_digest"] = compute_approval_digest(
        {k: v for k, v in artifact.items() if k != "unexpected_field"}
    )
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="unknown fields"):
        validate_quarantine_delete_approval(
            artifact,
            delete_plan,
            target_path=TARGET,
            retained_path=OLD,
            quarantine_path=QUARANTINE,
            context=delete_context,
            preflight_evidence=preflight_evidence,
            now_utc=NOW_VALID,
        )

    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    del artifact["document_id"]
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="missing mandatory fields"):
        validate_quarantine_delete_approval(
            artifact,
            delete_plan,
            target_path=TARGET,
            retained_path=OLD,
            quarantine_path=QUARANTINE,
            context=delete_context,
            preflight_evidence=preflight_evidence,
            now_utc=NOW_VALID,
        )


def test_intake_and_move_shapes_rejected(env, delete_plan, delete_context, preflight_evidence) -> None:
    intake_payload = {
        "schema_version": 1,
        "approved": True,
        "approved_by": "operator",
        "approved_at_utc": ISSUED_AT,
        "records_path": "/tmp/records.json",
        "records_sha256": "0" * 64,
        "plan_path": "/tmp/plan.json",
        "plan_sha256": plan_digest(delete_plan),
        "validator": {"ok": True, "warnings": []},
    }
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="Intake approval.json"):
        validate_quarantine_delete_approval(
            intake_payload,
            delete_plan,
            target_path=TARGET,
            retained_path=OLD,
            quarantine_path=QUARANTINE,
            context=delete_context,
            preflight_evidence=preflight_evidence,
            now_utc=NOW_VALID,
        )

    move_artifact = {
        "schema_version": 1,
        "approval_id": "move-approval-0001",
        "operation": "MOVE",
        "intent": INTENT_PLAN_MOVE,
        "request_id": delete_plan.request_id,
        "plan_digest": plan_digest(delete_plan),
        "source_path": OLD,
        "destination_path": NEW,
        "document_id": preflight_evidence.document_id,
        "source_hash": preflight_evidence.source_hash,
        "resolver_classification": CLASS_INDEXED_OK,
        "proposed_operation": OP_NO_OP,
        "expected_embedding_action": EMBEDDING_NONE,
        "expected_new_vectors": 0,
        "affected_source_file_ids": ["sf-old", "sf-new"],
        "registry_db_path": delete_context.registry_db_path,
        "library_root": delete_context.library_root,
        "persist_dir": delete_context.persist_dir,
        "issued_at": ISSUED_AT,
        "expires_at": EXPIRES_AT,
    }
    move_artifact["approval_digest"] = compute_move_approval_digest(move_artifact)
    with pytest.raises(QuarantineDeleteApprovalValidationError, match="MOVE approval"):
        validate_quarantine_delete_approval(
            move_artifact,
            delete_plan,
            target_path=TARGET,
            retained_path=OLD,
            quarantine_path=QUARANTINE,
            context=delete_context,
            preflight_evidence=preflight_evidence,
            now_utc=NOW_VALID,
        )


def test_validation_makes_no_writes(env, delete_plan, delete_context, preflight_evidence) -> None:
    artifact = _build_artifact(delete_plan, preflight_evidence=preflight_evidence, context=delete_context)
    before = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    bad_cases = [
        {**deepcopy(artifact), "plan_digest": "0" * 64},
        {**deepcopy(artifact), "request_id": "deadbeef" * 8},
    ]
    for case in bad_cases:
        with pytest.raises(QuarantineDeleteApprovalValidationError):
            validate_quarantine_delete_approval(
                case,
                delete_plan,
                target_path=TARGET,
                retained_path=OLD,
                quarantine_path=QUARANTINE,
                context=delete_context,
                preflight_evidence=preflight_evidence,
                now_utc=NOW_VALID,
            )
    after = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    assert after == before

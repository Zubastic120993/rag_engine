"""Isolated unit tests for the read-only MOVE approval-artifact validator."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from copy import deepcopy
from pathlib import Path

import pytest

from rag_engine.library_state.contract import (
    CLASS_INDEXED_OK,
    CLASS_SAME_BYTES_MOVED,
    EMBEDDING_NONE,
    INTENT_PLAN_MOVE,
    OP_METADATA_ONLY_RECONCILE,
    OP_NO_OP,
)
from rag_engine.library_state.move_approval import (
    MoveApprovalContext,
    MoveApprovalValidationError,
    compute_approval_digest,
    plan_digest,
    validate_move_approval,
)
from rag_engine.library_state.move_preflight import collect_pre_move_evidence
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
    dirs: dict[str, list[str] | None] = {}

    def add_file(path: Path) -> None:
        if path.is_file():
            files[str(path)] = _file_sha(path)

    def add_dir(path: Path) -> None:
        if path.is_dir():
            dirs[str(path)] = sorted(p.name for p in path.iterdir())
        else:
            dirs[str(path)] = None

    for rel in (OLD, NEW):
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


@pytest.fixture()
def move_plan(env: dict):
    _write(env["library"], OLD, BYTES_A)
    _seed_registry(env["registry"], aliases=[OLD])
    _write_tracker(env["tracker"], paths=[OLD])
    _make_chroma_sqlite(
        env["chroma"],
        [{"id": cid, "source": OLD} for cid in CHUNK_IDS],
    )
    return resolve_library_state(
        INTENT_PLAN_MOVE,
        [OLD],
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
    )


@pytest.fixture()
def post_move_plan(env: dict, pre_move_evidence):
    old_path = env["library"] / OLD
    if old_path.is_file():
        old_path.unlink()
    _write(env["library"], NEW, BYTES_A)
    return resolve_library_state(
        INTENT_PLAN_MOVE,
        [NEW],
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
    )


@pytest.fixture()
def pre_move_evidence(env: dict, move_plan):
    return collect_pre_move_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        source_path=OLD,
        destination_path=NEW,
    )


@pytest.fixture()
def move_context(env: dict) -> MoveApprovalContext:
    return MoveApprovalContext(
        affected_source_file_ids=(OLD_SOURCE_FILE_ID, NEW_SOURCE_FILE_ID),
        registry_db_path=str(env["registry"].resolve()),
        library_root=str(env["library"].resolve()),
        persist_dir=str(env["persist"].resolve()),
    )


def _build_artifact(
    plan,
    *,
    source_path: str = OLD,
    destination_path: str = NEW,
    context: MoveApprovalContext,
    approval_id: str = "move-approval-0001",
    issued_at: str = ISSUED_AT,
    expires_at: str = EXPIRES_AT,
) -> dict:
    item = next(c for c in plan.classifications if c.target == source_path)
    artifact = {
        "schema_version": 1,
        "approval_id": approval_id,
        "operation": "MOVE",
        "intent": INTENT_PLAN_MOVE,
        "request_id": plan.request_id,
        "plan_digest": plan_digest(plan),
        "source_path": source_path,
        "destination_path": destination_path,
        "document_id": item.document_id,
        "source_hash": item.source_hash,
        "resolver_classification": plan.classification,
        "proposed_operation": plan.proposed_operation,
        "expected_embedding_action": EMBEDDING_NONE,
        "expected_new_vectors": 0,
        "affected_source_file_ids": list(context.affected_source_file_ids),
        "registry_db_path": context.registry_db_path,
        "library_root": context.library_root,
        "persist_dir": context.persist_dir,
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
    context: MoveApprovalContext,
    pre_move_evidence,
    source_path: str = OLD,
    destination_path: str = NEW,
    now_utc: str = NOW_VALID,
):
    before = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    result = validate_move_approval(
        artifact,
        plan,
        source_path=source_path,
        destination_path=destination_path,
        context=context,
        pre_move_evidence=pre_move_evidence,
        now_utc=now_utc,
    )
    after = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    assert after == before, "validator mutated isolated fixture stores"
    return result


def test_valid_artifact_accepted_with_fixed_now(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    result = _validate_with_snapshot(
        artifact,
        move_plan,
        env=env,
        context=move_context,
        pre_move_evidence=pre_move_evidence,
        source_path=OLD,
        destination_path=NEW,
    )
    assert result.approval_id == "move-approval-0001"
    assert result.request_id == move_plan.request_id
    assert result.plan_digest == plan_digest(move_plan)
    assert result.approval_digest == artifact["approval_digest"]
    assert result.source_path == OLD
    assert result.destination_path == NEW
    assert result.resolver_classification == CLASS_INDEXED_OK
    assert result.proposed_operation == OP_NO_OP


def test_post_move_same_bytes_moved_plan_rejected(env, move_plan, post_move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    with pytest.raises(
        MoveApprovalValidationError,
        match="post-move reconciliation evidence",
    ):
        validate_move_approval(
            artifact,
            post_move_plan,
            source_path=OLD,
            destination_path=NEW,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            now_utc=NOW_VALID,
        )


def test_plan_digest_mismatch_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact["plan_digest"] = "0" * 64
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(MoveApprovalValidationError, match="plan_digest mismatch"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
        )


def test_artifact_digest_mismatch_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact["approval_digest"] = "f" * 64
    with pytest.raises(MoveApprovalValidationError, match="approval_digest mismatch"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
        )


def test_wrong_request_id_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact["request_id"] = "deadbeef" * 8
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(MoveApprovalValidationError, match="request_id mismatch"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
        )


def test_wrong_source_path_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    with pytest.raises(MoveApprovalValidationError, match="pre_move_evidence source_path mismatch"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path="other/source.pdf",
            destination_path=NEW,
        )


def test_wrong_destination_path_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    with pytest.raises(MoveApprovalValidationError, match="pre_move_evidence destination_path mismatch"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path="other/destination.pdf",
        )


def test_destination_traversal_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    with pytest.raises(MoveApprovalValidationError, match="traversal"):
        validate_move_approval(
            artifact,
            move_plan,
            source_path=OLD,
            destination_path="../escape.pdf",
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            now_utc=NOW_VALID,
        )


def test_destination_absolute_path_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    with pytest.raises(MoveApprovalValidationError, match="must not be absolute"):
        validate_move_approval(
            artifact,
            move_plan,
            source_path=OLD,
            destination_path="/tmp/abs.pdf",
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            now_utc=NOW_VALID,
        )


def test_source_equals_destination_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    with pytest.raises(MoveApprovalValidationError, match="must differ"):
        validate_move_approval(
            artifact,
            move_plan,
            source_path=NEW,
            destination_path=NEW,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            now_utc=NOW_VALID,
        )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("document_id", "docrev:wrong"),
        ("source_hash", "0" * 64),
        ("resolver_classification", "ALIAS_ONLY"),
        ("proposed_operation", "ALIAS_REGISTER"),
    ],
)
def test_wrong_plan_bindings_rejected(
    env,
    move_plan,
    move_context,
    pre_move_evidence,
    field: str,
    bad_value: str,
) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact[field] = bad_value
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(MoveApprovalValidationError, match=f"{field} mismatch"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
        )


def test_non_none_embedding_action_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact["expected_embedding_action"] = "PENDING_SEPARATE_APPROVAL"
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(MoveApprovalValidationError, match="expected_embedding_action"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
        )


def test_nonzero_expected_vectors_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact["expected_new_vectors"] = 1
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(MoveApprovalValidationError, match="expected_new_vectors must be 0"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
        )


@pytest.mark.parametrize(
    "affected_ids",
    [
        ["only-one-id"],
        ["dup", "dup"],
        ["a", "b", "c"],
        ["x", "y"],
    ],
)
def test_affected_source_file_ids_rejected(
    env,
    move_plan,
    move_context,
    pre_move_evidence,
    affected_ids: list[str],
) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact["affected_source_file_ids"] = affected_ids
    artifact["approval_digest"] = compute_approval_digest(artifact)
    pattern = (
        "exactly two"
        if len(affected_ids) != 2
        else "distinct"
        if len(set(affected_ids)) == 1
        else "mismatch"
    )
    with pytest.raises(MoveApprovalValidationError, match=pattern):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
        )


def test_invalid_timestamp_format_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact["issued_at"] = "2026-08-19T12:00:00+00:00"
    artifact["approval_digest"] = compute_approval_digest(artifact)
    with pytest.raises(MoveApprovalValidationError, match="issued_at must be UTC"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
        )


def test_expiry_before_issue_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(
        move_plan,
        context=move_context,
        issued_at="2026-08-19T12:00:00Z",
        expires_at="2026-08-19T11:59:59Z",
    )
    with pytest.raises(MoveApprovalValidationError, match="strictly after issued_at"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
            now_utc="2026-08-19T11:00:00Z",
        )


def test_duration_over_24h_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(
        move_plan,
        context=move_context,
        issued_at="2026-08-19T12:00:00Z",
        expires_at="2026-08-20T12:01:00Z",
    )
    with pytest.raises(MoveApprovalValidationError, match="24 hours"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
            now_utc="2026-08-19T12:05:00Z",
        )


def test_expired_artifact_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    with pytest.raises(MoveApprovalValidationError, match="expired"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
            now_utc="2026-08-19T12:16:00Z",
        )


def test_not_yet_valid_artifact_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    with pytest.raises(MoveApprovalValidationError, match="not yet valid"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
            now_utc="2026-08-19T11:59:59Z",
        )


@pytest.mark.parametrize(
    "bad_digest",
    [
        "F" * 64,
        "abc123",
        " " + ("a" * 64),
        ("a" * 64) + " ",
        "g" * 64,
    ],
)
def test_malformed_approval_digest_rejected(
    env,
    move_plan,
    move_context,
    pre_move_evidence,
    bad_digest: str,
) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact["approval_digest"] = bad_digest
    with pytest.raises(MoveApprovalValidationError, match="approval_digest"):
        _validate_with_snapshot(
            artifact,
            move_plan,
            env=env,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            source_path=OLD,
            destination_path=NEW,
        )


def test_unknown_field_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    artifact["unexpected_field"] = "x"
    artifact["approval_digest"] = compute_approval_digest(
        {k: v for k, v in artifact.items() if k != "unexpected_field"}
    )
    with pytest.raises(MoveApprovalValidationError, match="unknown fields"):
        validate_move_approval(
            artifact,
            move_plan,
            source_path=OLD,
            destination_path=NEW,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            now_utc=NOW_VALID,
        )


def test_missing_mandatory_field_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
    del artifact["document_id"]
    with pytest.raises(MoveApprovalValidationError, match="missing mandatory fields"):
        validate_move_approval(
            artifact,
            move_plan,
            source_path=OLD,
            destination_path=NEW,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            now_utc=NOW_VALID,
        )


@pytest.mark.parametrize(
    "invalid_artifact",
    [None, [], "not-a-json-object"],
)
def test_non_mapping_artifact_rejected(
    env,
    move_plan,
    move_context,
    pre_move_evidence,
    invalid_artifact,
) -> None:
    with pytest.raises(MoveApprovalValidationError, match="artifact must be a mapping"):
        validate_move_approval(
            invalid_artifact,
            move_plan,
            source_path=OLD,
            destination_path=NEW,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            now_utc=NOW_VALID,
        )


def test_intake_style_payload_rejected(env, move_plan, move_context, pre_move_evidence) -> None:
    intake_payload = {
        "schema_version": 1,
        "approved": True,
        "approved_by": "operator",
        "approved_at_utc": ISSUED_AT,
        "records_path": "/tmp/records.json",
        "records_sha256": "0" * 64,
        "plan_path": "/tmp/plan.json",
        "plan_sha256": plan_digest(move_plan),
        "validator": {"ok": True, "warnings": []},
    }
    with pytest.raises(MoveApprovalValidationError, match="Intake approval.json"):
        validate_move_approval(
            intake_payload,
            move_plan,
            source_path=OLD,
            destination_path=NEW,
            context=move_context,
            pre_move_evidence=pre_move_evidence,
            now_utc=NOW_VALID,
        )


def test_validator_does_not_mutate_fixture_stores(env, move_plan, move_context, pre_move_evidence) -> None:
    artifact = _build_artifact(move_plan, context=move_context)
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
        with pytest.raises(MoveApprovalValidationError):
            validate_move_approval(
                case,
                move_plan,
                source_path=OLD,
                destination_path=NEW,
                context=move_context,
                pre_move_evidence=pre_move_evidence,
                now_utc=NOW_VALID,
            )
    after = _snapshot(
        library=env["library"],
        persist=env["persist"],
        registry=env["registry"],
    )
    assert after == before

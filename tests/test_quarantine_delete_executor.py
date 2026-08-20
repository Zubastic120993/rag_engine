"""Tests for bounded quarantine-delete executor (DELETE Phase B)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from unittest import mock

import pytest

from rag_engine.governed_delete import (
    OUTCOME_BLOCKED,
    OUTCOME_COMPENSATED_BEFORE_COMMIT,
    OUTCOME_DRY_RUN,
    OUTCOME_RECOVERY_REQUIRED,
    OUTCOME_SUCCESS,
    QuarantineDeleteApprovalContext,
    QuarantineDeleteRequest,
    collect_quarantine_delete_evidence,
    compute_approval_digest,
    execute_quarantine_delete,
    plan_digest,
    recover_quarantine_delete,
)
from rag_engine.governed_delete import quarantine_executor as qd_module
from rag_engine.governed_delete.quarantine_journal import (
    JOURNAL_DIR_NAME,
    PHASE_COMPENSATED,
    PHASE_FILESYSTEM_QUARANTINED,
    PHASE_RECOVERY_REQUIRED,
    PHASE_VERIFIED,
    read_journal_exact,
)
from rag_engine.governed_reconciliation.explicit_move import read_tracker_json
from rag_engine.index_compatibility.chroma_inspect import chroma_sqlite_path
from rag_engine.library_state.contract import EMBEDDING_NONE, INTENT_PLAN_DELETE
from rag_engine.library_state.evidence import lookup_chroma_by_embedding_ids
from rag_engine.library_state.resolver import resolve_library_state
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    LOCATOR_INACTIVE,
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    append_source_file_event,
    get_locator_lifecycle_state,
    initialize_locator_lifecycle_state,
    initialize_registry,
    make_source_file_id,
    migrate_connection,
    open_registry,
    record_compensation_request,
    record_terminal_compensation_outcome,
    register_chunk,
    register_document_version,
    register_source_file,
    register_subject,
    register_vector_mapping,
    registry_transaction,
    validate_quarantine_registry_preconditions,
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
QUARANTINE = "_Quarantine/pending/MAN_ME-C_LGIP_duplicate.pdf"
QUARANTINE_PARENT = "_Quarantine/pending"

BYTES = b"%PDF-1.4\nMAN Academy LGIP duplicate fixture\n"
HASH = source_hash_from_bytes(BYTES)
DOC = document_id_from_bytes(BYTES)
SUBJECT = subject_id_from_key("maker_doc", "quarantine-delete-exec")
FP = "cd" * 32
ID1 = chunk_id(DOC, FP, 0)
ID2 = chunk_id(DOC, FP, 1)
VECTOR_IDS = (ID1, ID2)
COLLECTION = "maker-manuals"
OPERATION_ID = "quarantine-delete-op-001"

ISSUED_AT = "2026-08-19T12:00:00Z"
EXPIRES_AT = "2026-08-19T12:15:00Z"
NOW_VALID = "2026-08-19T12:05:00Z"


def _valid_clock() -> str:
    return NOW_VALID


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _write(lib: Path, rel: str, data: bytes) -> Path:
    path = lib / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_chroma(path: Path, *, source: str) -> None:
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
        for i, eid in enumerate(VECTOR_IDS, start=1):
            conn.execute(
                "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) "
                "VALUES (?, ?, ?, ?)",
                (i, seg_id, eid, b"\x00"),
            )
            for key, val in (
                ("source", source),
                ("collection", COLLECTION),
                ("document_id", DOC),
                ("source_hash", HASH),
            ):
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                    (i, key, val),
                )
        conn.commit()
    finally:
        conn.close()


def _write_tracker(path: Path, *, paths: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        HASH: {
            "paths": paths,
            "chunk_ids": list(VECTOR_IDS),
            "collection": COLLECTION,
            "document_id": DOC,
        }
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_env(env: dict) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    lock = env["persist"] / qd_module.QUARANTINE_DELETE_LOCK_NAME
    journal_root = env["persist"] / JOURNAL_DIR_NAME
    for key, path in (
        ("library", env["library"]),
        ("persist", env["persist"]),
        ("registry", env["registry"]),
        ("tracker", env["tracker"]),
        ("chroma", env["chroma"]),
        ("lock", lock),
        ("journal_root", journal_root),
    ):
        if path.is_file():
            out[str(path)] = _file_sha(path)
        elif path.is_dir():
            out[str(path)] = json.dumps(sorted(p.name for p in path.iterdir()))
        else:
            out[str(path)] = None
    for rel in (OLD, TARGET, QUARANTINE):
        p = env["library"] / rel
        out[str(p)] = _file_sha(p) if p.is_file() else None
    return out


def _resolve_alias_event_id(conn, source_file_id: str) -> int:
    row = conn.execute(
        "SELECT event_id FROM source_file_events "
        "WHERE source_file_id = ? AND event_type = ?",
        (source_file_id, SOURCE_FILE_EVENT_ALIAS_REGISTERED),
    ).fetchone()
    assert row is not None
    return int(row["event_id"])


def _assert_blocked_no_artifacts(
    env: dict,
    result,
    *,
    before: dict[str, str | None] | None = None,
    digest: str | None = None,
) -> None:
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert not (env["persist"] / qd_module.QUARANTINE_DELETE_LOCK_NAME).exists()
    journal_root = env["persist"] / JOURNAL_DIR_NAME
    assert not journal_root.exists()
    if before is not None:
        assert _snapshot_env(env) == before
    if digest is not None:
        with open_registry(env["registry"]) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
                    (digest,),
                ).fetchone()["c"]
                == 0
            )


def _target_locator_registered(env: dict) -> bool:
    with open_registry(env["registry"]) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM source_files WHERE source_file_id = ?",
                (env["target_sf"],),
            ).fetchone()
            is not None
        )


def _register_governed_target_locator(env: dict) -> str:
    if _target_locator_registered(env):
        return env["target_sf"]
    registry = env["registry"].resolve()
    with open_registry(registry) as conn:
        with registry_transaction(conn):
            target_sf = register_source_file(
                conn,
                document_id=DOC,
                relative_path=TARGET,
                source_hash=HASH,
                collection=COLLECTION,
            )["source_file_id"]
            append_source_file_event(
                conn,
                source_file_id=target_sf,
                document_id=DOC,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=_digest("target-alias"),
            )
        initialize_locator_lifecycle_state(
            conn,
            source_file_id=target_sf,
            document_id=DOC,
            source="test_fixture",
        )
        conn.commit()
    env["target_sf"] = target_sf
    return target_sf


def _seed_executor_env(
    env: dict,
    *,
    register_target: bool = False,
    target_alias: bool = True,
) -> dict:
    _write(env["library"], OLD, BYTES)
    _write(env["library"], TARGET, BYTES)
    (env["library"] / QUARANTINE_PARENT).mkdir(parents=True, exist_ok=True)
    registry = env["registry"].resolve()
    initialize_registry(registry)
    with open_registry(registry) as conn:
        migrate_connection(conn, target_version=5)
        with registry_transaction(conn):
            register_subject(conn, subject_id=SUBJECT)
            register_document_version(
                conn, document_id=DOC, subject_id=SUBJECT, source_hash=HASH
            )
            retained_sf = register_source_file(
                conn,
                document_id=DOC,
                relative_path=OLD,
                source_hash=HASH,
                collection=COLLECTION,
            )["source_file_id"]
            append_source_file_event(
                conn,
                source_file_id=retained_sf,
                document_id=DOC,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=_digest("retained-alias"),
            )
            if register_target:
                target_sf = register_source_file(
                    conn,
                    document_id=DOC,
                    relative_path=TARGET,
                    source_hash=HASH,
                    collection=COLLECTION,
                )["source_file_id"]
                if target_alias:
                    append_source_file_event(
                        conn,
                        source_file_id=target_sf,
                        document_id=DOC,
                        event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                        approval_digest=_digest("target-alias"),
                    )
            else:
                target_sf = make_source_file_id(document_id=DOC, relative_path=TARGET)
            for ordinal, cid in enumerate(VECTOR_IDS):
                register_chunk(
                    conn,
                    chunk_id=cid,
                    document_id=DOC,
                    chunking_fingerprint=FP,
                    ordinal=ordinal,
                )
            for cid in VECTOR_IDS:
                register_vector_mapping(
                    conn,
                    chunk_id=cid,
                    chroma_embedding_id=cid,
                    mapping_status="native_chunk_id",
                )
        initialize_locator_lifecycle_state(
            conn,
            source_file_id=retained_sf,
            document_id=DOC,
            source="test_fixture",
        )
        if register_target:
            initialize_locator_lifecycle_state(
                conn,
                source_file_id=target_sf,
                document_id=DOC,
                source="test_fixture",
            )
        conn.commit()
    _write_tracker(env["tracker"], paths=[OLD])
    _make_chroma(env["chroma"], source=OLD)
    env["retained_sf"] = retained_sf
    env["target_sf"] = target_sf
    env.setdefault("skip_governed_target", False)
    return env


def _build_artifact(evidence, context: QuarantineDeleteApprovalContext) -> dict:
    artifact = {
        "schema_version": 1,
        "approval_id": "quarantine-delete-exec-approval-001",
        "operation": "QUARANTINE_DELETE",
        "intent": INTENT_PLAN_DELETE,
        "request_id": evidence.plan.request_id,
        "plan_digest": plan_digest(evidence.plan),
        "target_path": evidence.target_path,
        "retained_path": evidence.retained_path,
        "quarantine_path": evidence.quarantine_path,
        "document_id": evidence.document_id,
        "source_hash": evidence.source_hash,
        "resolver_classification": evidence.resolver_classification,
        "proposed_operation": evidence.proposed_operation,
        "expected_embedding_action": EMBEDDING_NONE,
        "expected_new_vectors": 0,
        "approved_vector_ids": list(evidence.approved_vector_ids),
        "retained_aliases": list(evidence.retained_aliases),
        "registry_db_path": context.registry_db_path,
        "library_root": context.library_root,
        "persist_dir": context.persist_dir,
        "tracker_path": context.tracker_path,
        "issued_at": ISSUED_AT,
        "expires_at": EXPIRES_AT,
    }
    artifact["approval_digest"] = compute_approval_digest(artifact)
    return artifact


@pytest.fixture()
def env(tmp_path: Path):
    library = tmp_path / "lib"
    persist = tmp_path / "persist"
    library.mkdir()
    persist.mkdir()
    registry = (tmp_path / "registry" / "metadata.sqlite3").resolve()
    registry.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "library": library,
        "persist": persist,
        "registry": registry,
        "tracker": (persist / "embedded.json").resolve(),
        "chroma": (persist / "chroma.sqlite3").resolve(),
    }
    return _seed_executor_env(base)


@pytest.fixture()
def delete_context(env: dict) -> QuarantineDeleteApprovalContext:
    return QuarantineDeleteApprovalContext(
        registry_db_path=str(env["registry"]),
        library_root=str(env["library"].resolve()),
        persist_dir=str(env["persist"].resolve()),
        tracker_path=str(env["tracker"]),
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


_SKIP_GOVERNED_TARGET_SETUP = frozenset(
    {
        "test_dry_run_performs_no_writes_lock_or_journal",
        "test_missing_target_locator_blocks_before_writes",
    }
)


@pytest.fixture(autouse=True)
def governed_preflight(request):
    if request.node.name in _SKIP_GOVERNED_TARGET_SETUP:
        return
    env = request.getfixturevalue("env")
    if not env.get("skip_governed_target"):
        _register_governed_target_locator(env)


@pytest.fixture()
def approval_artifact(preflight_evidence, delete_context):
    return _build_artifact(preflight_evidence, delete_context)


def _request(
    env: dict,
    *,
    preflight_evidence,
    approval_artifact: dict,
    delete_context: QuarantineDeleteApprovalContext,
    execute: bool = False,
    **overrides,
) -> QuarantineDeleteRequest:
    base = {
        "approval_artifact": approval_artifact,
        "preflight_evidence": preflight_evidence,
        "approval_context": delete_context,
        "target_relative_path": TARGET,
        "retained_relative_path": OLD,
        "quarantine_relative_path": QUARANTINE,
        "library_root": str(env["library"].resolve()),
        "persist_dir": str(env["persist"].resolve()),
        "registry_db": str(env["registry"]),
        "tracker_path": str(env["tracker"]),
        "target_source_file_id": env["target_sf"],
        "retained_source_file_id": env["retained_sf"],
        "approved_vector_ids": VECTOR_IDS,
        "registry_collection": COLLECTION,
        "operation_id": OPERATION_ID,
        "execute": execute,
    }
    base.update(overrides)
    return QuarantineDeleteRequest(**base)


def _journal_phase(env: dict, operation_id: str = OPERATION_ID) -> str | None:
    root = env["persist"] / JOURNAL_DIR_NAME
    path = root / f"{operation_id}.json"
    if not path.is_file():
        return None
    return read_journal_exact(env["persist"], operation_id)["phase"]


def _execute(env, request, *, preflight_evidence=None, **kwargs):
    clock = kwargs.pop("now_utc_provider", _valid_clock)
    if preflight_evidence is not None and request.execute:
        with mock.patch.object(
            qd_module,
            "collect_quarantine_delete_evidence",
            return_value=preflight_evidence,
        ):
            return execute_quarantine_delete(
                request,
                now_utc_provider=clock,
                **kwargs,
            )
    return execute_quarantine_delete(
        request,
        now_utc_provider=clock,
        **kwargs,
    )


def test_dry_run_performs_no_writes_lock_or_journal(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=False,
        ),
    )
    after = _snapshot_env(env)
    assert after == before
    assert result.success is True
    assert result.outcome == OUTCOME_DRY_RUN
    assert not (env["persist"] / qd_module.QUARANTINE_DELETE_LOCK_NAME).exists()
    assert not (env["persist"] / JOURNAL_DIR_NAME).exists()


def test_happy_path_quarantines_duplicate_preserves_retained_and_verifies(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    before_target = _file_sha(env["library"] / TARGET)
    before_retained = _file_sha(env["library"] / OLD)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is True
    assert result.outcome == OUTCOME_SUCCESS
    assert not (env["library"] / TARGET).exists()
    assert (env["library"] / QUARANTINE).is_file()
    assert _file_sha(env["library"] / QUARANTINE) == before_target
    assert (env["library"] / OLD).is_file()
    assert _file_sha(env["library"] / OLD) == before_retained
    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["target_sf"])["activity_state"]
            == LOCATOR_INACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["retained_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"] == 2
        target_status = conn.execute(
            "SELECT status FROM source_files WHERE source_file_id = ?",
            (env["target_sf"],),
        ).fetchone()["status"]
    tracker = read_tracker_json(env["tracker"])
    assert tracker[HASH]["paths"] == [OLD]
    assert tracker[HASH]["chunk_ids"] == list(VECTOR_IDS)
    lookup = lookup_chroma_by_embedding_ids(env["chroma"], VECTOR_IDS)
    for rec in lookup.records:
        assert rec.source_path == OLD
    assert _journal_phase(env) == PHASE_VERIFIED


def test_non_duplicate_blocks_before_writes(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    _write(env["library"], TARGET, b"different bytes entirely")
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert _snapshot_env(env) == before


def test_retained_path_not_proven_blocks(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    _write(env["library"], UNPROVEN_RETAINED, BYTES)
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
            retained_relative_path=UNPROVEN_RETAINED,
        ),
    )
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert _snapshot_env(env) == before


def test_fresh_evidence_drift_blocks_before_writes(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    tracker = json.loads(env["tracker"].read_text(encoding="utf-8"))
    tracker[HASH]["paths"] = ["manuals/drifted.pdf"]
    env["tracker"].write_text(json.dumps(tracker) + "\n", encoding="utf-8")
    before_registry = _file_sha(env["registry"])
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert (env["library"] / TARGET).is_file()
    assert _file_sha(env["registry"]) == before_registry


def test_symlink_target_blocks(env, preflight_evidence, approval_artifact, delete_context) -> None:
    target = env["library"] / TARGET
    target.unlink()
    os.symlink(env["library"] / OLD, target)
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert _snapshot_env(env) == before


def test_quarantine_collision_blocks(env, preflight_evidence, approval_artifact, delete_context) -> None:
    _write(env["library"], QUARANTINE, BYTES)
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert _snapshot_env(env) == before


def test_missing_quarantine_parent_blocks(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    missing_parent = "_Quarantine/missing_parent/file.pdf"
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
            quarantine_relative_path=missing_parent,
        ),
    )
    assert result.success is False
    assert _snapshot_env(env) == before


def test_traversal_quarantine_path_blocks_structurally(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
            quarantine_relative_path="../outside.pdf",
        ),
    )
    assert result.success is False
    assert _snapshot_env(env) == before


def test_exdev_blocks_without_copy_fallback(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    def _exdev(_src: Path, _dst: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
        filesystem_renamer=_exdev,
    )
    assert result.success is False
    assert result.outcome == OUTCOME_COMPENSATED_BEFORE_COMMIT
    assert (env["library"] / TARGET).is_file()
    assert not (env["library"] / QUARANTINE).exists()


def test_lock_contention_blocks_without_state_change(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    lock = env["persist"] / qd_module.QUARANTINE_DELETE_LOCK_NAME
    lock.write_text("held\n", encoding="utf-8")
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert "lock" in (result.error_message or "").lower()
    assert _snapshot_env(env) == before


def test_tracker_mismatch_blocks_before_writes(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    _write_tracker(env["tracker"], paths=[TARGET])
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert _snapshot_env(env) == before


def test_chroma_missing_id_blocks(env, preflight_evidence, approval_artifact, delete_context) -> None:
    conn = sqlite3.connect(str(env["chroma"]))
    try:
        conn.execute("DELETE FROM embeddings WHERE embedding_id = ?", (ID1,))
        conn.commit()
    finally:
        conn.close()
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert _snapshot_env(env) == before


def test_chroma_duplicate_physical_row_blocks(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    conn = sqlite3.connect(str(env["chroma"]))
    try:
        coll = conn.execute("SELECT collection FROM segments LIMIT 1").fetchone()[0]
        seg2 = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO segments (id, type, scope, collection) VALUES (?, ?, ?, ?)",
            (seg2, "vector", "VECTOR", coll),
        )
        conn.execute(
            "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) "
            "VALUES (?, ?, ?, ?)",
            (99, seg2, ID1, b"\x01"),
        )
        conn.commit()
    finally:
        conn.close()
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert _snapshot_env(env) == before


def test_chroma_source_path_mismatch_blocks(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    conn = sqlite3.connect(str(env["chroma"]))
    try:
        row = conn.execute(
            "SELECT id FROM embeddings WHERE embedding_id = ?", (ID1,)
        ).fetchone()
        conn.execute(
            "UPDATE embedding_metadata SET string_value = ? WHERE id = ? AND key = 'source'",
            (TARGET, row[0]),
        )
        conn.commit()
    finally:
        conn.close()
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert _snapshot_env(env) == before


def test_tracker_failure_after_rename_compensates_filesystem_and_registry(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    def _fail_tracker(_path: Path, _data: dict) -> None:
        raise RuntimeError("simulated tracker failure")

    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
        tracker_writer=_fail_tracker,
    )
    assert result.success is False
    assert result.compensated is True
    assert (env["library"] / TARGET).is_file()
    assert not (env["library"] / QUARANTINE).exists()
    with open_registry(env["registry"]) as conn:
        target_state = get_locator_lifecycle_state(conn, source_file_id=env["target_sf"])
        assert target_state is not None
        assert target_state["activity_state"] == LOCATOR_ACTIVE


def test_chroma_failure_compensates_tracker_filesystem_and_registry(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    def _fail_chroma(_path: Path, _updates: dict) -> None:
        raise RuntimeError("simulated chroma failure")

    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
        chroma_updater=_fail_chroma,
    )
    assert result.success is False
    assert result.compensated is True
    assert (env["library"] / TARGET).is_file()
    tracker = read_tracker_json(env["tracker"])
    assert tracker[HASH]["paths"] == [OLD]


def test_pre_commit_verification_failure_compensates(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    with mock.patch.object(
        qd_module,
        "_verify_pre_commit",
        side_effect=qd_module.QuarantineDeleteError("simulated pre-commit failure"),
    ):
        result = _execute(
            env,
            _request(
                env,
                preflight_evidence=preflight_evidence,
                approval_artifact=approval_artifact,
                delete_context=delete_context,
                execute=True,
            ),
        )
    assert result.success is False
    assert result.compensated is True
    assert (env["library"] / TARGET).is_file()


def test_post_commit_verification_failure_returns_recovery_required(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    with mock.patch.object(
        qd_module,
        "_verify_post_commit",
        side_effect=qd_module.QuarantineDeleteError("simulated post-commit failure"),
    ):
        result = _execute(
            env,
            _request(
                env,
                preflight_evidence=preflight_evidence,
                approval_artifact=approval_artifact,
                delete_context=delete_context,
                execute=True,
            ),
        )
    assert result.success is False
    assert result.outcome == OUTCOME_RECOVERY_REQUIRED
    assert result.recovery_required is True
    assert result.compensated is False
    assert _journal_phase(env) == PHASE_RECOVERY_REQUIRED


def test_journal_exists_before_filesystem_rename_and_phase_order_valid(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    phases: list[str] = []

    original_create = qd_module.create_prepared_journal
    original_advance = qd_module.advance_journal_phase
    original_rename = qd_module._atomic_same_filesystem_rename

    def _tracking_create(*args, **kwargs):
        path = original_create(*args, **kwargs)
        phases.append("PREPARED")
        assert path.is_file()
        return path

    def _tracking_advance(journal_path, phase, **kwargs):
        phases.append(phase)
        return original_advance(journal_path, phase, **kwargs)

    renamed = {"done": False}

    def _tracking_rename(src: Path, dst: Path) -> None:
        assert "PREPARED" in phases
        assert "REGISTRY_PREPARED" in phases
        assert "FILESYSTEM_QUARANTINED" not in phases
        renamed["done"] = True
        original_rename(src, dst)

    with mock.patch(
        "rag_engine.governed_delete.quarantine_executor.create_prepared_journal",
        side_effect=_tracking_create,
    ):
        with mock.patch(
            "rag_engine.governed_delete.quarantine_executor.advance_journal_phase",
            side_effect=_tracking_advance,
        ):
            with mock.patch.object(
                qd_module,
                "_atomic_same_filesystem_rename",
                side_effect=_tracking_rename,
            ):
                result = _execute(
                    env,
                    _request(
                        env,
                        preflight_evidence=preflight_evidence,
                        approval_artifact=approval_artifact,
                        delete_context=delete_context,
                        execute=True,
                    ),
                )
    assert result.success is True
    assert renamed["done"] is True
    assert phases.index("PREPARED") < phases.index("REGISTRY_PREPARED")
    assert phases.index("REGISTRY_PREPARED") < phases.index("FILESYSTEM_QUARANTINED")


def test_no_permanent_deletion_or_status_mutation(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    with open_registry(env["registry"]) as conn:
        before_rows = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        before_events = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events"
        ).fetchone()["c"]
        retained_status = conn.execute(
            "SELECT status FROM source_files WHERE source_file_id = ?",
            (env["retained_sf"],),
        ).fetchone()["status"]
    chroma_path = chroma_sqlite_path(env["persist"])
    before_vectors = sqlite3.connect(str(chroma_path)).execute(
        "SELECT COUNT(*) FROM embeddings"
    ).fetchone()[0]

    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert result.success is True
    assert (env["library"] / QUARANTINE).is_file()
    with open_registry(env["registry"]) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"] == before_rows
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM source_file_events").fetchone()["c"]
            > before_events
        )
        assert conn.execute(
            "SELECT status FROM source_files WHERE source_file_id = ?",
            (env["retained_sf"],),
        ).fetchone()["status"] == retained_status
        assert conn.execute(
            "SELECT status FROM source_files WHERE source_file_id = ?",
            (env["target_sf"],),
        ).fetchone()["status"] is None
    after_vectors = sqlite3.connect(str(chroma_path)).execute(
        "SELECT COUNT(*) FROM embeddings"
    ).fetchone()[0]
    assert after_vectors == before_vectors


def test_missing_target_locator_blocks_before_writes(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    persist = tmp_path / "persist"
    registry = (tmp_path / "registry" / "metadata.sqlite3").resolve()
    registry.parent.mkdir(parents=True, exist_ok=True)
    env = _seed_executor_env(
        {
            "library": library,
            "persist": persist,
            "registry": registry,
            "tracker": (persist / "embedded.json").resolve(),
            "chroma": (persist / "chroma.sqlite3").resolve(),
        },
        register_target=False,
    )
    env["skip_governed_target"] = True
    delete_context = QuarantineDeleteApprovalContext(
        registry_db_path=str(env["registry"]),
        library_root=str(env["library"].resolve()),
        persist_dir=str(env["persist"].resolve()),
        tracker_path=str(env["tracker"]),
    )
    preflight_evidence = collect_quarantine_delete_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        target_path=TARGET,
        retained_path=OLD,
        quarantine_path=QUARANTINE,
    )
    approval_artifact = _build_artifact(preflight_evidence, delete_context)
    digest = approval_artifact["approval_digest"]
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert "not registered" in (result.error_message or "").lower()
    _assert_blocked_no_artifacts(env, result, before=before, digest=digest)
    assert (env["library"] / TARGET).is_file()
    with open_registry(env["registry"]) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM source_file_locator_state").fetchone()["c"] == 1


def test_wrong_target_document_id_blocks_before_writes(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    other_bytes = b"other document bytes for mismatch"
    other_doc = document_id_from_bytes(other_bytes)
    other_hash = source_hash_from_bytes(other_bytes)
    other_subject = subject_id_from_key("maker_doc", "quarantine-delete-other")
    digest = approval_artifact["approval_digest"]
    with open_registry(env["registry"]) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=other_subject)
            register_document_version(
                conn,
                document_id=other_doc,
                subject_id=other_subject,
                source_hash=other_hash,
            )
            conn.execute(
                "UPDATE source_files SET document_id = ? WHERE source_file_id = ?",
                (other_doc, env["target_sf"]),
            )
        conn.commit()
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    msg = (result.error_message or "").lower()
    assert "document_id" in msg or "plan_digest" in msg
    _assert_blocked_no_artifacts(env, result, before=before, digest=digest)


def test_wrong_target_relative_path_blocks_before_writes(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    wrong_path = "00_Career/03_Engine_Knowledge/MAN_Academy/wrong_target.pdf"
    digest = approval_artifact["approval_digest"]
    with open_registry(env["registry"]) as conn:
        conn.execute(
            "UPDATE source_files SET relative_path = ? WHERE source_file_id = ?",
            (wrong_path, env["target_sf"]),
        )
        conn.commit()
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    msg = (result.error_message or "").lower()
    assert "target_path" in msg or "plan_digest" in msg
    _assert_blocked_no_artifacts(env, result, before=before, digest=digest)


def test_target_without_v5_state_blocks_before_writes(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    digest = approval_artifact["approval_digest"]
    with open_registry(env["registry"]) as conn:
        conn.execute(
            "DELETE FROM source_file_locator_state WHERE source_file_id = ?",
            (env["target_sf"],),
        )
        conn.commit()
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert "lifecycle projection" in (result.error_message or "").lower()
    _assert_blocked_no_artifacts(env, result, before=before, digest=digest)


def test_inactive_target_blocks_before_writes(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    digest = approval_artifact["approval_digest"]
    with open_registry(env["registry"]) as conn:
        with registry_transaction(conn):
            request = record_compensation_request(
                conn,
                source_file_id=env["target_sf"],
                document_id=DOC,
                registration_v4_event_id=_resolve_alias_event_id(conn, env["target_sf"]),
                compensation_approval_digest=_digest("inactive-target"),
                operation_id="prior-inactivate",
            )
            record_terminal_compensation_outcome(
                conn,
                source_file_id=env["target_sf"],
                document_id=DOC,
                compensation_request_v4_event_id=int(request["v4_event"]["event_id"]),
                outcome="COMPLETED",
                operation_id="prior-inactivate",
            )
        conn.commit()
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert "activity_state active" in (result.error_message or "").lower()
    _assert_blocked_no_artifacts(env, result, before=before, digest=digest)


def test_target_without_alias_registration_blocks_before_writes(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    digest = approval_artifact["approval_digest"]
    with open_registry(env["registry"]) as conn:
        conn.execute(
            "UPDATE source_file_events SET event_type = ? "
            "WHERE source_file_id = ? AND event_type = ?",
            ("SOURCE_FILE_REGISTERED", env["target_sf"], SOURCE_FILE_EVENT_ALIAS_REGISTERED),
        )
        conn.commit()
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
        preflight_evidence=preflight_evidence,
    )
    assert "alias_registered" in (result.error_message or "").lower()
    _assert_blocked_no_artifacts(env, result, before=before, digest=digest)


def test_invalid_retained_locator_blocks_before_writes(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    digest = approval_artifact["approval_digest"]
    with open_registry(env["registry"]) as conn:
        with registry_transaction(conn):
            request = record_compensation_request(
                conn,
                source_file_id=env["retained_sf"],
                document_id=DOC,
                registration_v4_event_id=_resolve_alias_event_id(conn, env["retained_sf"]),
                compensation_approval_digest=_digest("inactive-retained"),
                operation_id="prior-inactivate-retained",
            )
            record_terminal_compensation_outcome(
                conn,
                source_file_id=env["retained_sf"],
                document_id=DOC,
                compensation_request_v4_event_id=int(request["v4_event"]["event_id"]),
                outcome="COMPLETED",
                operation_id="prior-inactivate-retained",
            )
        conn.commit()
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
    )
    assert "retained" in (result.error_message or "").lower()
    _assert_blocked_no_artifacts(env, result, before=before, digest=digest)


def test_read_only_preflight_runs_before_prepared_journal(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    order: list[str] = []

    def _tracking_validate(*args, **kwargs):
        order.append("registry_preflight")
        return validate_quarantine_registry_preconditions(*args, **kwargs)

    original_create = qd_module.create_prepared_journal

    def _tracking_create(*args, **kwargs):
        order.append("PREPARED")
        return original_create(*args, **kwargs)

    with mock.patch.object(
        qd_module,
        "validate_quarantine_registry_preconditions",
        side_effect=_tracking_validate,
    ):
        with mock.patch.object(
            qd_module,
            "create_prepared_journal",
            side_effect=_tracking_create,
        ):
            result = _execute(
                env,
                _request(
                    env,
                    preflight_evidence=preflight_evidence,
                    approval_artifact=approval_artifact,
                    delete_context=delete_context,
                    execute=True,
                ),
            )
    assert result.success is True
    assert order.index("registry_preflight") < order.index("PREPARED")


def test_expired_approval_blocks_under_lock(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
            execute=True,
        ),
        now_utc_provider=lambda: "2026-08-19T12:20:00Z",
    )
    assert result.success is False
    assert _snapshot_env(env) == before

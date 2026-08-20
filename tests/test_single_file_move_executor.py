"""Tests for bounded single-file filesystem MOVE executor (Phase E3)."""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from rag_engine.governed_move import (
    OUTCOME_BLOCKED,
    OUTCOME_COMPENSATED_BEFORE_COMMIT,
    OUTCOME_DRY_RUN,
    OUTCOME_RECOVERY_REQUIRED,
    OUTCOME_SUCCESS,
    SingleFileMoveError,
    SingleFileMoveRequest,
    execute_single_file_move,
)
from rag_engine.governed_move import single_file_move as sfm_module
from rag_engine.governed_move.move_journal import (
    JOURNAL_DIR_NAME,
    PHASE_COMPENSATED,
    PHASE_RECOVERY_REQUIRED,
    PHASE_VERIFIED,
    read_journal_exact,
)
from rag_engine.governed_reconciliation.explicit_move import read_tracker_json
from rag_engine.index_compatibility.constants import COMPAT_KNOWN_COMPATIBLE
from rag_engine.library_state.contract import EMBEDDING_NONE, INTENT_PLAN_MOVE
from rag_engine.library_state.evidence import lookup_chroma_by_embedding_ids
from rag_engine.library_state.move_approval import (
    MoveApprovalContext,
    compute_approval_digest,
    plan_digest,
)
from rag_engine.library_state.move_preflight import collect_pre_move_evidence
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    LOCATOR_INACTIVE,
    get_locator_lifecycle_state,
    initialize_locator_lifecycle_state,
    initialize_registry,
    migrate_connection,
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

OLD = "manuals/MAN_old_name.pdf"
NEW = "manuals/MAN_new_name.pdf"
THIRD = "manuals/other_alias.pdf"

BYTES = b"%PDF-1.4\nsingle file move executor fixture\n"
HASH = source_hash_from_bytes(BYTES)
DOC = document_id_from_bytes(BYTES)
SUBJECT = subject_id_from_key("maker_doc", "single-file-move")
FP = "ef" * 32
ID1 = chunk_id(DOC, FP, 0)
ID2 = chunk_id(DOC, FP, 1)
VECTOR_IDS = (ID1, ID2)
COLLECTION = "maker-manuals"
OPERATION_ID = "single-file-move-op-001"
NEW_PLACEHOLDER_SF = "sf-new-placeholder-0002"

ISSUED_AT = "2026-08-19T12:00:00Z"
EXPIRES_AT = "2026-08-19T12:15:00Z"
NOW_VALID = "2026-08-19T12:05:00Z"
NOW_EXPIRED = "2026-08-19T12:16:00Z"


def _valid_clock() -> str:
    return NOW_VALID


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
    for key in ("library", "persist", "registry", "tracker", "chroma", "lock"):
        path = env[key] if key != "lock" else env["persist"] / sfm_module.MOVE_LOCK_NAME
        if path.is_file():
            out[str(path)] = _file_sha(path)
        elif path.is_dir():
            out[str(path)] = json.dumps(sorted(p.name for p in path.iterdir()))
        else:
            out[str(path)] = None
    out[str(env["library"] / OLD)] = (
        _file_sha(env["library"] / OLD) if (env["library"] / OLD).is_file() else None
    )
    out[str(env["library"] / NEW)] = (
        _file_sha(env["library"] / NEW) if (env["library"] / NEW).is_file() else None
    )
    return out


def _seed_executor_env(env: dict) -> dict:
    _write(env["library"], OLD, BYTES)
    registry = env["registry"].resolve()
    initialize_registry(registry)
    with open_registry(registry) as conn:
        migrate_connection(conn, target_version=5)
        with registry_transaction(conn):
            register_subject(conn, subject_id=SUBJECT)
            register_document_version(
                conn, document_id=DOC, subject_id=SUBJECT, source_hash=HASH
            )
            old_sf = register_source_file(
                conn,
                document_id=DOC,
                relative_path=OLD,
                source_hash=HASH,
                collection=COLLECTION,
            )["source_file_id"]
            third_sf = register_source_file(
                conn,
                document_id=DOC,
                relative_path=THIRD,
                source_hash=HASH,
                collection=COLLECTION,
            )["source_file_id"]
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
        for sf in (old_sf, third_sf):
            initialize_locator_lifecycle_state(
                conn,
                source_file_id=sf,
                document_id=DOC,
                source="test_fixture",
            )
        conn.commit()
    _write_tracker(env["tracker"], paths=[OLD])
    _make_chroma(env["chroma"], source=OLD)
    env["old_sf"] = old_sf
    env["third_sf"] = third_sf
    return env


def _build_artifact(env: dict, evidence, context: MoveApprovalContext) -> dict:
    item = next(c for c in evidence.plan.classifications if c.target == OLD)
    artifact = {
        "schema_version": 1,
        "approval_id": "move-approval-sfm-001",
        "operation": "MOVE",
        "intent": INTENT_PLAN_MOVE,
        "request_id": evidence.plan.request_id,
        "plan_digest": plan_digest(evidence.plan),
        "source_path": OLD,
        "destination_path": evidence.destination_path,
        "document_id": item.document_id,
        "source_hash": item.source_hash,
        "resolver_classification": evidence.plan.classification,
        "proposed_operation": evidence.plan.proposed_operation,
        "expected_embedding_action": EMBEDDING_NONE,
        "expected_new_vectors": 0,
        "affected_source_file_ids": list(context.affected_source_file_ids),
        "registry_db_path": context.registry_db_path,
        "library_root": context.library_root,
        "persist_dir": context.persist_dir,
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
def move_context(env: dict) -> MoveApprovalContext:
    return MoveApprovalContext(
        affected_source_file_ids=(env["old_sf"], NEW_PLACEHOLDER_SF),
        registry_db_path=str(env["registry"]),
        library_root=str(env["library"].resolve()),
        persist_dir=str(env["persist"].resolve()),
    )


@pytest.fixture()
def pre_move_evidence(env: dict):
    return collect_pre_move_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        source_path=OLD,
        destination_path=NEW,
        operation_id=OPERATION_ID,
    )


@pytest.fixture()
def approval_artifact(env: dict, pre_move_evidence, move_context: MoveApprovalContext):
    return _build_artifact(env, pre_move_evidence, move_context)


def _request(
    env: dict,
    *,
    pre_move_evidence,
    approval_artifact: dict,
    move_context: MoveApprovalContext,
    execute: bool = False,
    **overrides,
) -> SingleFileMoveRequest:
    base = {
        "approval_artifact": approval_artifact,
        "pre_move_evidence": pre_move_evidence,
        "approval_context": move_context,
        "source_relative_path": OLD,
        "destination_relative_path": NEW,
        "library_root": str(env["library"].resolve()),
        "persist_dir": str(env["persist"].resolve()),
        "registry_db": str(env["registry"]),
        "tracker_path": str(env["tracker"]),
        "old_source_file_id": env["old_sf"],
        "approved_vector_ids": VECTOR_IDS,
        "registry_collection": COLLECTION,
        "operation_id": OPERATION_ID,
        "execute": execute,
    }
    base.update(overrides)
    return SingleFileMoveRequest(**base)


def _journal_phase(env: dict, operation_id: str = OPERATION_ID) -> str | None:
    root = env["persist"] / JOURNAL_DIR_NAME
    path = root / f"{operation_id}.json"
    if not path.is_file():
        return None
    return read_journal_exact(env["persist"], operation_id)["phase"]


def _execute(env, request, **kwargs):
    return execute_single_file_move(
        request,
        now_utc_provider=_valid_clock,
        **kwargs,
    )


def test_dry_run_performs_no_writes_and_does_not_acquire_lock(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=False,
        )
    )
    after = _snapshot_env(env)
    assert after == before
    assert result.success is True
    assert result.dry_run is True
    assert result.outcome == OUTCOME_DRY_RUN
    assert not (env["persist"] / sfm_module.MOVE_LOCK_NAME).exists()
    assert not (env["persist"] / JOURNAL_DIR_NAME).exists()


def test_happy_path_execute_moves_file_and_updates_bounded_stores(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        )
    )
    assert result.success is True
    assert result.outcome == OUTCOME_SUCCESS
    assert result.registry_committed is True
    assert not (env["library"] / OLD).exists()
    assert (env["library"] / NEW).is_file()
    assert _file_sha(env["library"] / NEW) == HASH

    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["old_sf"])["activity_state"]
            == LOCATOR_INACTIVE
        )
        new_sf = result.registry_transition.new_source_file_id
        assert (
            get_locator_lifecycle_state(conn, source_file_id=new_sf)["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["third_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        dest_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"]
        assert dest_rows == 1

    tracker = read_tracker_json(env["tracker"])
    assert tracker[HASH]["paths"] == [NEW]
    assert tracker[HASH]["chunk_ids"] == list(VECTOR_IDS)

    lookup = lookup_chroma_by_embedding_ids(env["chroma"], VECTOR_IDS)
    for rec in lookup.records:
        assert rec.source_path == NEW
    assert _journal_phase(env) == PHASE_VERIFIED


def test_fresh_evidence_drift_blocks_with_no_writes(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    before_registry = _file_sha(env["registry"])
    before_old = _file_sha(env["library"] / OLD)
    tracker = json.loads(env["tracker"].read_text(encoding="utf-8"))
    tracker[HASH]["paths"] = ["manuals/drifted.pdf"]
    env["tracker"].write_text(json.dumps(tracker) + "\n", encoding="utf-8")
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        )
    )
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert (env["library"] / OLD).is_file()
    assert not (env["library"] / NEW).exists()
    assert _file_sha(env["registry"]) == before_registry
    assert _file_sha(env["library"] / OLD) == before_old


def test_destination_created_after_approval_blocks_with_no_writes(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    _write(env["library"], NEW, BYTES)
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        )
    )
    after = _snapshot_env(env)
    assert result.success is False
    assert (env["library"] / OLD).is_file()
    assert after[str(env["library"] / OLD)] == before[str(env["library"] / OLD)]


def test_lock_contention_blocks_with_no_writes(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    lock = env["persist"] / sfm_module.MOVE_LOCK_NAME
    lock.write_text("held\n", encoding="utf-8")
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        )
    )
    after = _snapshot_env(env)
    assert result.success is False
    assert "lock" in (result.error_message or "").lower()
    assert after == before


def test_cross_device_rename_failure_blocks_without_copy_fallback(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    def _exdev(_src: Path, _dst: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
        filesystem_renamer=_exdev,
    )
    after = _snapshot_env(env)
    assert result.success is False
    assert result.compensated is True
    assert result.outcome == OUTCOME_COMPENSATED_BEFORE_COMMIT
    assert _journal_phase(env) == PHASE_COMPENSATED
    assert (env["library"] / OLD).is_file()
    assert not (env["library"] / NEW).exists()
    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["old_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0


def test_failure_after_registry_prep_before_filesystem_rolls_back_registry(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    def _fail_rename(_src: Path, _dst: Path) -> None:
        raise RuntimeError("simulated rename failure")

    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
        filesystem_renamer=_fail_rename,
    )
    assert result.success is False
    assert result.compensated is True
    assert (env["library"] / OLD).is_file()
    with open_registry(env["registry"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0


def test_failure_after_filesystem_rename_before_tracker_restores_source_and_registry(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    def _fail_tracker(_path: Path, _data: dict) -> None:
        raise RuntimeError("simulated tracker failure")

    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
        tracker_writer=_fail_tracker,
    )
    assert result.success is False
    assert result.compensated is True
    assert (env["library"] / OLD).is_file()
    assert not (env["library"] / NEW).exists()
    assert _file_sha(env["library"] / OLD) == HASH
    with open_registry(env["registry"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0


def test_chroma_update_failure_restores_filesystem_tracker_and_registry(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    def _fail_chroma(_path: Path, _updates: dict) -> None:
        raise RuntimeError("simulated chroma failure")

    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
        chroma_updater=_fail_chroma,
    )
    assert result.success is False
    assert result.compensated is True
    assert (env["library"] / OLD).is_file()
    tracker = read_tracker_json(env["tracker"])
    assert tracker[HASH]["paths"] == [OLD]
    with open_registry(env["registry"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0


def test_pre_commit_verification_failure_restores_bounded_state(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    with mock.patch.object(
        sfm_module,
        "_verify_pre_commit",
        side_effect=SingleFileMoveError("simulated pre-commit failure"),
    ):
        result = _execute(
            env,
            _request(
                env,
                pre_move_evidence=pre_move_evidence,
                approval_artifact=approval_artifact,
                move_context=move_context,
                execute=True,
            ),
        )
    assert result.success is False
    assert result.compensated is True
    assert (env["library"] / OLD).is_file()
    with open_registry(env["registry"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0


def test_post_commit_read_back_failure_reports_recovery_required(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    with mock.patch.object(
        sfm_module,
        "_verify_post_commit",
        side_effect=SingleFileMoveError("simulated post-commit failure"),
    ):
        result = _execute(
            env,
            _request(
                env,
                pre_move_evidence=pre_move_evidence,
                approval_artifact=approval_artifact,
                move_context=move_context,
                execute=True,
            ),
        )
    assert result.success is False
    assert result.recovery_required is True
    assert result.outcome == OUTCOME_RECOVERY_REQUIRED
    assert result.registry_committed is True
    assert result.compensated is False
    assert _journal_phase(env) == PHASE_RECOVERY_REQUIRED
    assert (env["library"] / NEW).is_file()


def test_no_reconcile_path_or_unbounded_operations(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    with mock.patch("rag_engine.reconcile_path.inspect_reconcile_request") as inspect_mock:
        with mock.patch("rag_engine.reconcile_path.run_reconcile_path") as run_mock:
            with mock.patch("shutil.copy2") as copy_mock:
                with mock.patch("os.remove") as remove_mock:
                    with mock.patch(
                        "rag_engine.library_state.evidence.lookup_chroma_by_embedding_ids",
                        wraps=lookup_chroma_by_embedding_ids,
                    ) as chroma_lookup:
                        _execute(
                            env,
                            _request(
                                env,
                                pre_move_evidence=pre_move_evidence,
                                approval_artifact=approval_artifact,
                                move_context=move_context,
                                execute=True,
                                operation_id="op-bounded-001",
                            ),
                        )
                        inspect_mock.assert_not_called()
                        run_mock.assert_not_called()
                        copy_mock.assert_not_called()
                        remove_mock.assert_not_called()
                        for call in chroma_lookup.call_args_list:
                            assert call.args[1]


def test_symlink_escape_outside_library_root_is_rejected(
    env, pre_move_evidence, approval_artifact, move_context, tmp_path: Path
) -> None:
    outside = tmp_path / "outside_secret.pdf"
    outside.write_bytes(BYTES)
    source_file = env["library"] / OLD
    source_file.unlink()
    source_file.symlink_to(outside)
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        )
    )
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert "symlink" in (result.error_message or "").lower() or "outside library_root" in (
        result.error_message or ""
    ) or "escape" in (result.error_message or "")


def test_single_file_only_rejects_directory_source(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    source_file = env["library"] / OLD
    source_file.unlink()
    source_file.mkdir()
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        )
    )
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert "single file" in (result.error_message or "").lower() or "regular file" in (
        result.error_message or ""
    )


def test_default_execute_false_is_dry_run(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    req = _request(
        env,
        pre_move_evidence=pre_move_evidence,
        approval_artifact=approval_artifact,
        move_context=move_context,
    )
    assert req.execute is False
    result = execute_single_file_move(req, now_utc_provider=_valid_clock)
    assert result.dry_run is True


def test_no_fixed_default_now_utc_on_request() -> None:
    field_names = {f.name for f in dataclasses.fields(SingleFileMoveRequest)}
    assert "now_utc" not in field_names
    assert not hasattr(sfm_module, "_DEFAULT_NOW_UTC")


def test_expired_approval_blocks_with_no_writes(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    before = _snapshot_env(env)
    result = execute_single_file_move(
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
        now_utc_provider=lambda: NOW_EXPIRED,
    )
    after = _snapshot_env(env)
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert "expired" in (result.error_message or "").lower()
    assert after == before


def test_valid_approval_succeeds_with_injected_clock(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    result = execute_single_file_move(
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
        now_utc_provider=lambda: NOW_VALID,
    )
    assert result.success is True
    assert result.outcome == OUTCOME_SUCCESS


def test_under_lock_evidence_drift_blocks_without_stale_restore(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    original_lock = sfm_module._governed_move_lock

    @contextmanager
    def _drift_on_lock(persist_dir: str):
        tracker = json.loads(env["tracker"].read_text(encoding="utf-8"))
        tracker[HASH]["paths"] = ["manuals/drifted_under_lock.pdf"]
        env["tracker"].write_text(json.dumps(tracker) + "\n", encoding="utf-8")
        with original_lock(persist_dir):
            yield

    before = _snapshot_env(env)
    with mock.patch.object(sfm_module, "_governed_move_lock", _drift_on_lock):
        result = _execute(
            env,
            _request(
                env,
                pre_move_evidence=pre_move_evidence,
                approval_artifact=approval_artifact,
                move_context=move_context,
                execute=True,
            ),
        )
    after = _snapshot_env(env)
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert (env["library"] / OLD).is_file()
    assert not (env["library"] / NEW).exists()
    assert after[str(env["library"] / OLD)] == before[str(env["library"] / OLD)]
    tracker = read_tracker_json(env["tracker"])
    assert tracker[HASH]["paths"] == ["manuals/drifted_under_lock.pdf"]


def test_destination_parent_removed_after_approval_blocks_without_rename(
    env, move_context, tmp_path: Path
) -> None:
    nested_new = "manuals/subdir/MAN_new_name.pdf"
    subdir = env["library"] / "manuals" / "subdir"
    subdir.mkdir(parents=True)
    evidence = collect_pre_move_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        source_path=OLD,
        destination_path=nested_new,
        operation_id=OPERATION_ID,
    )
    artifact = _build_artifact(env, evidence, move_context)
    before_old = _file_sha(env["library"] / OLD)
    subdir.rmdir()
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=evidence,
            approval_artifact=artifact,
            move_context=move_context,
            execute=True,
            destination_relative_path=nested_new,
        ),
    )
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert (env["library"] / OLD).is_file()
    assert not (env["library"] / nested_new).exists()
    assert _file_sha(env["library"] / OLD) == before_old
    assert "parent" in (result.error_message or "").lower()


def test_missing_persist_dir_blocks_without_creating_it(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    persist = env["persist"]
    for child in persist.iterdir():
        if child.is_file():
            child.unlink()
    persist.rmdir()
    assert not persist.exists()
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
    )
    assert result.success is False
    assert not persist.exists()
    assert "persist_dir" in (result.error_message or "").lower()


def test_destination_parent_symlink_outside_library_root_blocks(
    env, move_context, tmp_path: Path
) -> None:
    outside_dir = tmp_path / "outside_manuals"
    outside_dir.mkdir()
    dest_parent = env["library"] / "manuals_link"
    dest_parent.mkdir()
    nested_new = "manuals_link/MAN_new_name.pdf"
    evidence = collect_pre_move_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        source_path=OLD,
        destination_path=nested_new,
        operation_id=OPERATION_ID,
    )
    artifact = _build_artifact(env, evidence, move_context)
    dest_parent.rmdir()
    dest_parent.symlink_to(outside_dir, target_is_directory=True)
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=evidence,
            approval_artifact=artifact,
            move_context=move_context,
            execute=True,
            destination_relative_path=nested_new,
        ),
    )
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert (env["library"] / OLD).is_file()
    assert not (env["library"] / nested_new).exists()
    msg = (result.error_message or "").lower()
    assert "outside library_root" in msg or "symlink" in msg


def test_source_symlink_inside_library_blocks_before_mutation(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    real_file = env["library"] / "manuals" / "real_source.pdf"
    real_file.write_bytes(BYTES)
    source_file = env["library"] / OLD
    source_file.unlink()
    source_file.symlink_to(real_file)
    before = _snapshot_env(env)
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
    )
    after = _snapshot_env(env)
    assert result.success is False
    assert result.outcome == OUTCOME_BLOCKED
    assert "symlink" in (result.error_message or "").lower()
    assert (env["library"] / OLD).is_symlink()
    assert real_file.is_file()
    assert not (env["library"] / NEW).exists()
    assert after[str(env["library"] / OLD)] == before[str(env["library"] / OLD)]


def _corrupt_chroma_metadata(
    chroma_path: Path,
    *,
    vector_id: str,
    key: str,
    value: str,
) -> None:
    conn = sqlite3.connect(str(chroma_path))
    try:
        row = conn.execute(
            "SELECT e.id FROM embeddings e WHERE e.embedding_id = ?",
            (vector_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"missing embedding {vector_id!r}")
        conn.execute(
            "UPDATE embedding_metadata SET string_value = ? "
            "WHERE id = ? AND key = ?",
            (value, row[0], key),
        )
        conn.commit()
    finally:
        conn.close()


def test_pre_commit_chroma_identity_corruption_compensates(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    real_update = sfm_module.update_chroma_source_metadata

    def _update_then_corrupt(path: Path, updates: dict) -> None:
        real_update(path, updates)
        _corrupt_chroma_metadata(
            path,
            vector_id=VECTOR_IDS[0],
            key="document_id",
            value="corrupted-doc-id",
        )

    result = execute_single_file_move(
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
        now_utc_provider=_valid_clock,
        chroma_updater=_update_then_corrupt,
    )
    assert result.success is False
    assert result.compensated is True
    assert (env["library"] / OLD).is_file()
    assert not (env["library"] / NEW).exists()
    lookup = lookup_chroma_by_embedding_ids(env["chroma"], VECTOR_IDS)
    for rec in lookup.records:
        assert rec.source_path == OLD
        assert rec.metadata.get("document_id") == DOC
        assert rec.metadata.get("source_hash") == HASH
    with open_registry(env["registry"]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0


def test_post_commit_chroma_identity_corruption_reports_recovery_required(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    real_verify = sfm_module._verify_post_commit

    def _verify_with_corruption(*args, **kwargs):
        _corrupt_chroma_metadata(
            env["chroma"],
            vector_id=VECTOR_IDS[0],
            key="source_hash",
            value="corrupted-hash",
        )
        return real_verify(*args, **kwargs)

    with mock.patch.object(sfm_module, "_verify_post_commit", _verify_with_corruption):
        result = _execute(
            env,
            _request(
                env,
                pre_move_evidence=pre_move_evidence,
                approval_artifact=approval_artifact,
                move_context=move_context,
                execute=True,
            ),
        )
    assert result.success is False
    assert result.outcome == OUTCOME_RECOVERY_REQUIRED
    assert result.recovery_required is True
    assert result.registry_committed is True
    assert (env["library"] / NEW).is_file()


def test_happy_path_verifies_chroma_source_document_id_and_source_hash(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
    )
    assert result.success is True
    lookup = lookup_chroma_by_embedding_ids(env["chroma"], VECTOR_IDS)
    assert tuple(sorted(lookup.found_ids)) == tuple(sorted(VECTOR_IDS))
    assert not lookup.missing_ids
    for rec in lookup.records:
        assert rec.source_path == NEW
        assert rec.metadata.get("document_id") == DOC
        assert rec.metadata.get("source_hash") == HASH

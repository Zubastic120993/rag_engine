"""Tests for durable single-file MOVE journaling and recovery (Phase E4)."""

from __future__ import annotations

import errno
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from rag_engine.governed_move import (
    OUTCOME_COMPENSATED_BEFORE_COMMIT,
    OUTCOME_DRY_RUN,
    OUTCOME_RECOVERY_REQUIRED,
    OUTCOME_SUCCESS,
    SingleFileMoveRequest,
    execute_single_file_move,
    recover_single_file_move,
)
from rag_engine.governed_move import single_file_move as sfm_module
from rag_engine.governed_move.move_journal import (
    JOURNAL_DIR_NAME,
    PHASE_COMPENSATED,
    PHASE_FILESYSTEM_MOVED,
    PHASE_PREPARED,
    PHASE_RECOVERY_REQUIRED,
    PHASE_REGISTRY_COMMITTED,
    PHASE_REGISTRY_PREPARED,
    PHASE_VERIFIED,
    MoveJournalError,
    create_prepared_journal,
    journal_path_for_operation,
    read_journal_exact,
    validate_operation_id,
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

BYTES = b"%PDF-1.4\nsingle file move journal fixture\n"
HASH = source_hash_from_bytes(BYTES)
DOC = document_id_from_bytes(BYTES)
SUBJECT = subject_id_from_key("maker_doc", "single-file-move-journal")
FP = "ef" * 32
ID1 = chunk_id(DOC, FP, 0)
ID2 = chunk_id(DOC, FP, 1)
VECTOR_IDS = (ID1, ID2)
COLLECTION = "maker-manuals"
OPERATION_ID = "single-file-move-journal-op-001"
OTHER_OPERATION_ID = "single-file-move-journal-op-other"
NEW_PLACEHOLDER_SF = "sf-new-placeholder-journal"

ISSUED_AT = "2026-08-19T12:00:00Z"
EXPIRES_AT = "2026-08-19T12:15:00Z"
NOW_VALID = "2026-08-19T12:05:00Z"


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


def _journal_path(env: dict, operation_id: str = OPERATION_ID) -> Path:
    return journal_path_for_operation(env["persist"], operation_id)


def _read_journal(env: dict, operation_id: str = OPERATION_ID) -> dict:
    return read_journal_exact(env["persist"], operation_id)


def _seed_env(env: dict) -> dict:
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
        "approval_id": "move-approval-journal-001",
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
    return _seed_env(base)


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


def _execute(env, request, **kwargs):
    return execute_single_file_move(
        request,
        now_utc_provider=_valid_clock,
        **kwargs,
    )


def _recover(env, operation_id: str = OPERATION_ID):
    return recover_single_file_move(
        persist_dir=str(env["persist"].resolve()),
        operation_id=operation_id,
        library_root=str(env["library"].resolve()),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
    )


def test_prepared_journal_written_before_filesystem_rename(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    observed: list[str] = []
    real_rename = sfm_module._atomic_same_filesystem_rename

    def _rename(src: Path, dst: Path) -> None:
        journal_path = _journal_path(env)
        assert journal_path.is_file()
        journal = _read_journal(env)
        observed.append(journal["phase"])
        real_rename(src, dst)

    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
        filesystem_renamer=_rename,
    )
    assert result.success is True
    assert observed == [PHASE_REGISTRY_PREPARED]
    final = _read_journal(env)
    assert final["phase"] == PHASE_VERIFIED


def test_happy_path_ends_with_immutable_verified_journal(
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
    assert result.outcome == OUTCOME_SUCCESS
    journal_path = _journal_path(env)
    before = journal_path.read_bytes()
    journal = _read_journal(env)
    assert journal["phase"] == PHASE_VERIFIED
    assert journal["operation_id"] == OPERATION_ID
    assert journal["expected_sha256"] == HASH
    assert journal_path.read_bytes() == before


def test_tracker_failure_after_filesystem_move_produces_compensated_journal(
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
    assert result.outcome == OUTCOME_COMPENSATED_BEFORE_COMMIT
    journal = _read_journal(env)
    assert journal["phase"] == PHASE_COMPENSATED
    assert (env["library"] / OLD).is_file()
    assert not (env["library"] / NEW).exists()


def test_chroma_failure_produces_compensated_journal(
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
    assert result.compensated is True
    journal = _read_journal(env)
    assert journal["phase"] == PHASE_COMPENSATED


def test_failure_after_registry_commit_produces_recovery_required_journal(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    with mock.patch.object(
        sfm_module,
        "_verify_post_commit",
        side_effect=sfm_module.SingleFileMoveError("simulated post-commit failure"),
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
    assert result.outcome == OUTCOME_RECOVERY_REQUIRED
    journal = _read_journal(env)
    assert journal["phase"] == PHASE_RECOVERY_REQUIRED
    assert journal["residual_recovery_notes"]


def test_recovery_after_interruption_at_filesystem_moved(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    side_snapshot = sfm_module._capture_side_store_snapshot(
        {
            "library_root": str(env["library"].resolve()),
            "persist_dir": str(env["persist"].resolve()),
            "registry_db": str(env["registry"]),
            "tracker_path": str(env["tracker"]),
            "source_path": OLD,
            "destination_path": NEW,
            "source_hash": HASH,
            "document_id": DOC,
            "old_source_file_id": env["old_sf"],
            "approved_vector_ids": VECTOR_IDS,
            "registry_collection": COLLECTION,
            "operation_id": OPERATION_ID,
        }
    )
    os_replace = sfm_module.os.replace
    source = env["library"] / OLD
    dest = env["library"] / NEW
    os_replace(source, dest)
    create_prepared_journal(
        persist_dir=str(env["persist"].resolve()),
        operation_id=OPERATION_ID,
        source_relative_path=OLD,
        destination_relative_path=NEW,
        expected_sha256=HASH,
        document_id=DOC,
        source_hash=HASH,
        approved_vector_ids=VECTOR_IDS,
        old_source_file_id=env["old_sf"],
        tracker_snapshot=side_snapshot.tracker_root,
        chroma_identity_snapshot=sfm_module._chroma_identity_for_journal(side_snapshot),
        initial_registry_state_summary=sfm_module._capture_initial_registry_state_summary(
            {
                "document_id": DOC,
                "old_source_file_id": env["old_sf"],
                "source_path": OLD,
            },
            side_snapshot,
        ),
        library_root=str(env["library"].resolve()),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
        registry_collection=COLLECTION,
        created_at=NOW_VALID,
    )
    from rag_engine.governed_move.move_journal import advance_journal_phase

    advance_journal_phase(_journal_path(env), PHASE_FILESYSTEM_MOVED, updated_at=NOW_VALID)

    result = _recover(env)
    assert result.compensated is True
    assert result.phase == PHASE_COMPENSATED
    assert (env["library"] / OLD).is_file()
    assert not (env["library"] / NEW).exists()


def test_recovery_after_registry_committed_verifies_or_reports_without_rollback(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    execute_result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
    )
    assert execute_result.success is True
    journal_path = _journal_path(env)
    journal = _read_journal(env)
    journal["phase"] = PHASE_REGISTRY_COMMITTED
    journal_path.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    before_registry = _file_sha(env["registry"])
    result = _recover(env)
    assert result.verified is True
    assert result.phase == PHASE_VERIFIED
    assert _file_sha(env["registry"]) == before_registry
    assert (env["library"] / NEW).is_file()


def test_invalid_traversal_and_symlink_operation_journal_paths_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(MoveJournalError):
        validate_operation_id("../escape")
    with pytest.raises(MoveJournalError):
        validate_operation_id("bad/op")
    with pytest.raises(MoveJournalError):
        validate_operation_id("bad\x00id")

    outside = tmp_path / "outside_persist"
    outside.mkdir()
    persist = tmp_path / "persist_link"
    persist.symlink_to(outside)
    with pytest.raises(MoveJournalError, match="symlink"):
        journal_path_for_operation(persist, OPERATION_ID)


def test_existing_journal_operation_id_rejected_without_overwrite(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    journal_path = env["persist"] / JOURNAL_DIR_NAME / f"{OPERATION_ID}.json"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text('{"operation_id":"x"}\n', encoding="utf-8")
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
    assert "journal already exists" in (result.error_message or "").lower()
    assert journal_path.read_text(encoding="utf-8") == '{"operation_id":"x"}\n'


def test_dry_run_creates_no_lock_or_journal(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=False,
        ),
    )
    assert result.outcome == OUTCOME_DRY_RUN
    assert not (env["persist"] / sfm_module.MOVE_LOCK_NAME).exists()
    assert not (env["persist"] / JOURNAL_DIR_NAME).exists()


def test_execute_validation_uses_one_under_lock_clock_without_pre_lock_approval(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    calls: list[str] = []

    original = sfm_module.validate_move_approval

    def _spy(*args, **kwargs):
        calls.append(kwargs.get("now_utc", ""))
        return original(*args, **kwargs)

    with mock.patch.object(sfm_module, "validate_move_approval", side_effect=_spy):
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
    assert calls == [NOW_VALID]


def test_recovery_never_lists_journal_directories_or_touches_unrelated_journals(
    env, pre_move_evidence, approval_artifact, move_context, monkeypatch
) -> None:
    _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
            operation_id=OPERATION_ID,
        ),
    )
    other_path = _journal_path(env, OTHER_OPERATION_ID)
    other_path.write_text('{"phase":"VERIFIED"}\n', encoding="utf-8")
    other_before = other_path.read_text(encoding="utf-8")

    original_iterdir = Path.iterdir

    def _iterdir(self):
        if JOURNAL_DIR_NAME in self.parts:
            raise AssertionError("recovery must not list journal directories")
        return original_iterdir(self)

    with mock.patch.object(Path, "iterdir", _iterdir):
        result = _recover(env, OPERATION_ID)
    assert result.verified is True
    assert other_path.read_text(encoding="utf-8") == other_before


def test_chroma_failure_recovery_required_when_restore_fails(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    real_update = sfm_module.update_chroma_source_metadata

    def _update_then_corrupt(path: Path, updates: dict) -> None:
        real_update(path, updates)
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(
                "SELECT e.id FROM embeddings e WHERE e.embedding_id = ?",
                (VECTOR_IDS[0],),
            ).fetchone()
            conn.execute(
                "UPDATE embedding_metadata SET string_value = ? WHERE id = ? AND key = ?",
                ("corrupted-doc-id", row[0], "document_id"),
            )
            conn.commit()
        finally:
            conn.close()

    result = _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
        chroma_updater=_update_then_corrupt,
    )
    assert result.outcome in {OUTCOME_COMPENSATED_BEFORE_COMMIT, OUTCOME_RECOVERY_REQUIRED}
    journal = _read_journal(env)
    assert journal["phase"] in {PHASE_COMPENSATED, PHASE_RECOVERY_REQUIRED}


def test_recovery_idempotent_for_terminal_phases(
    env, pre_move_evidence, approval_artifact, move_context
) -> None:
    _execute(
        env,
        _request(
            env,
            pre_move_evidence=pre_move_evidence,
            approval_artifact=approval_artifact,
            move_context=move_context,
            execute=True,
        ),
    )
    first = _recover(env)
    second = _recover(env)
    assert first.phase == PHASE_VERIFIED
    assert second.phase == PHASE_VERIFIED
    assert _read_journal(env)["phase"] == PHASE_VERIFIED


def _prepared_journal_fixture(env: dict) -> None:
    side_snapshot = sfm_module._capture_side_store_snapshot(
        {
            "library_root": str(env["library"].resolve()),
            "persist_dir": str(env["persist"].resolve()),
            "registry_db": str(env["registry"]),
            "tracker_path": str(env["tracker"]),
            "source_path": OLD,
            "destination_path": NEW,
            "source_hash": HASH,
            "document_id": DOC,
            "old_source_file_id": env["old_sf"],
            "approved_vector_ids": VECTOR_IDS,
            "registry_collection": COLLECTION,
            "operation_id": OPERATION_ID,
        }
    )
    create_prepared_journal(
        persist_dir=str(env["persist"].resolve()),
        operation_id=OPERATION_ID,
        source_relative_path=OLD,
        destination_relative_path=NEW,
        expected_sha256=HASH,
        document_id=DOC,
        source_hash=HASH,
        approved_vector_ids=VECTOR_IDS,
        old_source_file_id=env["old_sf"],
        tracker_snapshot=side_snapshot.tracker_root,
        chroma_identity_snapshot=sfm_module._chroma_identity_for_journal(side_snapshot),
        initial_registry_state_summary=sfm_module._capture_initial_registry_state_summary(
            {
                "document_id": DOC,
                "old_source_file_id": env["old_sf"],
                "source_path": OLD,
            },
            side_snapshot,
        ),
        library_root=str(env["library"].resolve()),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
        registry_collection=COLLECTION,
        created_at=NOW_VALID,
    )


def test_recovery_stale_pre_lock_race_rereads_verified_under_lock(env) -> None:
    _prepared_journal_fixture(env)
    assert _read_journal(env)["phase"] == PHASE_PREPARED

    original_lock = sfm_module._governed_move_lock

    @contextmanager
    def _executor_completes_on_lock(persist_dir: str):
        with original_lock(persist_dir):
            journal_path = _journal_path(env)
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["phase"] = PHASE_VERIFIED
            journal_path.write_text(
                json.dumps(journal, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            yield

    with mock.patch.object(sfm_module, "_governed_move_lock", _executor_completes_on_lock):
        with mock.patch.object(sfm_module, "_compensate_before_commit") as compensate_mock:
            with mock.patch.object(sfm_module.os, "replace") as rename_mock:
                with mock.patch.object(
                    sfm_module, "write_tracker_atomic"
                ) as tracker_mock:
                    with mock.patch.object(
                        sfm_module, "update_chroma_source_metadata"
                    ) as chroma_mock:
                        result = _recover(env)

    assert result.verified is True
    assert result.phase == PHASE_VERIFIED
    assert _read_journal(env)["phase"] == PHASE_VERIFIED
    compensate_mock.assert_not_called()
    rename_mock.assert_not_called()
    tracker_mock.assert_not_called()
    chroma_mock.assert_not_called()


def test_recovery_terminal_phase_evaluated_only_after_lock(env, move_context) -> None:
    evidence = collect_pre_move_evidence(
        library_root=env["library"],
        persist_dir=env["persist"],
        registry_db=env["registry"],
        tracker_path=env["tracker"],
        source_path=OLD,
        destination_path=NEW,
        operation_id=OPERATION_ID,
    )
    _execute(
        env,
        _request(
            env,
            pre_move_evidence=evidence,
            approval_artifact=_build_artifact(env, evidence, move_context),
            move_context=move_context,
            execute=True,
        ),
    )
    order: list[str] = []
    original_lock = sfm_module._governed_move_lock
    original_read = sfm_module.read_journal_exact

    @contextmanager
    def _tracking_lock(persist_dir: str):
        order.append("lock")
        with original_lock(persist_dir):
            yield

    def _tracking_read(persist_dir, operation_id):
        order.append("read")
        return original_read(persist_dir, operation_id)

    with mock.patch.object(sfm_module, "_governed_move_lock", _tracking_lock):
        with mock.patch.object(sfm_module, "read_journal_exact", side_effect=_tracking_read):
            result = _recover(env)

    assert result.verified is True
    assert order == ["lock", "read"]


def test_recovery_missing_journal_does_not_create_directory_or_lock(env) -> None:
    missing_id = "missing-journal-op-404"
    persist = env["persist"]
    assert not (persist / JOURNAL_DIR_NAME).exists()
    assert not (persist / sfm_module.MOVE_LOCK_NAME).exists()

    result = recover_single_file_move(
        persist_dir=str(persist.resolve()),
        operation_id=missing_id,
        library_root=str(env["library"].resolve()),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
    )

    assert result.success is False
    assert result.recovery_required is True
    assert "not found" in (result.error_message or "").lower()
    assert not (persist / JOURNAL_DIR_NAME).exists()
    assert not (persist / sfm_module.MOVE_LOCK_NAME).exists()


def test_invalid_operation_id_fails_before_journal_read_or_lock(env) -> None:
    with mock.patch.object(sfm_module, "read_journal_exact") as read_mock:
        with mock.patch.object(sfm_module, "_governed_move_lock") as lock_mock:
            result = recover_single_file_move(
                persist_dir=str(env["persist"].resolve()),
                operation_id="../escape",
                library_root=str(env["library"].resolve()),
                registry_db=str(env["registry"]),
                tracker_path=str(env["tracker"]),
            )
    assert result.success is False
    assert result.recovery_required is True
    read_mock.assert_not_called()
    lock_mock.assert_not_called()

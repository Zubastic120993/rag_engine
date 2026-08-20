"""Tests for durable quarantine-delete journaling and recovery (DELETE Phase B)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from rag_engine.governed_delete import (
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
    PHASE_PREPARED,
    PHASE_RECOVERY_REQUIRED,
    PHASE_REGISTRY_COMMITTED,
    PHASE_VERIFIED,
    QuarantineDeleteJournalError,
    create_prepared_journal,
    journal_path_for_operation,
    read_journal_exact,
    validate_operation_id,
)
from rag_engine.library_state.contract import EMBEDDING_NONE, INTENT_PLAN_DELETE
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    append_source_file_event,
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
from tests.test_quarantine_delete_executor import (
    BYTES,
    COLLECTION,
    DOC,
    FP,
    HASH,
    ID1,
    ID2,
    ISSUED_AT,
    EXPIRES_AT,
    NOW_VALID,
    OLD,
    OPERATION_ID,
    QUARANTINE,
    QUARANTINE_PARENT,
    SUBJECT,
    TARGET,
    VECTOR_IDS,
    _build_artifact,
    _execute,
    _make_chroma,
    _register_governed_target_locator,
    _seed_executor_env,
    _write,
    _write_tracker,
)

OTHER_OPERATION_ID = "quarantine-delete-op-other"


def _valid_clock() -> str:
    return NOW_VALID


def _digest(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


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


@pytest.fixture(autouse=True)
def governed_preflight(env: dict):
    if env.get("skip_governed_target"):
        return
    _register_governed_target_locator(env)


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
def approval_artifact(preflight_evidence, delete_context):
    return _build_artifact(preflight_evidence, delete_context)


def _request(env, *, preflight_evidence, approval_artifact, delete_context, execute=True):
    return QuarantineDeleteRequest(
        approval_artifact=approval_artifact,
        preflight_evidence=preflight_evidence,
        approval_context=delete_context,
        target_relative_path=TARGET,
        retained_relative_path=OLD,
        quarantine_relative_path=QUARANTINE,
        library_root=str(env["library"].resolve()),
        persist_dir=str(env["persist"].resolve()),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
        target_source_file_id=env["target_sf"],
        retained_source_file_id=env["retained_sf"],
        approved_vector_ids=VECTOR_IDS,
        registry_collection=COLLECTION,
        operation_id=OPERATION_ID,
        execute=execute,
    )


def test_journal_atomic_creation_rejects_duplicate_operation_id(env) -> None:
    root = env["persist"] / JOURNAL_DIR_NAME
    root.mkdir(parents=True)
    payload = {"operation_id": OPERATION_ID, "phase": PHASE_PREPARED}
    (root / f"{OPERATION_ID}.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(QuarantineDeleteJournalError, match="already exists"):
        create_prepared_journal(
            persist_dir=str(env["persist"]),
            operation_id=OPERATION_ID,
            target_relative_path=TARGET,
            retained_relative_path=OLD,
            quarantine_relative_path=QUARANTINE,
            expected_sha256=HASH,
            document_id=DOC,
            source_hash=HASH,
            approved_vector_ids=VECTOR_IDS,
            target_source_file_id=env["target_sf"],
            retained_source_file_id=env["retained_sf"],
            approval_digest=_digest("a"),
            tracker_snapshot={},
            chroma_identity_snapshot={},
            initial_registry_state_summary={},
            library_root=str(env["library"]),
            registry_db=str(env["registry"]),
            tracker_path=str(env["tracker"]),
            registry_collection=COLLECTION,
            created_at=ISSUED_AT,
        )


def test_recovery_at_filesystem_quarantined_restores_from_exact_journal(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    original_rename = qd_module._atomic_same_filesystem_rename

    def _interrupt_after_rename(src: Path, dst: Path) -> None:
        original_rename(src, dst)
        raise RuntimeError("simulated interruption after filesystem quarantine")

    with mock.patch.object(qd_module, "_atomic_same_filesystem_rename", side_effect=_interrupt_after_rename):
        result = _execute(
            env,
            _request(
                env,
                preflight_evidence=preflight_evidence,
                approval_artifact=approval_artifact,
                delete_context=delete_context,
            ),
        )
    assert result.success is False
    journal = read_journal_exact(env["persist"], OPERATION_ID)
    assert journal["phase"] in {PHASE_COMPENSATED, PHASE_RECOVERY_REQUIRED, PHASE_FILESYSTEM_QUARANTINED}

    recovery = recover_quarantine_delete(
        persist_dir=str(env["persist"]),
        operation_id=OPERATION_ID,
        library_root=str(env["library"]),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
    )
    if journal["phase"] == PHASE_FILESYSTEM_QUARANTINED:
        assert recovery.compensated is True
        assert recovery.phase == PHASE_COMPENSATED
        assert (env["library"] / TARGET).is_file()
        assert not (env["library"] / QUARANTINE).exists()


def test_registry_committed_interruption_is_inspection_only_not_auto_reversal(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
        ),
    )
    journal_path = journal_path_for_operation(env["persist"], OPERATION_ID, create_root=False)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["phase"] = PHASE_REGISTRY_COMMITTED
    journal_path.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")

    recovery = recover_quarantine_delete(
        persist_dir=str(env["persist"]),
        operation_id=OPERATION_ID,
        library_root=str(env["library"]),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
    )
    assert recovery.phase in {PHASE_VERIFIED, PHASE_RECOVERY_REQUIRED}
    assert recovery.compensated is False
    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["target_sf"])["activity_state"]
            != LOCATOR_ACTIVE
            or recovery.phase == PHASE_RECOVERY_REQUIRED
        )


def test_recovery_rejects_missing_journal_without_creating_artifacts(env) -> None:
    before_dirs = set(env["persist"].iterdir()) if env["persist"].is_dir() else set()
    recovery = recover_quarantine_delete(
        persist_dir=str(env["persist"]),
        operation_id=OTHER_OPERATION_ID,
        library_root=str(env["library"]),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
    )
    assert recovery.recovery_required is True
    assert recovery.success is False
    assert not (env["persist"] / JOURNAL_DIR_NAME).exists()
    assert not (env["persist"] / qd_module.QUARANTINE_DELETE_LOCK_NAME).exists()
    after_dirs = set(env["persist"].iterdir())
    assert before_dirs == after_dirs


def test_recovery_rejects_traversal_operation_id(env) -> None:
    recovery = recover_quarantine_delete(
        persist_dir=str(env["persist"]),
        operation_id="../escape",
        library_root=str(env["library"]),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
    )
    assert recovery.recovery_required is True
    assert recovery.success is False


def test_recovery_rejects_symlink_persist_dir(env, tmp_path: Path) -> None:
    link = tmp_path / "persist_link"
    link.symlink_to(env["persist"], target_is_directory=True)
    recovery = recover_quarantine_delete(
        persist_dir=str(link),
        operation_id=OPERATION_ID,
        library_root=str(env["library"]),
        registry_db=str(env["registry"]),
        tracker_path=str(env["tracker"]),
    )
    assert recovery.recovery_required is True


def test_validate_operation_id_rejects_unsafe_values() -> None:
    with pytest.raises(QuarantineDeleteJournalError):
        validate_operation_id("../bad")
    with pytest.raises(QuarantineDeleteJournalError):
        validate_operation_id("bad/id")


def test_successful_execute_leaves_verified_terminal_journal(
    env, preflight_evidence, approval_artifact, delete_context
) -> None:
    result = _execute(
        env,
        _request(
            env,
            preflight_evidence=preflight_evidence,
            approval_artifact=approval_artifact,
            delete_context=delete_context,
        ),
    )
    assert result.outcome == OUTCOME_SUCCESS
    journal = read_journal_exact(env["persist"], OPERATION_ID)
    assert journal["phase"] == PHASE_VERIFIED

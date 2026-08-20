"""Tests for caller-owned atomic registry quarantine-delete transition (DELETE Phase B)."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from rag_engine.governed_delete.quarantine_approval import (
    QuarantineDeleteApprovalValidationResult,
)
from rag_engine.governed_delete.quarantine_preflight import QuarantineDeleteEvidence
from rag_engine.library_state.contract import (
    APPROVAL_REQUIRED,
    CLASS_EXACT_DUPLICATE,
    EMBEDDING_NONE,
    INTENT_PLAN_DELETE,
    OP_RETIREMENT_PROPOSAL,
    RESULT_VERIFIED,
)
from rag_engine.library_state.plan import OperationPlan, TargetClassification
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    LOCATOR_INACTIVE,
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
    append_source_file_event,
    get_locator_lifecycle_state,
    initialize_locator_lifecycle_state,
    initialize_registry,
    make_source_file_id,
    migrate_connection,
    open_registry,
    prepare_quarantine_registry_transition,
    record_compensation_request,
    record_terminal_compensation_outcome,
    register_document_version,
    register_source_file,
    register_subject,
    registry_transaction,
)
from rag_engine.metadata_registry.quarantine_transaction import (
    QuarantineRegistryTransitionError,
    validate_quarantine_registry_preconditions,
)
from rag_engine.stable_identity import (
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
)

OLD = "00_Career/03_Engine_Knowledge/MAN_Academy/MAN_ME-C_LGIP_old_name.pdf"
TARGET = "00_Career/03_Engine_Knowledge/MAN_Academy/copy/MAN_ME-C_LGIP_new_name.pdf"
QUARANTINE = "_Quarantine/pending/MAN_ME-C_LGIP_duplicate.pdf"

BYTES = b"%PDF-1.4\nquarantine registry transition fixture\n"
DOC = document_id_from_bytes(BYTES)
HASH = source_hash_from_bytes(BYTES)
SUBJECT = subject_id_from_key("maker_doc", "quarantine-registry-txn")
COLLECTION = "maker-manuals"
OPERATION_ID = "quarantine-registry-txn-op-001"
APPROVAL_DIGEST = "a" * 64
REQUEST_ID = "req-" + ("q" * 60)
PLAN_DIGEST = "b" * 64
TS = "2026-08-19T12:00:00Z"


def _minimal_plan(*, request_id: str = REQUEST_ID) -> OperationPlan:
    classification = TargetClassification(
        target=TARGET,
        classification=CLASS_EXACT_DUPLICATE,
        confidence="high",
        proposed_operation=OP_RETIREMENT_PROPOSAL,
        approval=APPROVAL_REQUIRED,
        embedding_action=EMBEDDING_NONE,
        result=RESULT_VERIFIED,
        expected_new_vectors=0,
        document_id=DOC,
        source_hash=HASH,
    )
    return OperationPlan(
        request_id=request_id,
        intent=INTENT_PLAN_DELETE,
        targets=(TARGET,),
        classification=CLASS_EXACT_DUPLICATE,
        authority_snapshot={"generation": {"state": "KNOWN_COMPATIBLE"}},
        proposed_operation=OP_RETIREMENT_PROPOSAL,
        approval=APPROVAL_REQUIRED,
        embedding_action=EMBEDDING_NONE,
        affected_stores=("registry",),
        risk_flags=(),
        ambiguity_flags=(),
        verification_contract={},
        evidence_summary="test fixture",
        result=RESULT_VERIFIED,
        classifications=(classification,),
        evidence_gaps=(),
    )


def _preflight_evidence(registry_db: Path) -> QuarantineDeleteEvidence:
    lib = registry_db.parent / "lib"
    persist = registry_db.parent / "persist"
    lib.mkdir(parents=True, exist_ok=True)
    persist.mkdir(parents=True, exist_ok=True)
    return QuarantineDeleteEvidence(
        target_path=TARGET,
        retained_path=OLD,
        quarantine_path=QUARANTINE,
        target_file_sha256=HASH,
        document_id=DOC,
        source_hash=HASH,
        plan=_minimal_plan(),
        request_id=REQUEST_ID,
        plan_digest=PLAN_DIGEST,
        approved_vector_ids=(),
        retained_aliases=(OLD,),
        resolver_classification=CLASS_EXACT_DUPLICATE,
        proposed_operation=OP_RETIREMENT_PROPOSAL,
        compatibility_state="KNOWN_COMPATIBLE",
        library_root=str(lib.resolve()),
        persist_dir=str(persist.resolve()),
        registry_db=str(registry_db.resolve()),
        tracker_path=str((persist / "embedded.json").resolve()),
    )


def _approval(*, digest: str = APPROVAL_DIGEST) -> QuarantineDeleteApprovalValidationResult:
    return QuarantineDeleteApprovalValidationResult(
        approval_id="quarantine-approval-txn-001",
        request_id=REQUEST_ID,
        plan_digest=PLAN_DIGEST,
        approval_digest=digest,
        target_path=TARGET,
        retained_path=OLD,
        quarantine_path=QUARANTINE,
        document_id=DOC,
        source_hash=HASH,
        resolver_classification=CLASS_EXACT_DUPLICATE,
        proposed_operation=OP_RETIREMENT_PROPOSAL,
    )


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@pytest.fixture()
def registry_db(tmp_path: Path) -> Path:
    db = (tmp_path / "registry" / "quarantine_registry_txn.sqlite3").resolve()
    initialize_registry(db)
    return db


def _seed_retained_locator(conn) -> str:
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
    initialize_locator_lifecycle_state(
        conn,
        source_file_id=retained_sf,
        document_id=DOC,
        source="test_fixture",
    )
    conn.commit()
    return retained_sf


def _seed_locators(
    conn,
    *,
    target_document_id: str = DOC,
    target_path: str = TARGET,
    target_alias: bool = True,
    target_lifecycle: bool = True,
    target_active: bool = True,
    include_target_row: bool = True,
) -> tuple[str, str]:
    retained_sf = _seed_retained_locator(conn)
    target_sf = make_source_file_id(document_id=target_document_id, relative_path=target_path)
    if not include_target_row:
        return retained_sf, target_sf

    if target_document_id != DOC:
        with registry_transaction(conn):
            register_document_version(
                conn,
                document_id=target_document_id,
                subject_id=SUBJECT,
                source_hash=HASH,
            )

    with registry_transaction(conn):
        register_source_file(
            conn,
            document_id=target_document_id,
            relative_path=target_path,
            source_hash=HASH,
            collection=COLLECTION,
            source_file_id=target_sf,
        )
        if target_alias:
            append_source_file_event(
                conn,
                source_file_id=target_sf,
                document_id=target_document_id,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=_digest("target-alias"),
            )
    if target_lifecycle:
        initialize_locator_lifecycle_state(
            conn,
            source_file_id=target_sf,
            document_id=target_document_id,
            source="test_fixture",
        )
    if target_lifecycle and not target_active:
        with registry_transaction(conn):
            request = record_compensation_request(
                conn,
                source_file_id=target_sf,
                document_id=target_document_id,
                registration_v4_event_id=resolve_alias_event_id(conn, target_sf),
                compensation_approval_digest=_digest("inactive-target"),
                operation_id="prior-inactivate",
            )
            record_terminal_compensation_outcome(
                conn,
                source_file_id=target_sf,
                document_id=target_document_id,
                compensation_request_v4_event_id=int(request["v4_event"]["event_id"]),
                outcome="COMPLETED",
                operation_id="prior-inactivate",
            )
    conn.commit()
    return retained_sf, target_sf


def resolve_alias_event_id(conn, source_file_id: str) -> int:
    row = conn.execute(
        "SELECT event_id FROM source_file_events "
        "WHERE source_file_id = ? AND event_type = ?",
        (source_file_id, SOURCE_FILE_EVENT_ALIAS_REGISTERED),
    ).fetchone()
    assert row is not None
    return int(row["event_id"])


def _validate(conn, registry_db: Path, retained_sf: str, target_sf: str, **overrides):
    params = {
        "approval": _approval(),
        "preflight_evidence": _preflight_evidence(registry_db),
        "target_source_file_id": target_sf,
        "retained_source_file_id": retained_sf,
        "registry_collection": COLLECTION,
        "operation_id": OPERATION_ID,
    }
    params.update(overrides)
    validate_quarantine_registry_preconditions(conn, **params)


def _prepare(conn, registry_db: Path, retained_sf: str, target_sf: str, **overrides):
    params = {
        "approval": _approval(),
        "preflight_evidence": _preflight_evidence(registry_db),
        "target_source_file_id": target_sf,
        "retained_source_file_id": retained_sf,
        "registry_collection": COLLECTION,
        "operation_id": OPERATION_ID,
        "actor": "test-operator",
        "created_at": TS,
    }
    params.update(overrides)
    return prepare_quarantine_registry_transition(conn, **params)


def _counts(conn) -> dict[str, int]:
    return {
        "source_files": conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"],
        "v4_events": conn.execute("SELECT COUNT(*) AS c FROM source_file_events").fetchone()["c"],
        "v5_state": conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_state"
        ).fetchone()["c"],
        "approval_digest": conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"],
    }


def test_read_only_preflight_missing_target_locator_fails_without_transaction(
    registry_db: Path,
) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        retained_sf, target_sf = _seed_locators(conn, include_target_row=False)
        before = _counts(conn)
        with pytest.raises(
            QuarantineRegistryTransitionError,
            match="is not registered",
        ):
            _validate(conn, registry_db, retained_sf, target_sf)
        assert _counts(conn) == before


def test_read_only_preflight_succeeds_for_healthy_locators(
    registry_db: Path,
) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        retained_sf, target_sf = _seed_locators(conn)
        before = _counts(conn)
        _validate(conn, registry_db, retained_sf, target_sf)
        assert _counts(conn) == before


def test_quarantine_transaction_module_does_not_import_register_source_file() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "rag_engine/metadata_registry/quarantine_transaction.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "register_source_file" not in imported_names


def test_successful_transaction_uses_existing_target_and_retained_locators(
    registry_db: Path,
) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        retained_sf, target_sf = _seed_locators(conn)
        with registry_transaction(conn):
            result = _prepare(conn, registry_db, retained_sf, target_sf)
        conn.commit()

        assert result.target_source_file_id == target_sf
        assert result.retained_source_file_id == retained_sf
        assert result.target_final_activity_state == LOCATOR_INACTIVE
        assert result.retained_final_activity_state == LOCATOR_ACTIVE
        assert result.approval_digest == APPROVAL_DIGEST

        v4 = conn.execute(
            "SELECT * FROM source_file_events WHERE event_id = ?",
            (result.compensation_request_v4_event_id,),
        ).fetchone()
        assert v4["event_type"] == SOURCE_FILE_EVENT_COMPENSATION_REQUESTED
        assert v4["approval_digest"] == APPROVAL_DIGEST


def test_missing_target_locator_fails_before_writes(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        retained_sf, target_sf = _seed_locators(conn, include_target_row=False)
        before = _counts(conn)
        with registry_transaction(conn):
            with pytest.raises(
                QuarantineRegistryTransitionError,
                match="is not registered",
            ):
                _prepare(conn, registry_db, retained_sf, target_sf)
        after = _counts(conn)
        assert after == before


def test_target_wrong_document_id_fails_before_writes(registry_db: Path) -> None:
    other_bytes = b"other document bytes for mismatch"
    other_doc = document_id_from_bytes(other_bytes)
    other_hash = source_hash_from_bytes(other_bytes)
    other_subject = subject_id_from_key("maker_doc", "quarantine-registry-other")
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        retained_sf, target_sf = _seed_locators(conn)
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
                (other_doc, target_sf),
            )
        conn.commit()
        before = _counts(conn)
        with registry_transaction(conn):
            with pytest.raises(
                QuarantineRegistryTransitionError,
                match="not bound to the supplied document_id",
            ):
                _prepare(conn, registry_db, retained_sf, target_sf)
        assert _counts(conn) == before


def test_target_wrong_relative_path_fails_before_writes(registry_db: Path) -> None:
    wrong_path = "00_Career/03_Engine_Knowledge/MAN_Academy/wrong_target.pdf"
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        retained_sf, target_sf = _seed_locators(conn)
        conn.execute(
            "UPDATE source_files SET relative_path = ? WHERE source_file_id = ?",
            (wrong_path, target_sf),
        )
        conn.commit()
        before = _counts(conn)
        with registry_transaction(conn):
            with pytest.raises(
                QuarantineRegistryTransitionError,
                match="does not match approval target_path",
            ):
                _prepare(conn, registry_db, retained_sf, target_sf)
        assert _counts(conn) == before


def test_target_without_v5_state_fails_before_writes(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        retained_sf, target_sf = _seed_locators(conn, target_lifecycle=False)
        before = _counts(conn)
        with registry_transaction(conn):
            with pytest.raises(
                QuarantineRegistryTransitionError,
                match="lifecycle projection",
            ):
                _prepare(conn, registry_db, retained_sf, target_sf)
        assert _counts(conn) == before


def test_target_not_active_fails_before_writes(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        retained_sf, target_sf = _seed_locators(conn, target_active=False)
        before = _counts(conn)
        with registry_transaction(conn):
            with pytest.raises(
                QuarantineRegistryTransitionError,
                match="activity_state ACTIVE",
            ):
                _prepare(conn, registry_db, retained_sf, target_sf)
        assert _counts(conn) == before


def test_target_without_alias_registration_v4_fails_before_writes(
    registry_db: Path,
) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        retained_sf, target_sf = _seed_locators(conn, target_alias=False)
        before = _counts(conn)
        with registry_transaction(conn):
            with pytest.raises(
                QuarantineRegistryTransitionError,
                match="SOURCE_FILE_ALIAS_REGISTERED",
            ):
                _prepare(conn, registry_db, retained_sf, target_sf)
        assert _counts(conn) == before

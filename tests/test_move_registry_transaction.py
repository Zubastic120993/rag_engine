"""Tests for caller-owned atomic registry MOVE transition (Phase E2)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from rag_engine.library_state.contract import (
    APPROVAL_REQUIRED,
    CLASS_INDEXED_OK,
    EMBEDDING_NONE,
    INTENT_PLAN_MOVE,
    OP_NO_OP,
    RESULT_VERIFIED,
)
from rag_engine.library_state.move_approval import MoveApprovalValidationResult
from rag_engine.library_state.move_preflight import PreMoveEvidence
from rag_engine.library_state.plan import OperationPlan, TargetClassification
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    LOCATOR_EVENT_INITIALIZED,
    LOCATOR_EVENT_MOVED,
    LOCATOR_INACTIVE,
    MOVE_DESTINATION_PROVISIONING_SOURCE,
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    SOURCE_FILE_EVENT_REGISTERED,
    append_source_file_event,
    get_locator_lifecycle_state,
    initialize_locator_lifecycle_state,
    initialize_registry,
    migrate_connection,
    open_registry,
    prepare_move_registry_transition,
    record_locator_move_transition,
    register_document_version,
    register_source_file,
    register_subject,
    registry_transaction,
)
from rag_engine.metadata_registry.move_transaction import MoveRegistryTransitionError
from rag_engine.stable_identity import (
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
)

OLD = "manuals/MAN_old_name.pdf"
NEW = "manuals/MAN_new_name.pdf"
OTHER = "manuals/other_doc.pdf"

BYTES = b"%PDF-1.4\nmove registry transition fixture\n"
DOC = document_id_from_bytes(BYTES)
HASH = source_hash_from_bytes(BYTES)
SUBJECT = subject_id_from_key("maker_doc", "move-registry-txn")
COLLECTION = "maker-manuals"
OPERATION_ID = "move-registry-txn-op-001"
APPROVAL_DIGEST = "c" * 64
REQUEST_ID = "req-" + ("d" * 60)
PLAN_DIGEST = "e" * 64
TS = "2026-08-19T12:00:00Z"


def _minimal_plan(*, request_id: str = REQUEST_ID) -> OperationPlan:
    classification = TargetClassification(
        target=OLD,
        classification=CLASS_INDEXED_OK,
        confidence="high",
        proposed_operation=OP_NO_OP,
        approval=APPROVAL_REQUIRED,
        embedding_action=EMBEDDING_NONE,
        result=RESULT_VERIFIED,
        expected_new_vectors=0,
        document_id=DOC,
        source_hash=HASH,
    )
    return OperationPlan(
        request_id=request_id,
        intent=INTENT_PLAN_MOVE,
        targets=(OLD,),
        classification=CLASS_INDEXED_OK,
        authority_snapshot={"generation": {"state": "KNOWN_COMPATIBLE"}},
        proposed_operation=OP_NO_OP,
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


def _pre_move_evidence(
    registry_db: Path,
    *,
    source: str = OLD,
    destination: str = NEW,
    request_id: str = REQUEST_ID,
    plan_digest: str = PLAN_DIGEST,
    registry_path: str | None = None,
) -> PreMoveEvidence:
    lib = registry_db.parent / "lib"
    persist = registry_db.parent / "persist"
    return PreMoveEvidence(
        source_path=source,
        destination_path=destination,
        source_file_sha256=HASH,
        document_id=DOC,
        source_hash=HASH,
        plan=_minimal_plan(request_id=request_id),
        request_id=request_id,
        plan_digest=plan_digest,
        approved_vector_ids=(),
        resolver_classification=CLASS_INDEXED_OK,
        proposed_operation=OP_NO_OP,
        compatibility_state="KNOWN_COMPATIBLE",
        library_root=str(lib.resolve()),
        persist_dir=str(persist.resolve()),
        registry_db=str(registry_path or registry_db.resolve()),
        tracker_path=None,
    )


def _approval(
    *,
    destination: str = NEW,
    digest: str = APPROVAL_DIGEST,
    request_id: str = REQUEST_ID,
    plan_digest: str = PLAN_DIGEST,
) -> MoveApprovalValidationResult:
    return MoveApprovalValidationResult(
        approval_id="move-approval-txn-001",
        request_id=request_id,
        plan_digest=plan_digest,
        approval_digest=digest,
        source_path=OLD,
        destination_path=destination,
        document_id=DOC,
        source_hash=HASH,
        resolver_classification=CLASS_INDEXED_OK,
        proposed_operation=OP_NO_OP,
    )


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_paths(*paths: Path) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for path in paths:
        key = str(path)
        if path.is_file():
            out[key] = _file_sha(path)
        elif path.is_dir():
            out[key] = json.dumps(sorted(p.name for p in path.iterdir()))
        else:
            out[key] = None
    return out


@pytest.fixture()
def registry_db(tmp_path: Path) -> Path:
    db = (tmp_path / "registry" / "move_registry_txn.sqlite3").resolve()
    initialize_registry(db)
    return db


@pytest.fixture()
def side_paths(tmp_path: Path) -> dict[str, Path]:
    library = tmp_path / "lib"
    persist = tmp_path / "persist"
    library.mkdir()
    persist.mkdir()
    return {
        "library": library,
        "persist": persist,
        "tracker": persist / "embedded.json",
        "chroma": persist / "chroma.sqlite3",
        "journal": persist / "journals" / "move-op.json",
        "lock": persist / "ingest.lock",
    }


def _seed_document(conn: sqlite3.Connection) -> str:
    old_sf = ""
    with registry_transaction(conn):
        register_subject(conn, subject_id=SUBJECT)
        register_document_version(
            conn,
            document_id=DOC,
            subject_id=SUBJECT,
            source_hash=HASH,
        )
        old_sf = register_source_file(
            conn,
            document_id=DOC,
            relative_path=OLD,
            source_hash=HASH,
            collection=COLLECTION,
        )["source_file_id"]
        initialize_locator_lifecycle_state(
            conn,
            source_file_id=old_sf,
            document_id=DOC,
            source="test_fixture",
            reason="old locator seed",
        )
    return old_sf


def _prepare(
    conn: sqlite3.Connection,
    registry_db: Path,
    old_sf: str,
    **overrides,
):
    params = {
        "approval": _approval(),
        "pre_move_evidence": _pre_move_evidence(registry_db),
        "old_source_file_id": old_sf,
        "registry_collection": COLLECTION,
        "operation_id": OPERATION_ID,
        "actor": "test-operator",
        "created_at": TS,
    }
    params.update(overrides)
    return prepare_move_registry_transition(conn, **params)


def test_successful_transaction_creates_exact_registry_artifacts(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        with registry_transaction(conn):
            result = _prepare(conn, registry_db, old_sf)
        conn.commit()

        new_sf = result.new_source_file_id
        assert result.old_source_file_id == old_sf
        assert result.operation_id == OPERATION_ID
        assert result.affected_source_file_ids == (old_sf, new_sf)
        assert result.old_final_activity_state == LOCATOR_INACTIVE
        assert result.new_final_activity_state == LOCATOR_ACTIVE

        assert result.destination_source_file["relative_path"] == NEW
        assert result.destination_source_file["status"] is None
        assert result.destination_v4_event_id > 0

        v4 = conn.execute(
            "SELECT * FROM source_file_events WHERE event_id = ?",
            (result.destination_v4_event_id,),
        ).fetchone()
        assert v4 is not None
        assert v4["event_type"] == SOURCE_FILE_EVENT_ALIAS_REGISTERED
        assert v4["approval_digest"] == APPROVAL_DIGEST
        assert v4["operation_id"] == OPERATION_ID

        init = conn.execute(
            "SELECT * FROM source_file_locator_lifecycle_events "
            "WHERE event_id = ?",
            (result.destination_v5_init_event["event_id"],),
        ).fetchone()
        assert init["event_type"] == LOCATOR_EVENT_INITIALIZED
        assert init["related_v4_event_id"] == result.destination_v4_event_id
        assert init["source"] == MOVE_DESTINATION_PROVISIONING_SOURCE

        move_events = conn.execute(
            "SELECT * FROM source_file_locator_lifecycle_events "
            "WHERE operation_id = ? AND event_type = ?",
            (OPERATION_ID, LOCATOR_EVENT_MOVED),
        ).fetchall()
        assert len(move_events) == 2
        old_move = next(e for e in move_events if e["source_file_id"] == old_sf)
        new_move = next(e for e in move_events if e["source_file_id"] == new_sf)
        assert int(old_move["event_id"]) == result.old_move_event_id
        assert int(new_move["event_id"]) == result.new_move_event_id
        assert old_move["related_v4_event_id"] is None
        assert new_move["related_v4_event_id"] == result.destination_v4_event_id
        assert old_move["related_event_id"] == new_move["event_id"]

        sf_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"]
        v4_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"]
        init_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE source_file_id = ? AND event_type = ?",
            (new_sf, LOCATOR_EVENT_INITIALIZED),
        ).fetchone()["c"]
        assert sf_count == 1
        assert v4_count == 1
        assert init_count == 1


def test_outside_registry_transaction_fails_before_writes(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        before_sf = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        before_v4 = conn.execute("SELECT COUNT(*) AS c FROM source_file_events").fetchone()["c"]
        with pytest.raises(
            MoveRegistryTransitionError,
            match="requires an active registry_transaction",
        ):
            _prepare(conn, registry_db, old_sf)
        after_sf = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        after_v4 = conn.execute("SELECT COUNT(*) AS c FROM source_file_events").fetchone()["c"]
        assert after_sf == before_sf
        assert after_v4 == before_v4


def test_old_locator_not_active_fails_before_provisioning(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        with registry_transaction(conn):
            temp_sf = register_source_file(
                conn,
                document_id=DOC,
                relative_path="manuals/temp_dest.pdf",
                source_hash=HASH,
                collection=COLLECTION,
            )["source_file_id"]
            initialize_locator_lifecycle_state(
                conn,
                source_file_id=temp_sf,
                document_id=DOC,
                source="test_fixture",
            )
            record_locator_move_transition(
                conn,
                document_id=DOC,
                old_source_file_id=old_sf,
                new_source_file_id=temp_sf,
                operation_id="prior-move",
            )
        conn.commit()
        before_sf = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        before_v4 = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"]
        with pytest.raises(MoveRegistryTransitionError, match="activity_state ACTIVE"):
            with registry_transaction(conn):
                _prepare(conn, registry_db, old_sf)
        after_sf = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        after_v4 = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"]
        assert after_sf == before_sf
        assert after_v4 == before_v4


def test_approval_pre_move_binding_mismatch_fails_before_writes(
    registry_db: Path,
) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        cases = [
            (
                "source_path",
                {"pre_move_evidence": _pre_move_evidence(registry_db, source=OTHER)},
            ),
            (
                "destination_path",
                {"pre_move_evidence": _pre_move_evidence(registry_db, destination=OTHER)},
            ),
            (
                "request_id",
                {
                    "pre_move_evidence": _pre_move_evidence(
                        registry_db, request_id="req-" + ("z" * 60)
                    )
                },
            ),
            (
                "plan_digest",
                {
                    "pre_move_evidence": _pre_move_evidence(
                        registry_db, plan_digest="f" * 64
                    )
                },
            ),
        ]
        for field, override in cases:
            with pytest.raises(MoveRegistryTransitionError, match=field):
                with registry_transaction(conn):
                    _prepare(conn, registry_db, old_sf, **override)
        with pytest.raises(MoveRegistryTransitionError, match="document_id"):
            with registry_transaction(conn):
                bad_evidence = _pre_move_evidence(registry_db)
                bad_approval = MoveApprovalValidationResult(
                    approval_id="bad",
                    request_id=bad_evidence.request_id,
                    plan_digest=bad_evidence.plan_digest,
                    approval_digest=APPROVAL_DIGEST,
                    source_path=bad_evidence.source_path,
                    destination_path=bad_evidence.destination_path,
                    document_id="docrev:" + ("y" * 58),
                    source_hash=HASH,
                    resolver_classification=CLASS_INDEXED_OK,
                    proposed_operation=OP_NO_OP,
                )
                prepare_move_registry_transition(
                    conn,
                    approval=bad_approval,
                    pre_move_evidence=bad_evidence,
                    old_source_file_id=old_sf,
                    registry_collection=COLLECTION,
                    operation_id=OPERATION_ID,
                )
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0


def test_existing_destination_path_fails_without_idempotent_success(
    registry_db: Path,
) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        with registry_transaction(conn):
            register_source_file(
                conn,
                document_id=DOC,
                relative_path=NEW,
                source_hash=HASH,
                collection=COLLECTION,
            )
        with pytest.raises(MoveRegistryTransitionError, match="already exists"):
            with registry_transaction(conn):
                _prepare(conn, registry_db, old_sf)
        v4_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"]
        assert v4_count == 0


def test_reused_approval_digest_fails_without_partial_destination_rows(
    registry_db: Path,
) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        with registry_transaction(conn):
            append_source_file_event(
                conn,
                source_file_id=old_sf,
                document_id=DOC,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=APPROVAL_DIGEST,
                operation_id="prior-digest-use",
                created_at=TS,
            )
        conn.commit()
        before_sf = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        with pytest.raises(MoveRegistryTransitionError, match="approval_digest"):
            with registry_transaction(conn):
                _prepare(conn, registry_db, old_sf)
        after_sf = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        assert after_sf == before_sf
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE source_file_id != ?",
            (old_sf,),
        ).fetchone()["c"] == 0


def test_rollback_after_provisioning_failure_leaves_old_locator_unchanged(
    registry_db: Path,
) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        old_state_before = get_locator_lifecycle_state(conn, source_file_id=old_sf)
        with pytest.raises(RuntimeError, match="simulated move failure"):
            with registry_transaction(conn):
                with mock.patch(
                    "rag_engine.metadata_registry.move_transaction.record_locator_move_transition",
                    side_effect=RuntimeError("simulated move failure"),
                ):
                    _prepare(conn, registry_db, old_sf)
        old_state_after = get_locator_lifecycle_state(conn, source_file_id=old_sf)
        assert dict(old_state_before) == dict(old_state_after)
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"] == 0
        move_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE event_type = ? AND operation_id = ?",
            (LOCATOR_EVENT_MOVED, OPERATION_ID),
        ).fetchone()["c"]
        assert move_count == 0


def test_unrelated_registry_rows_remain_unchanged(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        with registry_transaction(conn):
            unrelated = register_source_file(
                conn,
                document_id=DOC,
                relative_path="manuals/unrelated.pdf",
                source_hash=HASH,
                collection=COLLECTION,
            )
            initialize_locator_lifecycle_state(
                conn,
                source_file_id=unrelated["source_file_id"],
                document_id=DOC,
                source="test_fixture",
            )
            append_source_file_event(
                conn,
                source_file_id=unrelated["source_file_id"],
                document_id=DOC,
                event_type=SOURCE_FILE_EVENT_REGISTERED,
                created_at=TS,
            )
        conn.commit()
        unrelated_before = conn.execute(
            "SELECT * FROM source_file_locator_state WHERE source_file_id = ?",
            (unrelated["source_file_id"],),
        ).fetchone()
        v4_before = conn.execute("SELECT COUNT(*) AS c FROM source_file_events").fetchone()["c"]
        with registry_transaction(conn):
            _prepare(conn, registry_db, old_sf)
        conn.commit()
        unrelated_after = conn.execute(
            "SELECT * FROM source_file_locator_state WHERE source_file_id = ?",
            (unrelated["source_file_id"],),
        ).fetchone()
        v4_after = conn.execute("SELECT COUNT(*) AS c FROM source_file_events").fetchone()["c"]
        assert dict(unrelated_before) == dict(unrelated_after)
        assert v4_after == v4_before + 1


def test_no_side_store_paths_touched(registry_db: Path, side_paths: dict) -> None:
    side_paths["tracker"].write_text("{}\n", encoding="utf-8")
    side_paths["chroma"].write_text("{}", encoding="utf-8")
    side_paths["journal"].parent.mkdir(parents=True, exist_ok=True)
    side_paths["journal"].write_text("{}\n", encoding="utf-8")
    side_paths["lock"].write_text("locked\n", encoding="utf-8")
    before = _snapshot_paths(
        side_paths["library"],
        side_paths["persist"],
        side_paths["tracker"],
        side_paths["chroma"],
        side_paths["journal"],
        side_paths["lock"],
    )
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        with registry_transaction(conn):
            _prepare(conn, registry_db, old_sf)
        conn.commit()
    after = _snapshot_paths(
        side_paths["library"],
        side_paths["persist"],
        side_paths["tracker"],
        side_paths["chroma"],
        side_paths["journal"],
        side_paths["lock"],
    )
    assert after == before


def test_registry_db_path_must_match_connection(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        wrong_evidence = _pre_move_evidence(
            registry_db,
            registry_path=str(registry_db.parent / "other.sqlite3"),
        )
        with pytest.raises(MoveRegistryTransitionError, match="registry_db"):
            with registry_transaction(conn):
                prepare_move_registry_transition(
                    conn,
                    approval=_approval(),
                    pre_move_evidence=wrong_evidence,
                    old_source_file_id=old_sf,
                    registry_collection=COLLECTION,
                    operation_id=OPERATION_ID,
                )

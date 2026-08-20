"""Tests for transaction-bound MOVE destination locator provisioning (Phase E1)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from rag_engine.library_state.move_approval import MoveApprovalValidationResult
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    LOCATOR_EVENT_INITIALIZED,
    MOVE_DESTINATION_PROVISIONING_SOURCE,
    RegistryIntegrityError,
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    get_locator_lifecycle_state,
    initialize_locator_lifecycle_state,
    initialize_registry,
    migrate_connection,
    open_registry,
    provision_move_destination_locator,
    register_document_version,
    register_source_file,
    register_subject,
    registry_transaction,
)
from rag_engine.metadata_registry.move_provisioning import (
    MoveDestinationProvisioningError,
)
from rag_engine.stable_identity import (
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
)

OLD = "manuals/MAN_old_name.pdf"
NEW = "manuals/MAN_new_name.pdf"
OTHER = "manuals/other_doc.pdf"

BYTES = b"%PDF-1.4\nmove destination provisioning fixture\n"
DOC = document_id_from_bytes(BYTES)
HASH = source_hash_from_bytes(BYTES)
SUBJECT = subject_id_from_key("maker_doc", "move-dest-prov")
COLLECTION = "maker-manuals"
OPERATION_ID = "move-provision-op-001"
APPROVAL_DIGEST = "a" * 64
TS = "2026-08-19T12:00:00Z"


def _approval(*, destination: str = NEW, digest: str = APPROVAL_DIGEST) -> MoveApprovalValidationResult:
    return MoveApprovalValidationResult(
        approval_id="move-approval-prov-001",
        request_id="req-" + ("b" * 60),
        plan_digest="p" * 64,
        approval_digest=digest,
        source_path=OLD,
        destination_path=destination,
        document_id=DOC,
        source_hash=HASH,
        resolver_classification="INDEXED_OK",
        proposed_operation="NO_OP",
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
    db = (tmp_path / "registry" / "move_provision.sqlite3").resolve()
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


def _seed_document(conn: sqlite3.Connection, *, with_old_locator: bool = True) -> str:
    with registry_transaction(conn):
        register_subject(conn, subject_id=SUBJECT)
        register_document_version(
            conn,
            document_id=DOC,
            subject_id=SUBJECT,
            source_hash=HASH,
        )
        if with_old_locator:
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
    return old_sf if with_old_locator else ""


def _provision(conn: sqlite3.Connection, **overrides):
    params = {
        "approval": _approval(),
        "document_id": DOC,
        "source_hash": HASH,
        "destination_relative_path": NEW,
        "registry_collection": COLLECTION,
        "operation_id": OPERATION_ID,
        "actor": "test-operator",
        "created_at": TS,
    }
    params.update(overrides)
    return provision_move_destination_locator(conn, **params)


def test_valid_in_transaction_provisioning_creates_exact_rows(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        old_sf = _seed_document(conn)
        with registry_transaction(conn):
            result = _provision(conn)
        conn.commit()

        assert result.idempotent is False
        assert result.source_file["relative_path"] == NEW
        assert result.source_file["document_id"] == DOC
        assert result.source_file["source_hash"] == HASH
        assert result.source_file["collection"] == COLLECTION
        assert result.source_file["status"] is None

        assert result.v4_event["event_type"] == SOURCE_FILE_EVENT_ALIAS_REGISTERED
        assert result.v4_event["approval_digest"] == APPROVAL_DIGEST
        assert result.v4_event["operation_id"] == OPERATION_ID
        assert result.v4_event["source_file_id"] == result.source_file["source_file_id"]
        assert result.v4_event["source"] == MOVE_DESTINATION_PROVISIONING_SOURCE

        assert result.v5_state["activity_state"] == LOCATOR_ACTIVE
        assert result.v5_lifecycle_event["event_type"] == LOCATOR_EVENT_INITIALIZED
        assert result.v5_lifecycle_event["related_v4_event_id"] == result.v4_event["event_id"]
        assert result.v5_lifecycle_event["source"] == MOVE_DESTINATION_PROVISIONING_SOURCE

        sf_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"]
        v4_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"]
        v5_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE source_file_id = ?",
            (result.source_file["source_file_id"],),
        ).fetchone()["c"]
        assert sf_count == 1
        assert v4_count == 1
        assert v5_count == 1

        old_state = get_locator_lifecycle_state(conn, source_file_id=old_sf)
        assert old_state is not None
        assert old_state["activity_state"] == LOCATOR_ACTIVE


def test_outside_registry_transaction_fails_before_writes(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        _seed_document(conn)
        before_sf = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        before_v4 = conn.execute("SELECT COUNT(*) AS c FROM source_file_events").fetchone()["c"]
        with pytest.raises(
            MoveDestinationProvisioningError,
            match="requires an active registry_transaction",
        ):
            _provision(conn)
        after_sf = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        after_v4 = conn.execute("SELECT COUNT(*) AS c FROM source_file_events").fetchone()["c"]
        assert after_sf == before_sf
        assert after_v4 == before_v4


def test_exception_inside_transaction_rolls_back_all_rows(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        _seed_document(conn)
        with pytest.raises(RuntimeError, match="simulated failure"):
            with registry_transaction(conn):
                _provision(conn)
                raise RuntimeError("simulated failure")
        sf_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"]
        v4_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"]
        v5_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events"
        ).fetchone()["c"]
        assert sf_count == 0
        assert v4_count == 0
        assert v5_count == 1


def test_approval_binding_mismatch_blocks_before_writes(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        _seed_document(conn)
        with registry_transaction(conn):
            with pytest.raises(MoveDestinationProvisioningError, match="document_id mismatch"):
                _provision(conn, document_id="docrev:" + ("x" * 58))
            with pytest.raises(MoveDestinationProvisioningError, match="source_hash mismatch"):
                _provision(conn, source_hash="0" * 64)
            with pytest.raises(MoveDestinationProvisioningError, match="destination_path mismatch"):
                _provision(conn, destination_relative_path=OTHER)
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_files WHERE relative_path = ?",
            (NEW,),
        ).fetchone()["c"] == 0


def test_existing_destination_for_same_document_blocks(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        _seed_document(conn)
        with registry_transaction(conn):
            register_source_file(
                conn,
                document_id=DOC,
                relative_path=NEW,
                source_hash=HASH,
                collection=COLLECTION,
            )
            with pytest.raises(
                MoveDestinationProvisioningError,
                match="already exists for document_id",
            ):
                _provision(conn)
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"] == 0


def test_destination_registered_to_other_document_blocks(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        _seed_document(conn)
        other = document_id_from_bytes(b"other-document-for-dest-block")
        other_subject = subject_id_from_key("maker_doc", "other-doc")
        with registry_transaction(conn):
            register_subject(conn, subject_id=other_subject)
            register_document_version(
                conn,
                document_id=other,
                subject_id=other_subject,
                source_hash=source_hash_from_bytes(b"other-document-for-dest-block"),
            )
            register_source_file(
                conn,
                document_id=other,
                relative_path=NEW,
                source_hash=source_hash_from_bytes(b"other-document-for-dest-block"),
            )
            with pytest.raises(
                MoveDestinationProvisioningError,
                match="another document",
            ):
                _provision(conn)
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest = ?",
            (APPROVAL_DIGEST,),
        ).fetchone()["c"] == 0


def test_reused_approval_digest_rejected_by_v4_replay_guard(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        _seed_document(conn)
        with registry_transaction(conn):
            _provision(conn)
        conn.commit()
        with registry_transaction(conn):
            with pytest.raises(RegistryIntegrityError, match="approval_digest"):
                _provision(
                    conn,
                    approval=_approval(destination="manuals/another_new.pdf"),
                    destination_relative_path="manuals/another_new.pdf",
                )


def test_invalid_destination_path_and_operation_id_block(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        _seed_document(conn)
        with registry_transaction(conn):
            with pytest.raises(MoveDestinationProvisioningError, match="traversal"):
                _provision(conn, destination_relative_path="../escape.pdf")
            with pytest.raises(MoveDestinationProvisioningError, match="absolute"):
                _provision(conn, destination_relative_path="/tmp/abs.pdf")
            with pytest.raises(MoveDestinationProvisioningError, match="operation_id"):
                _provision(conn, operation_id="../bad-op")
        assert conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"] == 1


def test_old_locator_and_unrelated_rows_remain_unchanged(registry_db: Path) -> None:
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
        conn.commit()
        old_before = conn.execute(
            "SELECT * FROM source_files WHERE source_file_id = ?",
            (old_sf,),
        ).fetchone()
        unrelated_before = conn.execute(
            "SELECT * FROM source_file_locator_state WHERE source_file_id = ?",
            (unrelated["source_file_id"],),
        ).fetchone()
        with registry_transaction(conn):
            _provision(conn)
        conn.commit()
        old_after = conn.execute(
            "SELECT * FROM source_files WHERE source_file_id = ?",
            (old_sf,),
        ).fetchone()
        unrelated_after = conn.execute(
            "SELECT * FROM source_file_locator_state WHERE source_file_id = ?",
            (unrelated["source_file_id"],),
        ).fetchone()
        assert dict(old_before) == dict(old_after)
        assert dict(unrelated_before) == dict(unrelated_after)


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
        _seed_document(conn)
        with registry_transaction(conn):
            _provision(conn)
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

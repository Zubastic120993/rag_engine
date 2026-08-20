"""Tests for bounded explicit-target MOVE metadata reconciliation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from unittest import mock

import pytest

from rag_engine.governed_reconciliation import (
    ExplicitMoveReconciliationError,
    ExplicitMoveReconciliationRequest,
    apply_explicit_move_reconciliation,
    preview_explicit_move_reconciliation,
)
from rag_engine.governed_reconciliation import explicit_move as em_module
from rag_engine.index_compatibility.constants import COMPAT_KNOWN_COMPATIBLE
from rag_engine.library_state.evidence import lookup_chroma_by_embedding_ids
from rag_engine.library_state.move_approval import MoveApprovalValidationResult
from rag_engine.metadata_registry import (
    LOCATOR_ACTIVE,
    LOCATOR_COMPENSATION_PENDING,
    LOCATOR_EVENT_MOVED,
    get_locator_lifecycle_state,
    initialize_locator_lifecycle_state,
    initialize_registry,
    migrate_connection,
    open_registry,
    record_locator_move_transition,
    register_document_version,
    register_source_file,
    register_subject,
    registry_transaction,
)
from rag_engine.stable_identity import (
    chunk_id,
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
)

OLD = "manuals/MAN_ME-C_LGIP_old_name.pdf"
NEW = "manuals/MAN_ME-C_LGIP_new_name.pdf"
THIRD = "manuals/other_alias.pdf"
OTHER = "manuals/unrelated.pdf"

BYTES = b"%PDF-1.4\nexplicit move fixture\n"
HASH = source_hash_from_bytes(BYTES)
DOC = document_id_from_bytes(BYTES)
SUBJECT = subject_id_from_key("maker_doc", "explicit-move")
FP = "cd" * 32
ID1 = chunk_id(DOC, FP, 0)
ID2 = chunk_id(DOC, FP, 1)
VECTOR_IDS = (ID1, ID2)


def _approval(*, source: str = OLD, destination: str = NEW) -> MoveApprovalValidationResult:
    return MoveApprovalValidationResult(
        approval_id="move-approval-test-001",
        request_id="req-" + ("a" * 60),
        plan_digest="p" * 64,
        approval_digest="d" * 64,
        source_path=source,
        destination_path=destination,
        document_id=DOC,
        source_hash=HASH,
        resolver_classification="INDEXED_OK",
        proposed_operation="NO_OP",
    )


def _make_chroma(
    path: Path,
    *,
    source: str,
    ids: tuple[str, ...] = VECTOR_IDS,
    document_id: str = DOC,
    source_hash: str = HASH,
) -> None:
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
        for i, eid in enumerate(ids, start=1):
            conn.execute(
                "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) "
                "VALUES (?, ?, ?, ?)",
                (i, seg_id, eid, b"\x00"),
            )
            conn.execute(
                "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                (i, "source", source),
            )
            conn.execute(
                "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                (i, "collection", "maker-manuals"),
            )
            conn.execute(
                "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                (i, "document_id", document_id),
            )
            conn.execute(
                "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                (i, "source_hash", source_hash),
            )
        conn.commit()
    finally:
        conn.close()


def _write_tracker(path: Path, *, paths: list[str], chunk_ids: tuple[str, ...] = VECTOR_IDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        HASH: {
            "paths": paths,
            "chunk_ids": list(chunk_ids),
            "collection": "maker-manuals",
            "document_id": DOC,
        }
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_stores(env: dict) -> dict:
    files: dict[str, str] = {}
    for key in ("registry", "tracker", "chroma", "library_old", "library_new"):
        path = env[key]
        if path.is_file():
            files[str(path)] = _file_sha(path)
    return files


@pytest.fixture()
def env(tmp_path: Path):
    library = tmp_path / "lib"
    library.mkdir()
    old_file = library / OLD
    new_file = library / NEW
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_bytes(BYTES)
    new_file.write_bytes(BYTES)

    persist = tmp_path / "persist"
    persist.mkdir()
    registry = (tmp_path / "registry" / "metadata.sqlite3").resolve()
    registry.parent.mkdir(parents=True, exist_ok=True)
    initialize_registry(registry)
    tracker = (persist / "embedded.json").resolve()
    chroma = (persist / "chroma.sqlite3").resolve()

    with open_registry(registry) as conn:
        migrate_connection(conn, target_version=5)
        with registry_transaction(conn):
            register_subject(conn, subject_id=SUBJECT)
            register_document_version(
                conn, document_id=DOC, subject_id=SUBJECT, source_hash=HASH
            )
            old_sf = register_source_file(
                conn, document_id=DOC, relative_path=OLD, source_hash=HASH
            )["source_file_id"]
            new_sf = register_source_file(
                conn, document_id=DOC, relative_path=NEW, source_hash=HASH
            )["source_file_id"]
            third_sf = register_source_file(
                conn, document_id=DOC, relative_path=THIRD, source_hash=HASH
            )["source_file_id"]
        for sf in (old_sf, new_sf, third_sf):
            initialize_locator_lifecycle_state(
                conn,
                source_file_id=sf,
                document_id=DOC,
                source="test_fixture",
            )
        conn.commit()

    _write_tracker(tracker, paths=[OLD])
    _make_chroma(chroma, source=OLD)

    return {
        "library": library,
        "library_old": old_file,
        "library_new": new_file,
        "persist": persist,
        "registry": registry,
        "tracker": tracker,
        "chroma": chroma,
        "old_sf": old_sf,
        "new_sf": new_sf,
        "third_sf": third_sf,
    }


def _request(env: dict, **overrides) -> ExplicitMoveReconciliationRequest:
    base = {
        "approval": _approval(),
        "registry_db": str(env["registry"]),
        "persist_dir": str(env["persist"]),
        "tracker_path": str(env["tracker"]),
        "document_id": DOC,
        "source_hash": HASH,
        "old_relative_path": OLD,
        "new_relative_path": NEW,
        "old_source_file_id": env["old_sf"],
        "new_source_file_id": env["new_sf"],
        "approved_vector_ids": VECTOR_IDS,
        "compatibility_evidence": {"state": COMPAT_KNOWN_COMPATIBLE},
        "operation_id": "explicit-move-op-001",
    }
    base.update(overrides)
    return ExplicitMoveReconciliationRequest(**base)


def test_preview_succeeds_without_writes(env) -> None:
    before = _snapshot_stores(env)
    preview = preview_explicit_move_reconciliation(_request(env))
    after = _snapshot_stores(env)
    assert after == before
    assert preview.old_relative_path == OLD
    assert preview.new_relative_path == NEW
    assert preview.registry_delta["action"] == "record_locator_move_transition"
    assert preview.tracker_delta["paths_after"] == [NEW]
    assert preview.chroma_delta[ID1] == NEW


def test_successful_apply_updates_bounded_stores(env) -> None:
    preview = preview_explicit_move_reconciliation(_request(env))
    result = apply_explicit_move_reconciliation(_request(env), preview=preview)
    assert result.success is True
    assert result.verified is True
    assert result.chroma_ids_updated == VECTOR_IDS

    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["old_sf"])["activity_state"]
            == "INACTIVE"
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["new_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["third_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )

    tracker = json.loads(env["tracker"].read_text(encoding="utf-8"))
    assert tracker[HASH]["paths"] == [NEW]
    assert tracker[HASH]["chunk_ids"] == list(VECTOR_IDS)

    conn = sqlite3.connect(str(env["chroma"]))
    try:
        for eid in VECTOR_IDS:
            row = conn.execute(
                "SELECT m.string_value FROM embeddings e "
                "JOIN embedding_metadata m ON m.id = e.id "
                "WHERE e.embedding_id = ? AND m.key = 'source'",
                (eid,),
            ).fetchone()
            assert row[0] == NEW
    finally:
        conn.close()


def test_approval_mismatch_blocks_before_writes(env) -> None:
    bad = _approval(source=OTHER, destination=NEW)
    before = _snapshot_stores(env)
    with pytest.raises(ExplicitMoveReconciliationError, match="approval source_path mismatch"):
        preview_explicit_move_reconciliation(_request(env, approval=bad))
    assert _snapshot_stores(env) == before


def test_invalid_old_locator_state_blocks_before_writes(env) -> None:
    with open_registry(env["registry"]) as conn:
        with registry_transaction(conn):
            record_locator_move_transition(
                conn,
                document_id=DOC,
                old_source_file_id=env["old_sf"],
                new_source_file_id=env["new_sf"],
                operation_id="prior-move",
            )
        conn.commit()
    before = _snapshot_stores(env)
    with pytest.raises(ExplicitMoveReconciliationError, match="old locator activity_state"):
        preview_explicit_move_reconciliation(_request(env))
    assert _snapshot_stores(env) == before


def test_invalid_new_locator_compensation_pending_blocks(env) -> None:
    with open_registry(env["registry"]) as conn:
        conn.execute(
            "UPDATE source_file_locator_state SET activity_state = ? "
            "WHERE source_file_id = ?",
            (LOCATOR_COMPENSATION_PENDING, env["new_sf"]),
        )
        conn.commit()
    with pytest.raises(ExplicitMoveReconciliationError, match="COMPENSATION_PENDING"):
        preview_explicit_move_reconciliation(_request(env))


def test_tracker_mismatch_blocks_before_writes(env) -> None:
    _write_tracker(env["tracker"], paths=[NEW])
    before = _snapshot_stores(env)
    with pytest.raises(ExplicitMoveReconciliationError, match="tracker paths"):
        preview_explicit_move_reconciliation(_request(env))
    assert _snapshot_stores(env) == before


def test_missing_chroma_id_blocks_before_writes(env) -> None:
    _make_chroma(env["chroma"], source=OLD, ids=(ID1,))
    before = _snapshot_stores(env)
    with pytest.raises(ExplicitMoveReconciliationError, match="missing"):
        preview_explicit_move_reconciliation(_request(env))
    assert _snapshot_stores(env) == before


def test_unexpected_chroma_source_blocks_before_writes(env) -> None:
    _make_chroma(env["chroma"], source=NEW)
    before = _snapshot_stores(env)
    with pytest.raises(ExplicitMoveReconciliationError, match="Chroma source metadata mismatch"):
        preview_explicit_move_reconciliation(_request(env))
    assert _snapshot_stores(env) == before


def test_chroma_document_id_mismatch_blocks_before_writes(env) -> None:
    other_doc = document_id_from_bytes(b"other-document-bytes-for-chroma-test")
    _make_chroma(env["chroma"], source=OLD, document_id=other_doc)
    before = _snapshot_stores(env)
    with pytest.raises(ExplicitMoveReconciliationError, match="document_id metadata mismatch"):
        preview_explicit_move_reconciliation(_request(env))
    assert _snapshot_stores(env) == before


def test_chroma_source_hash_mismatch_blocks_before_writes(env) -> None:
    other_hash = source_hash_from_bytes(b"other-hash-bytes-for-chroma-test")
    _make_chroma(env["chroma"], source=OLD, source_hash=other_hash)
    before = _snapshot_stores(env)
    with pytest.raises(ExplicitMoveReconciliationError, match="source_hash metadata mismatch"):
        preview_explicit_move_reconciliation(_request(env))
    assert _snapshot_stores(env) == before


def test_chroma_missing_identity_metadata_blocks_before_writes(env) -> None:
    _make_chroma(env["chroma"], source=OLD)
    conn = sqlite3.connect(str(env["chroma"]))
    try:
        conn.execute(
            "DELETE FROM embedding_metadata WHERE key IN ('document_id', 'source_hash')"
        )
        conn.commit()
    finally:
        conn.close()
    before = _snapshot_stores(env)
    with pytest.raises(
        ExplicitMoveReconciliationError,
        match="document_id metadata missing|source_hash metadata missing",
    ):
        preview_explicit_move_reconciliation(_request(env))
    assert _snapshot_stores(env) == before


def test_duplicate_chroma_physical_row_blocks_before_writes(env) -> None:
    conn = sqlite3.connect(str(env["chroma"]))
    try:
        coll = conn.execute("SELECT id FROM collections LIMIT 1").fetchone()[0]
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
    before = _snapshot_stores(env)
    with pytest.raises(
        ExplicitMoveReconciliationError,
        match="exactly one physical row",
    ):
        preview_explicit_move_reconciliation(_request(env))
    assert _snapshot_stores(env) == before


def test_chroma_updater_rejects_duplicate_introduced_after_preflight(env) -> None:
    original_update = em_module._update_chroma_source_metadata
    call_count = {"n": 0}

    def duplicate_then_update(path: Path, updates: dict) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            conn = sqlite3.connect(str(path))
            try:
                coll = conn.execute("SELECT id FROM collections LIMIT 1").fetchone()[0]
                seg2 = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO segments (id, type, scope, collection) "
                    "VALUES (?, ?, ?, ?)",
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
        original_update(path, updates)

    result = apply_explicit_move_reconciliation(
        _request(env),
        chroma_updater=duplicate_then_update,
    )
    assert result.success is False
    assert result.compensated is True
    assert not result.residual_unrecovered
    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["old_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["new_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
    conn = sqlite3.connect(str(env["chroma"]))
    try:
        row = conn.execute(
            "SELECT m.string_value FROM embeddings e "
            "JOIN embedding_metadata m ON m.id = e.id "
            "WHERE e.embedding_id = ? AND m.key = 'source'",
            (ID1,),
        ).fetchone()
        assert row[0] == OLD
    finally:
        conn.close()


def test_non_compatible_generation_blocks_before_writes(env) -> None:
    before = _snapshot_stores(env)
    with pytest.raises(ExplicitMoveReconciliationError, match="KNOWN_COMPATIBLE"):
        preview_explicit_move_reconciliation(
            _request(env, compatibility_evidence={"state": "UNKNOWN_LEGACY"})
        )
    assert _snapshot_stores(env) == before


def test_tracker_failure_triggers_registry_compensation(env) -> None:
    def fail_tracker(path: Path, data: dict) -> None:
        raise OSError("simulated tracker write failure")

    result = apply_explicit_move_reconciliation(
        _request(env),
        tracker_writer=fail_tracker,
    )
    assert result.success is False
    assert result.compensated is True
    assert not result.residual_unrecovered
    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["old_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["new_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
    tracker = json.loads(env["tracker"].read_text(encoding="utf-8"))
    assert tracker[HASH]["paths"] == [OLD]


def test_tracker_failure_with_new_locator_inactive_restores_both_states(env) -> None:
    with open_registry(env["registry"]) as conn:
        conn.execute(
            "UPDATE source_file_locator_state SET activity_state = ? "
            "WHERE source_file_id = ?",
            ("INACTIVE", env["new_sf"]),
        )
        conn.commit()

    def fail_tracker(path: Path, data: dict) -> None:
        raise OSError("simulated tracker write failure")

    result = apply_explicit_move_reconciliation(
        _request(env),
        tracker_writer=fail_tracker,
    )
    assert result.success is False
    assert result.compensated is True
    assert not result.residual_unrecovered
    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["old_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["new_sf"])["activity_state"]
            == "INACTIVE"
        )


def test_chroma_failure_restores_tracker_and_registry(env) -> None:
    def fail_chroma(path: Path, updates: dict) -> None:
        raise OSError("simulated chroma update failure")

    result = apply_explicit_move_reconciliation(
        _request(env),
        chroma_updater=fail_chroma,
    )
    assert result.success is False
    assert result.compensated is True
    assert not result.residual_unrecovered
    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["old_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["new_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
    tracker = json.loads(env["tracker"].read_text(encoding="utf-8"))
    assert tracker[HASH]["paths"] == [OLD]
    conn = sqlite3.connect(str(env["chroma"]))
    try:
        row = conn.execute(
            "SELECT m.string_value FROM embeddings e "
            "JOIN embedding_metadata m ON m.id = e.id "
            "WHERE e.embedding_id = ? AND m.key = 'source'",
            (ID1,),
        ).fetchone()
        assert row[0] == OLD
    finally:
        conn.close()


def test_chroma_failure_with_new_locator_inactive_restores_both_states(env) -> None:
    with open_registry(env["registry"]) as conn:
        conn.execute(
            "UPDATE source_file_locator_state SET activity_state = ? "
            "WHERE source_file_id = ?",
            ("INACTIVE", env["new_sf"]),
        )
        conn.commit()

    def fail_chroma(path: Path, updates: dict) -> None:
        raise OSError("simulated chroma update failure")

    result = apply_explicit_move_reconciliation(
        _request(env),
        chroma_updater=fail_chroma,
    )
    assert result.success is False
    assert result.compensated is True
    assert not result.residual_unrecovered
    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["old_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["new_sf"])["activity_state"]
            == "INACTIVE"
        )


def test_post_verification_failure_triggers_compensation(env) -> None:
    with mock.patch.object(
        em_module,
        "_verify_post_apply",
        side_effect=ExplicitMoveReconciliationError("simulated verification failure"),
    ):
        result = apply_explicit_move_reconciliation(_request(env))
    assert result.success is False
    assert result.compensated is True
    assert not result.residual_unrecovered
    assert "simulated verification failure" in (result.error_message or "")
    with open_registry(env["registry"]) as conn:
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["old_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=env["new_sf"])["activity_state"]
            == LOCATOR_ACTIVE
        )
    tracker = json.loads(env["tracker"].read_text(encoding="utf-8"))
    assert tracker[HASH]["paths"] == [OLD]


def test_compensation_preserves_forward_move_history(env) -> None:
    operation_id = "explicit-move-op-history"

    def fail_tracker(path: Path, data: dict) -> None:
        raise OSError("simulated tracker write failure")

    apply_explicit_move_reconciliation(
        _request(env, operation_id=operation_id),
        tracker_writer=fail_tracker,
    )
    with open_registry(env["registry"]) as conn:
        forward = conn.execute(
            "SELECT event_id, source_file_id, event_type, operation_id "
            "FROM source_file_locator_lifecycle_events "
            "WHERE operation_id = ? AND event_type = ?",
            (operation_id, LOCATOR_EVENT_MOVED),
        ).fetchall()
        assert len(forward) == 2
        recovery = conn.execute(
            "SELECT event_id, source_file_id, event_type, operation_id "
            "FROM source_file_locator_lifecycle_events "
            "WHERE operation_id = ?",
            (f"{operation_id}:compensate",),
        ).fetchall()
        assert recovery
        assert all(r["event_type"] != LOCATOR_EVENT_MOVED for r in recovery)


def test_compensated_false_when_registry_recovery_readback_fails(env) -> None:
    def fail_tracker(path: Path, data: dict) -> None:
        raise OSError("simulated tracker write failure")

    with mock.patch.object(
        em_module,
        "restore_locator_move_states_after_failure",
        side_effect=RuntimeError("simulated recovery failure"),
    ):
        result = apply_explicit_move_reconciliation(
            _request(env),
            tracker_writer=fail_tracker,
        )
    assert result.success is False
    assert result.compensated is False
    assert any("registry_compensation_failed" in item for item in result.residual_unrecovered)


def test_no_filesystem_mutation(env) -> None:
    before = {
        str(env["library_old"]): _file_sha(env["library_old"]),
        str(env["library_new"]): _file_sha(env["library_new"]),
    }
    apply_explicit_move_reconciliation(_request(env))
    after = {
        str(env["library_old"]): _file_sha(env["library_old"]),
        str(env["library_new"]): _file_sha(env["library_new"]),
    }
    assert after == before


def test_no_reconcile_path_or_unbounded_chroma_calls(env) -> None:
    with mock.patch("rag_engine.reconcile_path.inspect_reconcile_request") as inspect_mock:
        with mock.patch("rag_engine.reconcile_path.run_reconcile_path") as run_mock:
            with mock.patch(
                "rag_engine.library_state.evidence.lookup_chroma_by_embedding_ids",
                wraps=lookup_chroma_by_embedding_ids,
            ) as chroma_lookup:
                preview_explicit_move_reconciliation(_request(env))
                apply_explicit_move_reconciliation(
                    _request(env, operation_id="op-002"),
                )
                inspect_mock.assert_not_called()
                run_mock.assert_not_called()
                for call in chroma_lookup.call_args_list:
                    assert call.args[1]

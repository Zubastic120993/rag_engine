"""V5 source-file locator lifecycle schema, migration, and repository tests."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from rag_engine.metadata_registry import (
    CURRENT_SCHEMA_VERSION,
    LOCATOR_ACTIVE,
    LOCATOR_EVENT_INITIALIZED,
    LOCATOR_EVENT_MOVED,
    LOCATOR_INACTIVE,
    MIGRATION_V5_REASON,
    MIGRATION_V5_SOURCE,
    LifecycleTransitionError,
    MigrationError,
    RegistryValidationError,
    REQUIRED_TABLES,
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
    SOURCE_FILE_EVENT_REGISTERED,
    append_source_file_event,
    foreign_keys_enabled,
    get_locator_lifecycle_state,
    get_schema_version,
    initialize_locator_lifecycle_state,
    initialize_registry,
    list_locator_lifecycle_states_for_document,
    migrate_connection,
    open_registry,
    record_locator_move_transition,
    restore_locator_move_states_after_failure,
    register_document_version,
    register_source_file,
    register_subject,
    registry_transaction,
    verify_locator_state_event_consistency,
)
from rag_engine.metadata_registry.migrations import (
    _apply_v5,
    _ensure_v5_complete,
    _verify_v5_postflight,
    _v5_missing_indexes,
    _v5_objects_complete,
    utc_now,
)
from rag_engine.metadata_registry.schema import (
    SCHEMA_SQL_V5_UPGRADE,
    V5_REQUIRED_INDEXES,
)
from rag_engine.stable_identity import (
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
)


def _digest(seed: str = "a") -> str:
    return ("a" if seed == "r" else seed[0] if seed else "a") * 64


def _create_v3_registry(tmp_path: Path) -> Path:
    db = (tmp_path / "v3.sqlite3").resolve()
    conn = open_registry(db, create=True)
    try:
        migrate_connection(conn, target_version=3)
    finally:
        conn.close()
    return db


def _create_v4_registry(tmp_path: Path) -> Path:
    db = (tmp_path / "v4.sqlite3").resolve()
    conn = open_registry(db, create=True)
    try:
        migrate_connection(conn, target_version=4)
    finally:
        conn.close()
    return db


def _seed_document_with_locators(
    conn,
    *,
    label: bytes = b"v5-lifecycle",
    subject_key: str = "v5-loc",
    paths: tuple[str, ...] = ("manuals/a.pdf", "manuals/b.pdf", "manuals/c.pdf"),
    status: str | None = "legacy-status",
) -> tuple[str, list[str]]:
    sid = subject_id_from_key("sms", subject_key)
    doc = document_id_from_bytes(label)
    locators: list[str] = []
    with registry_transaction(conn):
        register_subject(conn, subject_id=sid)
        register_document_version(
            conn,
            document_id=doc,
            subject_id=sid,
            source_hash=source_hash_from_bytes(label),
        )
        for path in paths:
            loc = register_source_file(
                conn,
                document_id=doc,
                relative_path=path,
                status=status,
            )
            locators.append(loc["source_file_id"])
    return doc, locators


def _seed_with_lifecycle(conn, **kwargs) -> tuple[str, list[str]]:
    doc, locators = _seed_document_with_locators(conn, **kwargs)
    for sf in locators:
        initialize_locator_lifecycle_state(
            conn,
            source_file_id=sf,
            document_id=doc,
            source="test_fixture",
            reason="test lifecycle initialization",
        )
    return doc, locators


@pytest.fixture()
def registry_db(tmp_path: Path) -> Path:
    db = (tmp_path / "registry" / "v5.sqlite3").resolve()
    initialize_registry(db)
    return db


def test_valid_v4_to_v5_migration(tmp_path: Path) -> None:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        doc, locs = _seed_document_with_locators(conn)
        conn.commit()
        assert get_schema_version(conn) == 4
        migrate_connection(conn, target_version=5)
        assert get_schema_version(conn) == 5
        assert conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"] == 3
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM source_file_locator_state").fetchone()[
                "c"
            ]
            == 3
        )
        for sf in locs:
            st = get_locator_lifecycle_state(conn, source_file_id=sf)
            assert st is not None
            assert st["activity_state"] == LOCATOR_ACTIVE


def test_v3_to_v5_direct_apply_fails_closed(tmp_path: Path) -> None:
    db = _create_v3_registry(tmp_path)
    with open_registry(db) as conn:
        _seed_document_with_locators(conn)
        conn.commit()
        assert get_schema_version(conn) == 3
        with pytest.raises(MigrationError, match="requires schema version >= 4"):
            _apply_v5(conn)


def test_repeated_v5_migration_idempotent(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        migrate_connection(conn, target_version=5)
        migrate_connection(conn, target_version=5)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION == 5
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM registry_schema_version WHERE schema_version=5"
        ).fetchone()["c"]
        assert n == 1


def test_v5_tables_and_indexes_exist(registry_db: Path) -> None:
    with open_registry(registry_db, readonly=True) as conn:
        assert get_schema_version(conn) == 5
        for table in (
            "source_file_locator_lifecycle_events",
            "source_file_locator_state",
        ):
            assert table in REQUIRED_TABLES
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_autoindex%' "
            "AND tbl_name IN ('source_file_locator_lifecycle_events', "
            "'source_file_locator_state')"
        ).fetchall()
        assert {r["name"] for r in rows} == set(V5_REQUIRED_INDEXES)


def test_backfill_one_active_projection_and_baseline_event_per_locator(
    tmp_path: Path,
) -> None:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        doc, locs = _seed_document_with_locators(conn, paths=("only/one.pdf",))
        conn.commit()
        migrate_connection(conn, target_version=5)
        sf = locs[0]
        st = get_locator_lifecycle_state(conn, source_file_id=sf)
        assert st["activity_state"] == LOCATOR_ACTIVE
        events = conn.execute(
            "SELECT * FROM source_file_locator_lifecycle_events "
            "WHERE source_file_id = ?",
            (sf,),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["event_type"] == LOCATOR_EVENT_INITIALIZED
        assert events[0]["source"] == MIGRATION_V5_SOURCE
        assert events[0]["reason"] == MIGRATION_V5_REASON


def test_baseline_event_not_historic_registration(tmp_path: Path) -> None:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        doc, locs = _seed_document_with_locators(conn, paths=("x/y.pdf",))
        conn.commit()
        migrate_connection(conn, target_version=5)
        sf = locs[0]
        v4_reg = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE source_file_id=?",
            (sf,),
        ).fetchone()["c"]
        assert v4_reg == 0
        init = conn.execute(
            "SELECT source, reason FROM source_file_locator_lifecycle_events "
            "WHERE source_file_id=?",
            (sf,),
        ).fetchone()
        assert init["source"] == MIGRATION_V5_SOURCE
        assert "backfill" in init["reason"]


def test_orphan_v4_compensation_does_not_create_pending(tmp_path: Path) -> None:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        doc, locs = _seed_document_with_locators(conn, paths=("orphan.pdf",))
        sf = locs[0]
        with registry_transaction(conn):
            reg = append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=_digest("a"),
            )
            append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
                approval_digest=_digest("b"),
                related_event_id=reg["event_id"],
            )
        migrate_connection(conn, target_version=5)
        st = get_locator_lifecycle_state(conn, source_file_id=sf)
        assert st is not None
        assert st["activity_state"] == LOCATOR_ACTIVE
        check = verify_locator_state_event_consistency(conn, source_file_id=sf)
        assert check["requires_manual_review"] is True


def test_source_files_status_unchanged_after_v5(tmp_path: Path) -> None:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        doc, locs = _seed_document_with_locators(
            conn, paths=("status/check.pdf",), status="keep-me"
        )
        conn.commit()
        migrate_connection(conn, target_version=5)
        row = conn.execute(
            "SELECT status FROM source_files WHERE source_file_id=?", (locs[0],)
        ).fetchone()
        assert row["status"] == "keep-me"


def test_multiple_active_aliases_remain_valid(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn)
        conn.commit()
        states = list_locator_lifecycle_states_for_document(conn, document_id=doc)
        assert len(states) == 3
        assert all(s["activity_state"] == LOCATOR_ACTIVE for s in states)


def test_valid_explicit_move_transition(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn)
        old_sf, new_sf, other_sf = locs
        with registry_transaction(conn):
            result = record_locator_move_transition(
                conn,
                document_id=doc,
                old_source_file_id=old_sf,
                new_source_file_id=new_sf,
                operation_id="move-op-001",
            )
        assert result["affected_source_file_ids"] == [old_sf, new_sf]
        assert get_locator_lifecycle_state(conn, source_file_id=old_sf)["activity_state"] == LOCATOR_INACTIVE
        assert get_locator_lifecycle_state(conn, source_file_id=new_sf)["activity_state"] == LOCATOR_ACTIVE
        assert (
            get_locator_lifecycle_state(conn, source_file_id=other_sf)["activity_state"] == LOCATOR_ACTIVE
        )
        pair = conn.execute(
            "SELECT event_id, source_file_id, related_event_id FROM "
            "source_file_locator_lifecycle_events "
            "WHERE operation_id=? AND event_type=?",
            ("move-op-001", LOCATOR_EVENT_MOVED),
        ).fetchall()
        assert len(pair) == 2
        old_ev = next(r for r in pair if r["source_file_id"] == old_sf)
        new_ev = next(r for r in pair if r["source_file_id"] == new_sf)
        assert old_ev["related_event_id"] == new_ev["event_id"]


def test_move_rejects_different_document_locators(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc2, locs2 = _seed_with_lifecycle(
            conn, label=b"doc-two", subject_key="doc-two", paths=("d2/b.pdf",)
        )
        doc1, locs1 = _seed_with_lifecycle(
            conn, label=b"doc-one", subject_key="doc-one", paths=("d1/a.pdf",)
        )
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="not bound"):
                record_locator_move_transition(
                    conn,
                    document_id=doc1,
                    old_source_file_id=locs1[0],
                    new_source_file_id=locs2[0],
                    operation_id="bad-doc-move",
                )


def test_move_rejects_old_not_active(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("old.pdf", "new.pdf"))
        old_sf, new_sf = locs
        with registry_transaction(conn):
            record_locator_move_transition(
                conn,
                document_id=doc,
                old_source_file_id=old_sf,
                new_source_file_id=new_sf,
                operation_id="first-move",
            )
            with pytest.raises(LifecycleTransitionError, match="ACTIVE"):
                record_locator_move_transition(
                    conn,
                    document_id=doc,
                    old_source_file_id=old_sf,
                    new_source_file_id=new_sf,
                    operation_id="second-move",
                )


def test_move_rejects_missing_new_locator(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("only.pdf",))
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="not registered"):
                record_locator_move_transition(
                    conn,
                    document_id=doc,
                    old_source_file_id=locs[0],
                    new_source_file_id="src:nonexistent000000000000000000",
                    operation_id="missing-new",
                )


def test_move_transition_rollback_atomic(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("r1.pdf", "r2.pdf"))
        old_sf, new_sf = locs
        with pytest.raises(RuntimeError, match="simulated event failure"):
            with registry_transaction(conn):
                with mock.patch(
                    "rag_engine.metadata_registry.locator_lifecycle."
                    "_append_locator_lifecycle_event",
                    side_effect=RuntimeError("simulated event failure"),
                ):
                    record_locator_move_transition(
                        conn,
                        document_id=doc,
                        old_source_file_id=old_sf,
                        new_source_file_id=new_sf,
                        operation_id="rollback-move",
                    )
        assert get_locator_lifecycle_state(conn, source_file_id=old_sf)["activity_state"] == LOCATOR_ACTIVE
        assert get_locator_lifecycle_state(conn, source_file_id=new_sf)["activity_state"] == LOCATOR_ACTIVE
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE operation_id=?",
            ("rollback-move",),
        ).fetchone()["c"]
        assert n == 0


def test_no_source_file_row_deletion_on_move(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("del.pdf", "keep.pdf"))
        before = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        with registry_transaction(conn):
            record_locator_move_transition(
                conn,
                document_id=doc,
                old_source_file_id=locs[0],
                new_source_file_id=locs[1],
                operation_id="no-delete-move",
            )
        after = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        assert before == after == 2


def test_v5_partial_object_detection(tmp_path: Path) -> None:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        _seed_document_with_locators(conn, paths=("partial.pdf",))
        conn.commit()
        partial = SCHEMA_SQL_V5_UPGRADE.split(
            "CREATE TABLE IF NOT EXISTS source_file_locator_state"
        )[0]
        conn.executescript(partial)
        assert not _v5_objects_complete(conn)
        missing = _v5_missing_indexes(conn)
        assert "idx_source_file_locator_state_activity_state" in missing


def test_v5_recoverable_repair_converges(tmp_path: Path) -> None:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        _seed_document_with_locators(conn, paths=("repair.pdf",))
        conn.commit()
        conn.executescript(SCHEMA_SQL_V5_UPGRADE)
        assert get_schema_version(conn) == 4
        _ensure_v5_complete(conn)
        assert get_schema_version(conn) == 5
        assert _v5_objects_complete(conn)


def test_version_five_missing_object_repaired(tmp_path: Path) -> None:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        _seed_document_with_locators(conn, paths=("v5partial.pdf",))
        conn.commit()
        migrate_connection(conn, target_version=5)
        conn.execute("DROP INDEX idx_source_file_locator_state_activity_state")
        conn.commit()
        _ensure_v5_complete(conn)
        assert _v5_objects_complete(conn)
        assert foreign_keys_enabled(conn)


def test_consistency_verifier_catches_corrupted_projection(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("bad.pdf",))
        sf = locs[0]
        conn.execute(
            "UPDATE source_file_locator_state SET activity_state=? WHERE source_file_id=?",
            ("INACTIVE", sf),
        )
        conn.commit()
        check = verify_locator_state_event_consistency(conn, source_file_id=sf)
        assert check["consistent"] is False
        assert check["requires_manual_review"] is True
        assert check["discrepancies"]


def _append_v4_registered(
    conn,
    *,
    source_file_id: str,
    document_id: str,
) -> int:
    with registry_transaction(conn):
        row = append_source_file_event(
            conn,
            source_file_id=source_file_id,
            document_id=document_id,
            event_type=SOURCE_FILE_EVENT_REGISTERED,
            approval_digest=None,
            source="test",
        )
    return int(row["event_id"])


def test_move_valid_locator_specific_v4_links(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("old-v4.pdf", "new-v4.pdf"))
        old_sf, new_sf = locs
        old_v4 = _append_v4_registered(conn, source_file_id=old_sf, document_id=doc)
        new_v4 = _append_v4_registered(conn, source_file_id=new_sf, document_id=doc)
        with registry_transaction(conn):
            result = record_locator_move_transition(
                conn,
                document_id=doc,
                old_source_file_id=old_sf,
                new_source_file_id=new_sf,
                operation_id="v4-linked-move",
                old_related_v4_event_id=old_v4,
                new_related_v4_event_id=new_v4,
            )
        old_ev = result["old_event"]
        new_ev = result["new_event"]
        assert old_ev["related_v4_event_id"] == old_v4
        assert new_ev["related_v4_event_id"] == new_v4
        assert old_ev["related_event_id"] == new_ev["event_id"]
        assert new_ev["related_event_id"] is None


def test_move_rejects_old_v4_event_as_new_reference(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("swap-old.pdf", "swap-new.pdf"))
        old_sf, new_sf = locs
        old_v4 = _append_v4_registered(conn, source_file_id=old_sf, document_id=doc)
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="not bound to new locator"):
                record_locator_move_transition(
                    conn,
                    document_id=doc,
                    old_source_file_id=old_sf,
                    new_source_file_id=new_sf,
                    operation_id="wrong-new-v4",
                    new_related_v4_event_id=old_v4,
                )
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE operation_id=?",
            ("wrong-new-v4",),
        ).fetchone()["c"]
        assert n == 0
        assert get_locator_lifecycle_state(conn, source_file_id=old_sf)["activity_state"] == LOCATOR_ACTIVE
        assert get_locator_lifecycle_state(conn, source_file_id=new_sf)["activity_state"] == LOCATOR_ACTIVE


def test_move_rejects_different_document_v4_event(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc1, locs1 = _seed_with_lifecycle(
            conn,
            label=b"move-doc-one",
            subject_key="move-d1",
            paths=("d1/a.pdf", "d1/b.pdf"),
        )
        data2 = b"move-doc-two-wrong"
        doc2 = document_id_from_bytes(data2)
        sid2 = subject_id_from_key("sms", "move-d2")
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid2)
            register_document_version(
                conn,
                document_id=doc2,
                subject_id=sid2,
                source_hash=source_hash_from_bytes(data2),
            )
        old_sf, new_sf = locs1[0], locs1[1]
        ts = utc_now()
        cur = conn.execute(
            "INSERT INTO source_file_events ("
            "source_file_id, document_id, event_type, created_at"
            ") VALUES (?, ?, ?, ?)",
            (old_sf, doc2, SOURCE_FILE_EVENT_REGISTERED, ts),
        )
        conn.commit()
        wrong_id = int(cur.lastrowid)
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="not bound to document_id"):
                record_locator_move_transition(
                    conn,
                    document_id=doc1,
                    old_source_file_id=old_sf,
                    new_source_file_id=new_sf,
                    operation_id="wrong-doc-v4",
                    old_related_v4_event_id=wrong_id,
                )
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE operation_id=?",
            ("wrong-doc-v4",),
        ).fetchone()["c"]
        assert n == 0


def test_move_rejects_nonexistent_v4_event(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("ghost-old.pdf", "ghost-new.pdf"))
        old_sf, new_sf = locs
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="does not exist"):
                record_locator_move_transition(
                    conn,
                    document_id=doc,
                    old_source_file_id=old_sf,
                    new_source_file_id=new_sf,
                    operation_id="missing-v4",
                    old_related_v4_event_id=99999,
                )
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE operation_id=?",
            ("missing-v4",),
        ).fetchone()["c"]
        assert n == 0
        assert get_locator_lifecycle_state(conn, source_file_id=old_sf)["activity_state"] == LOCATOR_ACTIVE
        assert get_locator_lifecycle_state(conn, source_file_id=new_sf)["activity_state"] == LOCATOR_ACTIVE


def test_move_v4_ids_retained_on_respective_events_only(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("only-old.pdf", "only-new.pdf"))
        old_sf, new_sf = locs
        old_v4 = _append_v4_registered(conn, source_file_id=old_sf, document_id=doc)
        new_v4 = _append_v4_registered(conn, source_file_id=new_sf, document_id=doc)
        with registry_transaction(conn):
            record_locator_move_transition(
                conn,
                document_id=doc,
                old_source_file_id=old_sf,
                new_source_file_id=new_sf,
                operation_id="split-v4-ids",
                old_related_v4_event_id=old_v4,
                new_related_v4_event_id=new_v4,
            )
        rows = conn.execute(
            "SELECT source_file_id, related_v4_event_id FROM "
            "source_file_locator_lifecycle_events WHERE operation_id=?",
            ("split-v4-ids",),
        ).fetchall()
        old_row = next(r for r in rows if r["source_file_id"] == old_sf)
        new_row = next(r for r in rows if r["source_file_id"] == new_sf)
        assert old_row["related_v4_event_id"] == old_v4
        assert new_row["related_v4_event_id"] == new_v4
        assert old_row["related_v4_event_id"] != new_row["related_v4_event_id"]


def _migrate_v5_with_locators(tmp_path: Path, *, paths: tuple[str, ...]) -> tuple[Path, list[str]]:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        _, locs = _seed_document_with_locators(conn, paths=paths)
        conn.commit()
        migrate_connection(conn, target_version=5)
    return db, locs


def _duplicate_migration_baseline(conn, *, source_file_id: str) -> None:
    dup = conn.execute(
        "SELECT * FROM source_file_locator_lifecycle_events "
        "WHERE source_file_id = ? AND event_type = ? AND source = ?",
        (source_file_id, LOCATOR_EVENT_INITIALIZED, MIGRATION_V5_SOURCE),
    ).fetchone()
    ts = utc_now()
    conn.execute(
        "INSERT INTO source_file_locator_lifecycle_events ("
        "source_file_id, document_id, event_type, previous_state, new_state, "
        "related_v4_event_id, related_event_id, operation_id, approval_digest, "
        "reason, actor, source, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dup["source_file_id"],
            dup["document_id"],
            LOCATOR_EVENT_INITIALIZED,
            dup["previous_state"],
            dup["new_state"],
            dup["related_v4_event_id"],
            dup["related_event_id"],
            dup["operation_id"],
            dup["approval_digest"],
            dup["reason"],
            dup["actor"],
            MIGRATION_V5_SOURCE,
            ts,
        ),
    )


def _remove_migration_baseline(conn, *, source_file_id: str, fallback_event_id: int) -> None:
    baseline = conn.execute(
        "SELECT event_id FROM source_file_locator_lifecycle_events "
        "WHERE source_file_id = ? AND event_type = ? AND source = ?",
        (source_file_id, LOCATOR_EVENT_INITIALIZED, MIGRATION_V5_SOURCE),
    ).fetchone()
    conn.execute(
        "UPDATE source_file_locator_state SET last_lifecycle_event_id = ? "
        "WHERE source_file_id = ?",
        (fallback_event_id, source_file_id),
    )
    conn.execute(
        "DELETE FROM source_file_locator_lifecycle_events WHERE event_id = ?",
        (baseline["event_id"],),
    )


def test_v5_postflight_missing_baseline_for_one_locator(tmp_path: Path) -> None:
    db, locs = _migrate_v5_with_locators(tmp_path, paths=("a.pdf", "b.pdf", "c.pdf"))
    with open_registry(db) as conn:
        fallback = conn.execute(
            "SELECT event_id FROM source_file_locator_lifecycle_events "
            "WHERE source_file_id = ? AND event_type = ? AND source = ?",
            (locs[0], LOCATOR_EVENT_INITIALIZED, MIGRATION_V5_SOURCE),
        ).fetchone()["event_id"]
        _remove_migration_baseline(conn, source_file_id=locs[1], fallback_event_id=fallback)
        _duplicate_migration_baseline(conn, source_file_id=locs[0])
        conn.commit()
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE event_type = ? AND source = ?",
            (LOCATOR_EVENT_INITIALIZED, MIGRATION_V5_SOURCE),
        ).fetchone()["c"]
        sf_count = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        assert int(total) == int(sf_count)
        with pytest.raises(MigrationError, match="zero migration-baseline"):
            _verify_v5_postflight(conn)


def test_v5_postflight_duplicate_and_missing_baseline(tmp_path: Path) -> None:
    db, locs = _migrate_v5_with_locators(tmp_path, paths=("x.pdf", "y.pdf"))
    with open_registry(db) as conn:
        fallback = conn.execute(
            "SELECT event_id FROM source_file_locator_lifecycle_events "
            "WHERE source_file_id = ? AND event_type = ? AND source = ?",
            (locs[0], LOCATOR_EVENT_INITIALIZED, MIGRATION_V5_SOURCE),
        ).fetchone()["event_id"]
        _remove_migration_baseline(conn, source_file_id=locs[1], fallback_event_id=fallback)
        _duplicate_migration_baseline(conn, source_file_id=locs[0])
        conn.commit()
        baseline_total = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE event_type = ? AND source = ?",
            (LOCATOR_EVENT_INITIALIZED, MIGRATION_V5_SOURCE),
        ).fetchone()["c"]
        sf_count = conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
        assert int(baseline_total) == int(sf_count)
        with pytest.raises(MigrationError) as exc:
            _verify_v5_postflight(conn)
        msg = str(exc.value)
        assert locs[0] in msg
        assert locs[1] in msg
        assert "migration-baseline" in msg


def test_v5_postflight_one_baseline_and_state_per_locator_passes(tmp_path: Path) -> None:
    db, _ = _migrate_v5_with_locators(tmp_path, paths=("ok.pdf",))
    with open_registry(db) as conn:
        _verify_v5_postflight(conn)


def test_v5_postflight_duplicate_baseline_no_event_mutation(tmp_path: Path) -> None:
    db, locs = _migrate_v5_with_locators(tmp_path, paths=("dup.pdf",))
    with open_registry(db) as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events"
        ).fetchone()["c"]
        _duplicate_migration_baseline(conn, source_file_id=locs[0])
        conn.commit()
        with pytest.raises(MigrationError, match="migration-baseline"):
            _ensure_v5_complete(conn)
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events"
        ).fetchone()["c"]
        assert after == before + 1
        dup_count = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE source_file_id = ? AND event_type = ? AND source = ?",
            (locs[0], LOCATOR_EVENT_INITIALIZED, MIGRATION_V5_SOURCE),
        ).fetchone()["c"]
        assert dup_count == 2


def test_restore_move_failure_reactivates_old_and_preserves_new_active(
    registry_db: Path,
) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("old.pdf", "new.pdf"))
        old_sf, new_sf = locs
        with registry_transaction(conn):
            record_locator_move_transition(
                conn,
                document_id=doc,
                old_source_file_id=old_sf,
                new_source_file_id=new_sf,
                operation_id="failed-move-active",
            )
            restore_locator_move_states_after_failure(
                conn,
                document_id=doc,
                old_source_file_id=old_sf,
                new_source_file_id=new_sf,
                old_target_activity_state=LOCATOR_ACTIVE,
                new_target_activity_state=LOCATOR_ACTIVE,
                failed_move_operation_id="failed-move-active",
                operation_id="failed-move-active:recover",
            )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=old_sf)["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=new_sf)["activity_state"]
            == LOCATOR_ACTIVE
        )
        forward = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_locator_lifecycle_events "
            "WHERE operation_id = ? AND event_type = ?",
            ("failed-move-active", LOCATOR_EVENT_MOVED),
        ).fetchone()["c"]
        assert forward == 2


def test_restore_move_failure_restores_new_inactive(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        doc, locs = _seed_with_lifecycle(conn, paths=("old-i.pdf", "new-i.pdf"))
        old_sf, new_sf = locs
        conn.execute(
            "UPDATE source_file_locator_state SET activity_state = ? "
            "WHERE source_file_id = ?",
            (LOCATOR_INACTIVE, new_sf),
        )
        conn.commit()
        with registry_transaction(conn):
            record_locator_move_transition(
                conn,
                document_id=doc,
                old_source_file_id=old_sf,
                new_source_file_id=new_sf,
                operation_id="failed-move-inactive",
            )
            restore_locator_move_states_after_failure(
                conn,
                document_id=doc,
                old_source_file_id=old_sf,
                new_source_file_id=new_sf,
                old_target_activity_state=LOCATOR_ACTIVE,
                new_target_activity_state=LOCATOR_INACTIVE,
                failed_move_operation_id="failed-move-inactive",
                operation_id="failed-move-inactive:recover",
            )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=old_sf)["activity_state"]
            == LOCATOR_ACTIVE
        )
        assert (
            get_locator_lifecycle_state(conn, source_file_id=new_sf)["activity_state"]
            == LOCATOR_INACTIVE
        )

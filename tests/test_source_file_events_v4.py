"""V4 source_file_events schema, migration, and repository primitive tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rag_engine.metadata_registry import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    RegistryIntegrityError,
    RegistryValidationError,
    REQUIRED_TABLES,
    SOURCE_FILE_EVENT_ALIAS_REGISTERED,
    SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
    SOURCE_FILE_EVENT_REGISTERED,
    append_source_file_event,
    foreign_keys_enabled,
    get_schema_version,
    initialize_registry,
    migrate_connection,
    open_registry,
    register_document_version,
    register_source_file,
    register_subject,
    registry_transaction,
)
from rag_engine.metadata_registry.migrations import (
    _apply_v4_ddl,
    _ensure_v4_complete,
    _find_duplicate_approval_digests,
    _v4_missing_indexes,
    _v4_objects_complete,
    _v4_table_present,
    utc_now,
)
from rag_engine.metadata_registry.schema import (
    SCHEMA_SQL_V4_UPGRADE,
    V4_REQUIRED_INDEXES,
)
from rag_engine.stable_identity import (
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
)


def _create_v4_registry(tmp_path: Path) -> Path:
    db = (tmp_path / "v4_registry.sqlite3").resolve()
    conn = open_registry(db, create=True)
    try:
        migrate_connection(conn, target_version=4)
    finally:
        conn.close()
    return db


def _create_v3_registry(tmp_path: Path) -> Path:
    db = (tmp_path / "v3_registry.sqlite3").resolve()
    conn = open_registry(db, create=True)
    try:
        migrate_connection(conn, target_version=3)
    finally:
        conn.close()
    return db


def _seed_source_file(conn: sqlite3.Connection) -> tuple[str, str, str]:
    sid = subject_id_from_key("sms", "v4-event")
    data = b"v4-source-file-event"
    doc = document_id_from_bytes(data)
    with registry_transaction(conn):
        register_subject(conn, subject_id=sid)
        register_document_version(
            conn,
            document_id=doc,
            subject_id=sid,
            source_hash=source_hash_from_bytes(data),
        )
        loc = register_source_file(
            conn,
            document_id=doc,
            relative_path="manuals/v4-test.pdf",
        )
    return loc["source_file_id"], doc, sid


def _v4_indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='source_file_events'"
    ).fetchall()
    return {row["name"] for row in rows}


def _digest(seed: str = "a") -> str:
    return (seed * 64)[:64]


def _seed_second_source_file(
    conn: sqlite3.Connection,
    *,
    label: bytes = b"v4-second-locator",
    relative_path: str = "manuals/v4-second.pdf",
) -> tuple[str, str]:
    sid = subject_id_from_key("sms", "v4-second")
    doc = document_id_from_bytes(label)
    with registry_transaction(conn):
        register_subject(conn, subject_id=sid)
        register_document_version(
            conn,
            document_id=doc,
            subject_id=sid,
            source_hash=source_hash_from_bytes(label),
        )
        loc = register_source_file(
            conn,
            document_id=doc,
            relative_path=relative_path,
        )
    return loc["source_file_id"], doc


@pytest.fixture()
def registry_db(tmp_path: Path) -> Path:
    db = (tmp_path / "registry" / "v4.sqlite3").resolve()
    initialize_registry(db)
    return db


# ---------------------------------------------------------------------------
# Migration / schema
# ---------------------------------------------------------------------------


def test_clean_v3_to_v4_migration(tmp_path: Path) -> None:
    db = _create_v3_registry(tmp_path)
    sid = subject_id_from_key("sms", "pre-v4")
    data = b"keep-through-v4"
    doc = document_id_from_bytes(data)
    with open_registry(db) as conn:
        register_subject(conn, subject_id=sid)
        register_document_version(
            conn,
            document_id=doc,
            subject_id=sid,
            source_hash=source_hash_from_bytes(data),
        )
        conn.commit()
        assert get_schema_version(conn) == 3
        migrate_connection(conn, target_version=4)
        assert get_schema_version(conn) == 4
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM document_versions WHERE document_id=?",
            (doc,),
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events"
        ).fetchone()["c"] == 0


def test_repeated_v4_migration(tmp_path: Path) -> None:
    db = _create_v4_registry(tmp_path)
    with open_registry(db) as conn:
        migrate_connection(conn, target_version=4)
        migrate_connection(conn, target_version=4)
        assert get_schema_version(conn) == 4
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM registry_schema_version WHERE schema_version=4"
        ).fetchone()["c"]
        assert n == 1


def test_all_four_v4_indexes_exist(registry_db: Path) -> None:
    with open_registry(registry_db, readonly=True) as conn:
        assert _v4_indexes(conn) == set(V4_REQUIRED_INDEXES)


def test_required_tables_and_version(registry_db: Path) -> None:
    with open_registry(registry_db, readonly=True) as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        assert "source_file_events" in REQUIRED_TABLES
        assert len(REQUIRED_TABLES) >= 10
        for table in REQUIRED_TABLES:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert row is not None


def test_foreign_keys_reject_orphan_source_file_event(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        _seed_source_file(conn)
        conn.commit()
    with open_registry(registry_db) as conn:
        assert foreign_keys_enabled(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_file_events ("
                "source_file_id, document_id, event_type, created_at"
                ") VALUES (?, ?, ?, ?)",
                ("src:missing", "docrev:" + "a" * 64, SOURCE_FILE_EVENT_REGISTERED, utc_now()),
            )
            conn.commit()


def test_check_vocabulary_rejects_seen_again(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        conn.commit()
    with open_registry(registry_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO source_file_events ("
                "source_file_id, document_id, event_type, created_at"
                ") VALUES (?, ?, ?, ?)",
                (sf, doc, "SOURCE_FILE_SEEN_AGAIN", utc_now()),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Repository append primitive
# ---------------------------------------------------------------------------


def test_registered_event_accepts_null_digest(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            row = append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_REGISTERED,
                approval_digest=None,
                source="test",
            )
        assert row["approval_digest"] is None
        assert row["event_type"] == SOURCE_FILE_EVENT_REGISTERED


def test_alias_event_requires_canonical_digest(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            with pytest.raises(
                RegistryValidationError,
                match="leading or trailing whitespace|64 lowercase hexadecimal",
            ):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                    approval_digest="   ",
                )


def test_compensation_event_requires_distinct_nonblank_digest(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        reg_digest = _digest("a")
        with registry_transaction(conn):
            reg = append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=reg_digest,
            )
            comp = append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
                approval_digest=_digest("b"),
                related_event_id=reg["event_id"],
            )
            with pytest.raises(RegistryValidationError, match="must not reuse"):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
                    approval_digest=reg_digest,
                    related_event_id=reg["event_id"],
                )
        assert comp["approval_digest"] != reg_digest


def test_duplicate_non_null_digest_rejected(registry_db: Path) -> None:
    digest = _digest("d")
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=digest,
            )
            with pytest.raises(RegistryIntegrityError):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                    approval_digest=digest,
                )


def test_null_digests_permitted_for_non_consuming_rows(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            for _ in range(3):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_REGISTERED,
                    approval_digest=None,
                )
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events "
            "WHERE approval_digest IS NULL"
        ).fetchone()["c"]
        assert n == 3


def test_event_self_link_foreign_key(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="does not exist"):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
                    approval_digest=_digest("c"),
                    related_event_id=99999,
                )


# ---------------------------------------------------------------------------
# Phase A correction: canonical digest + compensation linkage
# ---------------------------------------------------------------------------


def test_valid_lowercase_digest_succeeds(registry_db: Path) -> None:
    digest = "0123456789abcdef" * 4
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            row = append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=digest,
            )
        assert row["approval_digest"] == digest


def test_uppercase_digest_rejected(registry_db: Path) -> None:
    digest = ("A" * 64).lower().upper()
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="64 lowercase hexadecimal"):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                    approval_digest=digest,
                )


def test_short_digest_rejected(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="64 lowercase hexadecimal"):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                    approval_digest="abc123",
                )


def test_non_hex_digest_rejected(registry_db: Path) -> None:
    digest = "g" * 64
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="64 lowercase hexadecimal"):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                    approval_digest=digest,
                )


def test_whitespace_padded_digest_rejected_not_trimmed(registry_db: Path) -> None:
    digest = f" {_digest('f')} "
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="leading or trailing whitespace"):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                    approval_digest=digest,
                )
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM source_file_events WHERE approval_digest IS NOT NULL"
        ).fetchone()["c"]
        assert n == 0


def test_compensation_requires_related_event_id(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="requires related_event_id"):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
                    approval_digest=_digest("c"),
                    related_event_id=None,
                )


def test_compensation_rejects_other_source_file_link(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf1, doc1, _ = _seed_source_file(conn)
        sf2, doc2 = _seed_second_source_file(conn)
        with registry_transaction(conn):
            alias = append_source_file_event(
                conn,
                source_file_id=sf1,
                document_id=doc1,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=_digest("1"),
            )
            with pytest.raises(RegistryValidationError, match="same source_file_id"):
                append_source_file_event(
                    conn,
                    source_file_id=sf2,
                    document_id=doc2,
                    event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
                    approval_digest=_digest("2"),
                    related_event_id=alias["event_id"],
                )


def test_compensation_rejects_other_document_link(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc1, _ = _seed_source_file(conn)
        data2 = b"v4-other-document"
        doc2 = document_id_from_bytes(data2)
        sid2 = subject_id_from_key("sms", "v4-other-doc")
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid2)
            register_document_version(
                conn,
                document_id=doc2,
                subject_id=sid2,
                source_hash=source_hash_from_bytes(data2),
            )
        ts = utc_now()
        cur = conn.execute(
            "INSERT INTO source_file_events ("
            "source_file_id, document_id, event_type, approval_digest, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (sf, doc2, SOURCE_FILE_EVENT_ALIAS_REGISTERED, _digest("3"), ts),
        )
        conn.commit()
        alias_event_id = int(cur.lastrowid)
        with registry_transaction(conn):
            with pytest.raises(RegistryValidationError, match="same document_id"):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc1,
                    event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
                    approval_digest=_digest("4"),
                    related_event_id=alias_event_id,
                )


def test_compensation_rejects_registered_event_link(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        with registry_transaction(conn):
            registered = append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_REGISTERED,
                approval_digest=None,
            )
            with pytest.raises(
                RegistryValidationError, match="SOURCE_FILE_ALIAS_REGISTERED"
            ):
                append_source_file_event(
                    conn,
                    source_file_id=sf,
                    document_id=doc,
                    event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
                    approval_digest=_digest("5"),
                    related_event_id=registered["event_id"],
                )


def test_compensation_valid_alias_linkage_succeeds(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        sf, doc, _ = _seed_source_file(conn)
        alias_digest = _digest("6")
        comp_digest = _digest("7")
        with registry_transaction(conn):
            alias = append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_ALIAS_REGISTERED,
                approval_digest=alias_digest,
            )
            comp = append_source_file_event(
                conn,
                source_file_id=sf,
                document_id=doc,
                event_type=SOURCE_FILE_EVENT_COMPENSATION_REQUESTED,
                approval_digest=comp_digest,
                related_event_id=alias["event_id"],
            )
        assert comp["related_event_id"] == alias["event_id"]
        assert comp["approval_digest"] == comp_digest
        assert comp["approval_digest"] != alias_digest


# ---------------------------------------------------------------------------
# Partial migration detection and repair
# ---------------------------------------------------------------------------


def test_partial_table_index_version_states_detected(tmp_path: Path) -> None:
    db = _create_v3_registry(tmp_path)
    with open_registry(db) as conn:
        assert not _v4_objects_complete(conn)
        partial = SCHEMA_SQL_V4_UPGRADE.split(
            "CREATE UNIQUE INDEX IF NOT EXISTS"
        )[0]
        conn.executescript(partial)
        assert _v4_table_present(conn)
        missing = _v4_missing_indexes(conn)
        assert "idx_source_file_events_approval_digest_unique" in missing
        assert get_schema_version(conn) == 3


def test_recoverable_missing_object_state_converges(tmp_path: Path) -> None:
    db = _create_v3_registry(tmp_path)
    with open_registry(db) as conn:
        conn.executescript(SCHEMA_SQL_V4_UPGRADE)
        assert get_schema_version(conn) == 3
        migrate_connection(conn, target_version=4)
        assert get_schema_version(conn) == 4
        assert _v4_objects_complete(conn)


def test_incomplete_unique_index_repair(tmp_path: Path) -> None:
    db = _create_v3_registry(tmp_path)
    with open_registry(db) as conn:
        partial = SCHEMA_SQL_V4_UPGRADE.split(
            "CREATE UNIQUE INDEX IF NOT EXISTS"
        )[0]
        conn.executescript(partial)
        missing = _v4_missing_indexes(conn)
        assert "idx_source_file_events_approval_digest_unique" in missing
        migrate_connection(conn, target_version=4)
        assert _v4_objects_complete(conn)


def test_duplicate_pre_index_digest_fails_closed(tmp_path: Path) -> None:
    db = _create_v3_registry(tmp_path)
    sid = subject_id_from_key("sms", "dup-pre-index")
    data = b"dup-digest"
    doc = document_id_from_bytes(data)
    digest = "e" * 64
    with open_registry(db) as conn:
        register_subject(conn, subject_id=sid)
        register_document_version(
            conn,
            document_id=doc,
            subject_id=sid,
            source_hash=source_hash_from_bytes(data),
        )
        loc = register_source_file(conn, document_id=doc, relative_path="dup.pdf")
        conn.commit()

    with open_registry(db) as conn:
        table_only = SCHEMA_SQL_V4_UPGRADE.split("CREATE INDEX IF NOT EXISTS")[0]
        conn.executescript(table_only)
        sf = loc["source_file_id"]
        ts = utc_now()
        conn.execute(
            "INSERT INTO source_file_events ("
            "source_file_id, document_id, event_type, approval_digest, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (sf, doc, SOURCE_FILE_EVENT_ALIAS_REGISTERED, digest, ts),
        )
        conn.execute(
            "INSERT INTO source_file_events ("
            "source_file_id, document_id, event_type, approval_digest, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (sf, doc, SOURCE_FILE_EVENT_ALIAS_REGISTERED, digest, ts),
        )
        conn.commit()
        assert _find_duplicate_approval_digests(conn)
        with pytest.raises(MigrationError, match="duplicate non-null approval_digest"):
            _apply_v4_ddl(conn)


def test_version_four_with_missing_object_repaired(tmp_path: Path) -> None:
    db = _create_v3_registry(tmp_path)
    with open_registry(db) as conn:
        conn.execute(
            "INSERT INTO registry_schema_version "
            "(schema_version, applied_at, status, description, backward_compatible) "
            "VALUES (4, ?, 'applied', 'partial', 0)",
            (utc_now(),),
        )
        conn.commit()
        assert get_schema_version(conn) == 4
        assert not _v4_objects_complete(conn)
        _ensure_v4_complete(conn)
        assert _v4_objects_complete(conn)
        assert get_schema_version(conn) == 4

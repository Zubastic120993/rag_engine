"""Phase 5 document revision / replacement lifecycle tests — temp SQLite only."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from rag_engine.metadata_registry import (
    CURRENT_SCHEMA_VERSION,
    LifecycleTransitionError,
    RegistryConflictError,
    RegistryValidationError,
    activate_revision,
    archive_revision,
    get_active_revision,
    get_lifecycle_history,
    get_relations,
    get_revision_status,
    get_schema_version,
    initialize_registry,
    mark_duplicate,
    migrate_connection,
    open_registry,
    register_document_version,
    register_subject,
    registry_transaction,
    replace_revision,
    set_revision_status,
    supersede_revision,
    transition_matrix,
    withdraw_revision,
)
from rag_engine.metadata_registry.migrations import (
    MIGRATION_V2_MULTI_REASON,
    MIGRATION_V2_SOURCE,
    utc_now,
)
from rag_engine.metadata_registry.schema import SCHEMA_SQL
from rag_engine.stable_identity import (
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
)


@pytest.fixture()
def registry_db(tmp_path: Path) -> Path:
    db = (tmp_path / "registry" / "lifecycle_v2.sqlite3").resolve()
    assert ".rag_db" not in str(db)
    assert ".rag_state" not in str(db)
    initialize_registry(db)
    return db


def _rev(conn: sqlite3.Connection, label: bytes, subject_id: str, **kwargs):
    data = label
    return register_document_version(
        conn,
        document_id=document_id_from_bytes(data),
        subject_id=subject_id,
        source_hash=source_hash_from_bytes(data),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Basic status
# ---------------------------------------------------------------------------


def test_new_revision_defaults_active(registry_db: Path) -> None:
    sid = subject_id_from_key("sms", "lc-default")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            rev = _rev(conn, b"first-rev", sid)
        assert rev["lifecycle_status"] == "ACTIVE"
        assert get_active_revision(conn, sid)["document_id"] == rev["document_id"]
        hist = get_lifecycle_history(conn, rev["document_id"])
        assert len(hist) == 1
        assert hist[0]["previous_state"] is None
        assert hist[0]["new_state"] == "ACTIVE"


def test_archive_and_withdraw(registry_db: Path) -> None:
    sid = subject_id_from_key("sms", "lc-arch")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            rev = _rev(conn, b"arch-rev", sid)
            doc = rev["document_id"]
        withdraw_revision(conn, doc, reason="ops withdraw")
        assert get_revision_status(conn, doc)["lifecycle_status"] == "WITHDRAWN"
        assert get_active_revision(conn, sid) is None
        archive_revision(conn, doc, reason="retain history")
        assert get_revision_status(conn, doc)["lifecycle_status"] == "ARCHIVED"
        hist = get_lifecycle_history(conn, doc)
        assert [e["new_state"] for e in hist] == ["ACTIVE", "WITHDRAWN", "ARCHIVED"]


# ---------------------------------------------------------------------------
# Supersede
# ---------------------------------------------------------------------------


def test_supersede_same_subject_atomic(registry_db: Path) -> None:
    sid = subject_id_from_key("maker_doc", "pump")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            old = _rev(conn, b"pump-A", sid)
            new = _rev(
                conn, b"pump-B", sid, lifecycle_status="WITHDRAWN"
            )
        result = supersede_revision(
            conn,
            old_document_id=old["document_id"],
            new_document_id=new["document_id"],
            reason="rev B published",
            actor="chief",
        )
        assert result["idempotent"] is False
        assert result["old"]["lifecycle_status"] == "SUPERSEDED"
        assert result["new"]["lifecycle_status"] == "ACTIVE"
        assert get_active_revision(conn, sid)["document_id"] == new["document_id"]
        rels = get_relations(conn, new["document_id"])
        assert any(
            r["relation_type"] == "supersedes"
            and r["target_document_id"] == old["document_id"]
            for r in rels
        )
        old_hist = get_lifecycle_history(conn, old["document_id"])
        assert old_hist[-1]["new_state"] == "SUPERSEDED"
        assert old_hist[-1]["related_document_id"] == new["document_id"]


def test_supersede_requires_same_subject(registry_db: Path) -> None:
    s1 = subject_id_from_key("sms", "a")
    s2 = subject_id_from_key("sms", "b")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=s1)
            register_subject(conn, subject_id=s2)
            a = _rev(conn, b"cross-A", s1)
            b = _rev(conn, b"cross-B", s2)
        with pytest.raises(LifecycleTransitionError, match="same subject"):
            supersede_revision(
                conn,
                old_document_id=a["document_id"],
                new_document_id=b["document_id"],
                reason="bad",
            )


def test_supersede_idempotent(registry_db: Path) -> None:
    sid = subject_id_from_key("sms", "idem-sup")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            old = _rev(conn, b"idem-A", sid)
            new = _rev(conn, b"idem-B", sid, lifecycle_status="WITHDRAWN")
        supersede_revision(
            conn,
            old_document_id=old["document_id"],
            new_document_id=new["document_id"],
            reason="once",
        )
        n_events = conn.execute(
            "SELECT COUNT(*) AS c FROM document_lifecycle_events"
        ).fetchone()["c"]
        n_rels = conn.execute(
            "SELECT COUNT(*) AS c FROM document_version_relations"
        ).fetchone()["c"]
        again = supersede_revision(
            conn,
            old_document_id=old["document_id"],
            new_document_id=new["document_id"],
            reason="twice",
        )
        assert again["idempotent"] is True
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM document_lifecycle_events"
            ).fetchone()["c"]
            == n_events
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM document_version_relations"
            ).fetchone()["c"]
            == n_rels
        )


def test_supersede_rollback_on_relation_failure(registry_db: Path) -> None:
    sid = subject_id_from_key("sms", "rollback")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            old = _rev(conn, b"rb-A", sid)
            new = _rev(conn, b"rb-B", sid, lifecycle_status="WITHDRAWN")
        with mock.patch(
            "rag_engine.metadata_registry.lifecycle._upsert_relation",
            side_effect=RuntimeError("forced relation failure"),
        ):
            with pytest.raises(RuntimeError, match="forced relation failure"):
                supersede_revision(
                    conn,
                    old_document_id=old["document_id"],
                    new_document_id=new["document_id"],
                    reason="should roll back",
                )
        assert get_revision_status(conn, old["document_id"])["lifecycle_status"] == "ACTIVE"
        assert (
            get_revision_status(conn, new["document_id"])["lifecycle_status"]
            == "WITHDRAWN"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM document_version_relations"
            ).fetchone()["c"]
            == 0
        )


# ---------------------------------------------------------------------------
# Replace / duplicate
# ---------------------------------------------------------------------------


def test_replace_cross_subject(registry_db: Path) -> None:
    s1 = subject_id_from_key("sms", "old-family")
    s2 = subject_id_from_key("sms", "new-family")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=s1)
            register_subject(conn, subject_id=s2)
            old = _rev(conn, b"repl-old", s1)
            new = _rev(conn, b"repl-new", s2)
        result = replace_revision(
            conn,
            old_document_id=old["document_id"],
            new_document_id=new["document_id"],
            reason="policy replace",
        )
        assert result["old"]["lifecycle_status"] == "REPLACED"
        assert result["new"]["lifecycle_status"] == "ACTIVE"
        assert result["relation"]["relation_type"] == "replaces"
        # new remains ACTIVE on its own subject
        assert get_active_revision(conn, s2)["document_id"] == new["document_id"]
        assert get_active_revision(conn, s1) is None


def test_replace_missing_target_rejected(registry_db: Path) -> None:
    sid = subject_id_from_key("sms", "miss")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            old = _rev(conn, b"miss-old", sid)
        missing = document_id_from_bytes(b"does-not-exist")
        with pytest.raises(RegistryValidationError, match="not found"):
            replace_revision(
                conn,
                old_document_id=old["document_id"],
                new_document_id=missing,
                reason="nope",
            )


def test_duplicate_requires_canonical_preserves_history(registry_db: Path) -> None:
    s1 = subject_id_from_key("sms", "dup-a")
    s2 = subject_id_from_key("sms", "dup-b")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=s1)
            register_subject(conn, subject_id=s2)
            a = _rev(conn, b"dup-copy", s1)
            b = _rev(conn, b"dup-canon", s2)
        with pytest.raises(RegistryValidationError, match="self-duplicate"):
            mark_duplicate(
                conn,
                document_id=a["document_id"],
                canonical_document_id=a["document_id"],
            )
        mark_duplicate(
            conn,
            document_id=a["document_id"],
            canonical_document_id=b["document_id"],
            reason="byte-identical governed",
        )
        assert get_revision_status(conn, a["document_id"])["lifecycle_status"] == "DUPLICATE"
        # row still present
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM document_versions WHERE document_id=?",
                (a["document_id"],),
            ).fetchone()["c"]
            == 1
        )
        hist = get_lifecycle_history(conn, a["document_id"])
        assert hist[-1]["new_state"] == "DUPLICATE"
        assert hist[-1]["related_document_id"] == b["document_id"]


# ---------------------------------------------------------------------------
# Active uniqueness / invalid transitions / history immutability
# ---------------------------------------------------------------------------


def test_second_active_rejected(registry_db: Path) -> None:
    sid = subject_id_from_key("sms", "uniq")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            _rev(conn, b"uniq-A", sid)
            with pytest.raises(RegistryConflictError, match="already has ACTIVE"):
                _rev(conn, b"uniq-B", sid)


def test_invalid_transitions_rejected(registry_db: Path) -> None:
    sid = subject_id_from_key("sms", "bad-tx")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            rev = _rev(conn, b"bad-tx-doc", sid)
            doc = rev["document_id"]
        withdraw_revision(conn, doc, reason="stop")
        with pytest.raises(LifecycleTransitionError):
            set_revision_status(conn, document_id=doc, new_state="ACTIVE")
        archive_revision(conn, doc, reason="archive")
        with pytest.raises(LifecycleTransitionError):
            set_revision_status(conn, document_id=doc, new_state="WITHDRAWN")
        # SUPERSEDED/REPLACED/DUPLICATE via set_revision_status blocked
        with pytest.raises(LifecycleTransitionError, match="related document"):
            set_revision_status(conn, document_id=doc, new_state="SUPERSEDED")


def test_history_append_only(registry_db: Path) -> None:
    sid = subject_id_from_key("sms", "hist")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            rev = _rev(conn, b"hist-doc", sid)
            doc = rev["document_id"]
        withdraw_revision(conn, doc, reason="w1")
        first = get_lifecycle_history(conn, doc)
        assert first[0]["event_id"] == 1
        archive_revision(conn, doc, reason="a1")
        second = get_lifecycle_history(conn, doc)
        assert second[0] == first[0]  # old event unchanged
        assert len(second) == len(first) + 1


def test_transition_matrix_covers_roadmap_states() -> None:
    matrix = transition_matrix()
    assert set(matrix) == {
        "ACTIVE",
        "SUPERSEDED",
        "ARCHIVED",
        "REPLACED",
        "WITHDRAWN",
        "DUPLICATE",
    }
    assert "ACTIVE" not in matrix["WITHDRAWN"]
    assert "ACTIVE" not in matrix["DUPLICATE"]
    assert "ACTIVE" not in matrix["SUPERSEDED"]


# ---------------------------------------------------------------------------
# Legacy boundary — no chunk_id / no chroma UUID required
# ---------------------------------------------------------------------------


def test_lifecycle_without_chunks_or_vector_map(registry_db: Path) -> None:
    sid = subject_id_from_key("sms", "legacy-free")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            old = _rev(conn, b"leg-A", sid)
            new = _rev(conn, b"leg-B", sid, lifecycle_status="WITHDRAWN")
        # Prove no chunks / mappings exist
        assert conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"] == 0
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM chunk_vector_map").fetchone()["c"]
            == 0
        )
        supersede_revision(
            conn,
            old_document_id=old["document_id"],
            new_document_id=new["document_id"],
            reason="no vectors needed",
        )
        assert get_active_revision(conn, sid)["document_id"] == new["document_id"]


# ---------------------------------------------------------------------------
# Migration v1 → v2 (evidence-safe; no invented succession)
# ---------------------------------------------------------------------------


def _build_v1_db(path: Path) -> sqlite3.Connection:
    """Create a pure schema-v1 registry (no lifecycle columns)."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO registry_schema_version "
        "(schema_version, applied_at, status, description, backward_compatible) "
        "VALUES (1, ?, 'applied', 'v1 fixture', 1)",
        (utc_now(),),
    )
    return conn


def test_v1_to_v2_single_revision_becomes_active(tmp_path: Path) -> None:
    """Test A — single revision → ACTIVE; no relations."""
    db = (tmp_path / "v1_single.sqlite3").resolve()
    conn = _build_v1_db(db)
    try:
        sid = subject_id_from_key("sms", "mig-single")
        conn.execute(
            "INSERT INTO documents (subject_id, created_at, updated_at) VALUES (?, ?, ?)",
            (sid, utc_now(), utc_now()),
        )
        data = b"mig-only-one"
        doc = document_id_from_bytes(data)
        conn.execute(
            "INSERT INTO document_versions ("
            "document_id, subject_id, source_hash, identity_scheme_version, created_at"
            ") VALUES (?, ?, ?, 'stable-id-v1', ?)",
            (doc, sid, source_hash_from_bytes(data), "2021-05-01T00:00:00Z"),
        )
        conn.commit()
        assert get_schema_version(conn) == 1
    finally:
        conn.close()

    with open_registry(db) as conn:
        migrate_connection(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION == 2
        assert get_revision_status(conn, doc)["lifecycle_status"] == "ACTIVE"
        assert get_active_revision(conn, sid)["document_id"] == doc
        n_active = conn.execute(
            "SELECT COUNT(*) AS c FROM document_versions "
            "WHERE subject_id=? AND lifecycle_status='ACTIVE'",
            (sid,),
        ).fetchone()["c"]
        assert n_active == 1
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM document_version_relations").fetchone()[
                "c"
            ]
            == 0
        )


def test_v1_to_v2_multi_revision_all_withdrawn_no_relations(tmp_path: Path) -> None:
    """Test B + C — multi-revision → all WITHDRAWN; zero ACTIVE; audit only."""
    db = (tmp_path / "v1_multi.sqlite3").resolve()
    conn = _build_v1_db(db)
    try:
        sid = subject_id_from_key("sms", "mig-multi")
        conn.execute(
            "INSERT INTO documents (subject_id, created_at, updated_at) VALUES (?, ?, ?)",
            (sid, utc_now(), utc_now()),
        )
        # Scrambled insert order vs created_at vs document_id lexical order.
        specs = [
            (b"mig-newest-zzz", "2024-06-01T00:00:00Z"),
            (b"mig-oldest-aaa", "2020-01-01T00:00:00Z"),
            (b"mig-middle-mmm", "2022-01-01T00:00:00Z"),
        ]
        docs = []
        for data, created in specs:
            doc = document_id_from_bytes(data)
            docs.append(doc)
            conn.execute(
                "INSERT INTO document_versions ("
                "document_id, subject_id, source_hash, identity_scheme_version, created_at"
                ") VALUES (?, ?, ?, 'stable-id-v1', ?)",
                (doc, sid, source_hash_from_bytes(data), created),
            )
        conn.commit()
        assert get_schema_version(conn) == 1
    finally:
        conn.close()

    with open_registry(db) as conn:
        migrate_connection(conn)
        assert get_schema_version(conn) == 2
        statuses = {
            r["document_id"]: r["lifecycle_status"]
            for r in conn.execute(
                "SELECT document_id, lifecycle_status FROM document_versions "
                "WHERE subject_id=?",
                (sid,),
            ).fetchall()
        }
        assert set(statuses.values()) == {"WITHDRAWN"}
        assert all(statuses[d] == "WITHDRAWN" for d in docs)
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM document_versions "
                "WHERE subject_id=? AND lifecycle_status='ACTIVE'",
                (sid,),
            ).fetchone()["c"]
            == 0
        )
        assert get_active_revision(conn, sid) is None
        rel_counts = {
            t: conn.execute(
                "SELECT COUNT(*) AS c FROM document_version_relations "
                "WHERE relation_type=?",
                (t,),
            ).fetchone()["c"]
            for t in ("supersedes", "replaces", "duplicate_of")
        }
        assert rel_counts == {"supersedes": 0, "replaces": 0, "duplicate_of": 0}

        # Test C — one migration audit event per revision; no succession.
        events = conn.execute(
            "SELECT * FROM document_lifecycle_events "
            "WHERE reason = ? ORDER BY event_id ASC",
            (MIGRATION_V2_MULTI_REASON,),
        ).fetchall()
        assert len(events) == 3
        event_docs = {e["document_id"] for e in events}
        assert event_docs == set(docs)
        for e in events:
            assert e["new_state"] == "WITHDRAWN"
            assert e["previous_state"] is None
            assert e["related_document_id"] is None
            assert e["relation_type"] is None
            assert e["reason"] == MIGRATION_V2_MULTI_REASON
            assert e["source"] == MIGRATION_V2_SOURCE
            assert e["actor"] is None

        # Ordering independence: repeat migration is no-op; statuses unchanged.
        migrate_connection(conn)
        statuses2 = {
            r["document_id"]: r["lifecycle_status"]
            for r in conn.execute(
                "SELECT document_id, lifecycle_status FROM document_versions "
                "WHERE subject_id=?",
                (sid,),
            ).fetchall()
        }
        assert statuses2 == statuses


def test_activate_revision_after_multi_migration(tmp_path: Path) -> None:
    """Test D — explicit activate_revision resolves ambiguous migrated subject."""
    db = (tmp_path / "v1_activate.sqlite3").resolve()
    conn = _build_v1_db(db)
    try:
        sid = subject_id_from_key("sms", "mig-act")
        conn.execute(
            "INSERT INTO documents (subject_id, created_at, updated_at) VALUES (?, ?, ?)",
            (sid, utc_now(), utc_now()),
        )
        a = document_id_from_bytes(b"act-A")
        b = document_id_from_bytes(b"act-B")
        for data, doc, created in (
            (b"act-A", a, "2023-01-01T00:00:00Z"),
            (b"act-B", b, "2023-06-01T00:00:00Z"),
        ):
            conn.execute(
                "INSERT INTO document_versions ("
                "document_id, subject_id, source_hash, identity_scheme_version, created_at"
                ") VALUES (?, ?, ?, 'stable-id-v1', ?)",
                (doc, sid, source_hash_from_bytes(data), created),
            )
        conn.commit()
    finally:
        conn.close()

    with open_registry(db) as conn:
        migrate_connection(conn)
        assert get_revision_status(conn, a)["lifecycle_status"] == "WITHDRAWN"
        assert get_revision_status(conn, b)["lifecycle_status"] == "WITHDRAWN"
        # General matrix still forbids WITHDRAWN → ACTIVE via set_revision_status.
        with pytest.raises(LifecycleTransitionError):
            set_revision_status(conn, document_id=b, new_state="ACTIVE")
        # Narrow explicit activation.
        with pytest.raises(RegistryValidationError, match="reason"):
            activate_revision(conn, b, reason="")
        result = activate_revision(
            conn,
            b,
            reason="operator selected current revision after v2 migration",
            source="admin",
        )
        assert result["idempotent"] is False
        assert result["document_version"]["lifecycle_status"] == "ACTIVE"
        assert get_active_revision(conn, sid)["document_id"] == b
        assert get_revision_status(conn, a)["lifecycle_status"] == "WITHDRAWN"
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM document_version_relations").fetchone()[
                "c"
            ]
            == 0
        )
        # Second ACTIVE blocked.
        with pytest.raises(RegistryConflictError, match="already has ACTIVE"):
            activate_revision(conn, a, reason="should fail")
        # Idempotent re-activate of current ACTIVE.
        again = activate_revision(conn, b, reason="noop")
        assert again["idempotent"] is True
        # Reject SUPERSEDED / DUPLICATE / ARCHIVED reactivation.
        sid2 = subject_id_from_key("sms", "mig-act-rej")
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid2)
            old = register_document_version(
                conn,
                document_id=document_id_from_bytes(b"rej-old"),
                subject_id=sid2,
                source_hash=source_hash_from_bytes(b"rej-old"),
            )
            new = register_document_version(
                conn,
                document_id=document_id_from_bytes(b"rej-new"),
                subject_id=sid2,
                source_hash=source_hash_from_bytes(b"rej-new"),
                lifecycle_status="WITHDRAWN",
            )
        supersede_revision(
            conn,
            old_document_id=old["document_id"],
            new_document_id=new["document_id"],
            reason="publish",
        )
        with pytest.raises(LifecycleTransitionError, match="WITHDRAWN"):
            activate_revision(conn, old["document_id"], reason="no")
        archive_revision(conn, old["document_id"], reason="archive")
        with pytest.raises(LifecycleTransitionError, match="WITHDRAWN"):
            activate_revision(conn, old["document_id"], reason="no")


def test_v1_to_v2_migration_preserves_data(tmp_path: Path) -> None:
    """Preservation + mixed single/multi subjects under remediated policy."""
    db = (tmp_path / "v1only.sqlite3").resolve()
    conn = _build_v1_db(db)
    try:
        sid_multi = subject_id_from_key("sms", "mig")
        sid_single = subject_id_from_key("sms", "mig-one")
        for sid in (sid_multi, sid_single):
            conn.execute(
                "INSERT INTO documents (subject_id, created_at, updated_at) VALUES (?, ?, ?)",
                (sid, utc_now(), utc_now()),
            )
        data_a = b"mig-A"
        data_b = b"mig-B"
        data_c = b"mig-C-only"
        for data, sid in ((data_a, sid_multi), (data_b, sid_multi), (data_c, sid_single)):
            doc = document_id_from_bytes(data)
            h = source_hash_from_bytes(data)
            conn.execute(
                "INSERT INTO document_versions ("
                "document_id, subject_id, source_hash, identity_scheme_version, created_at"
                ") VALUES (?, ?, ?, 'stable-id-v1', ?)",
                (doc, sid, h, utc_now()),
            )
        conn.commit()
        assert get_schema_version(conn) == 1
    finally:
        conn.close()

    with open_registry(db) as conn:
        migrate_connection(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION == 2
        n = conn.execute("SELECT COUNT(*) AS c FROM document_versions").fetchone()["c"]
        assert n == 3
        multi_active = conn.execute(
            "SELECT COUNT(*) AS c FROM document_versions "
            "WHERE subject_id=? AND lifecycle_status='ACTIVE'",
            (sid_multi,),
        ).fetchone()["c"]
        assert multi_active == 0
        single_active = conn.execute(
            "SELECT COUNT(*) AS c FROM document_versions "
            "WHERE subject_id=? AND lifecycle_status='ACTIVE'",
            (sid_single,),
        ).fetchone()["c"]
        assert single_active == 1
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM document_version_relations").fetchone()[
                "c"
            ]
            == 0
        )
        migrate_connection(conn)
        assert get_schema_version(conn) == 2
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM document_versions").fetchone()["c"]
            == 3
        )


def test_unknown_newer_schema_still_fails(tmp_path: Path) -> None:
    from rag_engine.metadata_registry import UnknownSchemaVersionError

    db = (tmp_path / "newer.sqlite3").resolve()
    initialize_registry(db)
    with open_registry(db) as conn:
        conn.execute(
            "INSERT INTO registry_schema_version "
            "(schema_version, applied_at, status) VALUES (99, 't', 'applied')"
        )
        conn.commit()
    with open_registry(db) as conn:
        with pytest.raises(UnknownSchemaVersionError):
            get_schema_version(conn)

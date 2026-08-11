"""Phase 3 metadata registry tests — temporary SQLite only."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from rag_engine.metadata_registry import (
    CURRENT_SCHEMA_VERSION,
    REQUIRED_TABLES,
    DowngradeNotAllowedError,
    RegistryConflictError,
    RegistryValidationError,
    UnknownSchemaVersionError,
    foreign_keys_enabled,
    get_schema_version,
    initialize_registry,
    open_registry,
    production_registry_path,
    register_chunk,
    register_document_lifecycle,
    register_document_version,
    register_source_file,
    register_subject,
    register_vector_mapping,
    registry_transaction,
)
from rag_engine.stable_identity import (
    IDENTITY_SCHEME_VERSION,
    chunk_id,
    chunking_fingerprint,
    content_hash,
    default_chunking_contract,
    document_id_from_bytes,
    source_hash_from_bytes,
    subject_id_from_key,
    subject_id_pending,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def registry_db(tmp_path: Path) -> Path:
    db = (tmp_path / "registry" / "metadata_registry_v1.sqlite3").resolve()
    assert ".rag_db" not in str(db)
    assert str(db).startswith(str(tmp_path.resolve()))
    initialize_registry(db)
    return db


def _fp() -> str:
    return chunking_fingerprint(default_chunking_contract())


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_init_and_version(registry_db: Path) -> None:
    with open_registry(registry_db, readonly=True) as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION == 3
        assert foreign_keys_enabled(conn)
        for table in REQUIRED_TABLES:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert row is not None


def test_repeated_initialization_safe(tmp_path: Path) -> None:
    db = (tmp_path / "r.sqlite3").resolve()
    initialize_registry(db)
    initialize_registry(db)
    with open_registry(db) as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        register_subject(conn, subject_id=subject_id_from_key("sms", "keep-me"))
        conn.commit()
    initialize_registry(db)
    with open_registry(db, readonly=True) as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
        assert n == 1


def test_unknown_newer_schema_fails(tmp_path: Path) -> None:
    db = (tmp_path / "new.sqlite3").resolve()
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


def test_downgrade_not_allowed(registry_db: Path) -> None:
    from rag_engine.metadata_registry.migrations import migrate_connection

    with open_registry(registry_db) as conn:
        with pytest.raises(DowngradeNotAllowedError):
            migrate_connection(conn, target_version=0)


def test_foreign_keys_reject_orphan_chunk(registry_db: Path) -> None:
    with open_registry(registry_db) as conn:
        assert foreign_keys_enabled(conn)
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO chunks ("
                "chunk_id, document_id, identity_scheme_version, "
                "chunking_fingerprint, chunk_ordinal, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "chunk:" + "a" * 32,
                    "docrev:" + "b" * 64,
                    IDENTITY_SCHEME_VERSION,
                    "c" * 64,
                    0,
                    "t",
                ),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Subject / revision / locator / chunk / vector
# ---------------------------------------------------------------------------


def test_subject_validation_and_idempotency(registry_db: Path) -> None:
    sid = subject_id_from_key("maker_doc", "yanmar-x")
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            a = register_subject(conn, subject_id=sid, document_type="manual")
            b = register_subject(conn, subject_id=sid, document_type="manual")
            assert a["subject_id"] == b["subject_id"] == sid
            with pytest.raises(RegistryConflictError):
                register_subject(conn, subject_id=sid, document_type="other")
            with pytest.raises(RegistryValidationError):
                register_subject(conn, subject_id="not-a-subject")


def test_document_version_invariant_and_family(registry_db: Path) -> None:
    sid = subject_id_from_key("manual_family", "pump-manual")
    data_a = b"revision-A-bytes"
    data_b = b"revision-B-bytes"
    doc_a = document_id_from_bytes(data_a)
    doc_b = document_id_from_bytes(data_b)
    ha = source_hash_from_bytes(data_a)
    hb = source_hash_from_bytes(data_b)
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            register_document_version(
                conn, document_id=doc_a, subject_id=sid, source_hash=ha
            )
            # Second revision must be staged non-ACTIVE while A remains ACTIVE.
            register_document_version(
                conn,
                document_id=doc_b,
                subject_id=sid,
                source_hash=hb,
                lifecycle_status="WITHDRAWN",
            )
            # idempotent same revision
            register_document_version(
                conn, document_id=doc_a, subject_id=sid, source_hash=ha
            )
            with pytest.raises(RegistryValidationError):
                register_document_version(
                    conn,
                    document_id=doc_a,
                    subject_id=sid,
                    source_hash=hb,  # mismatch
                )
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM document_versions WHERE subject_id=?",
                (sid,),
            ).fetchone()["c"]
            assert n == 2


def test_duplicate_copy_two_locators(registry_db: Path) -> None:
    data = b"same-bytes-dup-copy"
    doc = document_id_from_bytes(data)
    h = source_hash_from_bytes(data)
    sid = subject_id_pending(h)
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            register_document_version(
                conn, document_id=doc, subject_id=sid, source_hash=h
            )
            register_source_file(
                conn, document_id=doc, relative_path="path/A/manual.pdf", source_hash=h
            )
            register_source_file(
                conn,
                document_id=doc,
                relative_path="path/B/manual-copy.pdf",
                source_hash=h,
            )
            # idempotent locator
            register_source_file(
                conn, document_id=doc, relative_path="path/A/manual.pdf", source_hash=h
            )
            assert (
                conn.execute("SELECT COUNT(*) AS c FROM document_versions").fetchone()[
                    "c"
                ]
                == 1
            )
            assert (
                conn.execute("SELECT COUNT(*) AS c FROM source_files").fetchone()["c"]
                == 2
            )


def test_chunks_and_vector_map(registry_db: Path) -> None:
    data = b"chunked-doc"
    doc = document_id_from_bytes(data)
    h = source_hash_from_bytes(data)
    sid = subject_id_from_key("sms", "chunk-test")
    fp = _fp()
    c0 = chunk_id(doc, fp, 0)
    c1 = chunk_id(doc, fp, 1)
    legacy0 = str(uuid.uuid4())
    legacy1 = str(uuid.uuid4())
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            register_document_version(
                conn, document_id=doc, subject_id=sid, source_hash=h
            )
            register_chunk(
                conn,
                chunk_id=c0,
                document_id=doc,
                chunking_fingerprint=fp,
                ordinal=0,
                content_hash=content_hash("t0"),
            )
            register_chunk(
                conn,
                chunk_id=c1,
                document_id=doc,
                chunking_fingerprint=fp,
                ordinal=1,
                content_hash=content_hash("t1"),
            )
            register_chunk(
                conn,
                chunk_id=c0,
                document_id=doc,
                chunking_fingerprint=fp,
                ordinal=0,
            )
            with pytest.raises(RegistryValidationError):
                register_chunk(
                    conn,
                    chunk_id=c0,
                    document_id=doc,
                    chunking_fingerprint=fp,
                    ordinal=9,
                )
            register_vector_mapping(
                conn, chunk_id=c0, chroma_embedding_id=legacy0, mapping_status="legacy_uuid"
            )
            register_vector_mapping(
                conn, chunk_id=c1, chroma_embedding_id=legacy1, mapping_status="legacy_uuid"
            )
            # future native mapping on different collection
            register_vector_mapping(
                conn,
                chunk_id=c0,
                chroma_embedding_id=c0,
                physical_collection_name="future",
                mapping_status="native_chunk_id",
            )
            with pytest.raises(RegistryConflictError):
                register_vector_mapping(
                    conn,
                    chunk_id=c1,
                    chroma_embedding_id=legacy0,
                    mapping_status="legacy_uuid",
                )
            # orphan mapping
            with pytest.raises(RegistryValidationError):
                register_vector_mapping(
                    conn,
                    chunk_id="chunk:" + "f" * 32,
                    chroma_embedding_id=str(uuid.uuid4()),
                )


def test_transaction_rollback_on_bad_mapping(registry_db: Path) -> None:
    data = b"txn-doc"
    doc = document_id_from_bytes(data)
    h = source_hash_from_bytes(data)
    sid = subject_id_from_key("reg", "txn-test")
    fp = _fp()
    c0 = chunk_id(doc, fp, 0)
    with open_registry(registry_db) as conn:
        with pytest.raises(RegistryValidationError):
            register_document_lifecycle(
                conn,
                subject_id=sid,
                document_id=doc,
                source_hash=h,
                relative_path="x/y.pdf",
                chunks=[
                    {
                        "chunk_id": c0,
                        "chunking_fingerprint": fp,
                        "ordinal": 0,
                    }
                ],
                vector_mappings=[
                    {
                        "chunk_id": "chunk:" + "e" * 32,  # orphan / wrong
                        "chroma_embedding_id": str(uuid.uuid4()),
                    }
                ],
            )
        assert conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"] == 0
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM document_versions").fetchone()["c"]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"] == 0


def test_pending_subject_supported(registry_db: Path) -> None:
    h = source_hash_from_bytes(b"pending-bytes")
    sid = subject_id_pending(h)
    with open_registry(registry_db) as conn:
        with registry_transaction(conn):
            register_subject(conn, subject_id=sid)
            assert (
                conn.execute(
                    "SELECT subject_id FROM documents WHERE subject_id=?", (sid,)
                ).fetchone()["subject_id"]
                == sid
            )


def test_production_path_helper_does_not_create(tmp_path: Path) -> None:
    lib = tmp_path / "CE_Library"
    lib.mkdir()
    path = production_registry_path(lib)
    assert path.name == "metadata_registry_v1.sqlite3"
    assert ".rag_state" in str(path)
    assert not path.exists()
    assert not (lib / ".rag_state").exists()


# ---------------------------------------------------------------------------
# Import safety
# ---------------------------------------------------------------------------


def test_import_boundary_subprocess() -> None:
    script = r"""
import sys, os
from pathlib import Path
before = set(sys.modules)
import rag_engine.metadata_registry as mr
after = set(sys.modules)
loaded = sorted(after - before)
forbidden = [n for n in loaded if any(x in n.lower() for x in ('chromadb','langchain','openai'))]
print('VERSION', mr.CURRENT_SCHEMA_VERSION)
print('FORBIDDEN', ','.join(forbidden))
# ensure no .rag_state created under home CE library by import
prod = Path('/Users/vladymyrzub/CE_Library/.rag_state')
print('RAG_STATE_EXISTS', prod.exists())
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "VERSION 3" in proc.stdout
    assert "FORBIDDEN \n" in proc.stdout or proc.stdout.strip().endswith("FORBIDDEN")
    assert "RAG_STATE_EXISTS False" in proc.stdout

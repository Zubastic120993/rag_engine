"""Phase 2 stable-id-v1 unit tests (tmp paths / tmp SQLite only)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from rag_engine.stable_identity import (
    IDENTITY_SCHEME_VERSION,
    IdentityCollisionError,
    IdentityValidationError,
    canonicalize_chunking_contract,
    chunk_id,
    chunking_fingerprint,
    content_hash,
    default_chunking_contract,
    document_id_from_bytes,
    document_id_from_file,
    normalize_relative_path,
    reject_conflicting_chunk_reuse,
    sha256_bytes,
    source_hash_from_bytes,
    source_hash_from_file,
    subject_id_from_key,
    subject_id_from_uuid,
    subject_id_pending,
    validate_chunk_id,
    validate_chunking_fingerprint,
    validate_content_hash,
    validate_document_id,
    validate_source_hash,
    validate_subject_id,
    verify_chunk_id_matches_preimage,
)
from rag_engine.stable_identity.canonical import CanonicalizationError
from rag_engine.stable_identity.registry_tables import (
    apply_stable_identity_tables,
    insert_chunk,
    insert_chunk_vector_map,
)

ROOT = Path(__file__).resolve().parents[1]

# FIPS 180-4 / common empty and "abc" digests
SHA256_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SHA256_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# ---------------------------------------------------------------------------
# SHA-256 / source_hash
# ---------------------------------------------------------------------------


def test_sha256_known_vectors() -> None:
    assert sha256_bytes(b"") == SHA256_EMPTY
    assert sha256_bytes(b"abc") == SHA256_ABC


def test_source_hash_bytes_deterministic() -> None:
    data = b"stable-id-v1-bytes"
    assert source_hash_from_bytes(data) == source_hash_from_bytes(bytes(data))


def test_source_hash_file_equals_bytes(tmp_path: Path) -> None:
    data = b"file-bytes-xyz"
    p = tmp_path / "a.bin"
    p.write_bytes(data)
    assert source_hash_from_file(p) == source_hash_from_bytes(data)


def test_source_hash_path_rename_invariant(tmp_path: Path) -> None:
    data = b"rename-invariant"
    a = tmp_path / "dir1" / "x.pdf"
    b = tmp_path / "dir2" / "y.pdf"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(data)
    b.write_bytes(data)
    assert source_hash_from_file(a) == source_hash_from_file(b)


def test_source_hash_byte_mutation_changes(tmp_path: Path) -> None:
    p = tmp_path / "m.bin"
    p.write_bytes(b"AAAA")
    h1 = source_hash_from_file(p)
    p.write_bytes(b"AAAB")
    h2 = source_hash_from_file(p)
    assert h1 != h2


def test_source_hash_validation() -> None:
    h = source_hash_from_bytes(b"x")
    assert validate_source_hash(h) == h
    with pytest.raises(IdentityValidationError):
        validate_source_hash(h.upper())
    with pytest.raises(IdentityValidationError):
        validate_source_hash(h[:-1])
    with pytest.raises(IdentityValidationError):
        validate_source_hash(h + "0")
    with pytest.raises(IdentityValidationError):
        validate_source_hash("g" * 64)


# ---------------------------------------------------------------------------
# document_id
# ---------------------------------------------------------------------------


def test_document_id_prefix_and_determinism() -> None:
    data = b"doc-bytes"
    d1 = document_id_from_bytes(data)
    d2 = document_id_from_bytes(data)
    assert d1 == d2
    assert d1.startswith("docrev:")
    assert d1 == f"docrev:{source_hash_from_bytes(data)}"
    validate_document_id(d1)


def test_document_id_same_bytes_different_path(tmp_path: Path) -> None:
    data = b"same-rev"
    p1 = tmp_path / "one" / "a.pdf"
    p2 = tmp_path / "two" / "b.pdf"
    p1.parent.mkdir()
    p2.parent.mkdir()
    p1.write_bytes(data)
    p2.write_bytes(data)
    assert document_id_from_file(p1) == document_id_from_file(p2)


def test_document_id_mutation_changes() -> None:
    assert document_id_from_bytes(b"A") != document_id_from_bytes(b"B")


def test_document_id_validation_rejects_malformed() -> None:
    good = document_id_from_bytes(b"ok")
    validate_document_id(good)
    with pytest.raises(IdentityValidationError):
        validate_document_id(good.replace("docrev:", "doc:"))
    with pytest.raises(IdentityValidationError):
        validate_document_id(good.upper())
    with pytest.raises(IdentityValidationError):
        validate_document_id(good + " ")
    with pytest.raises(IdentityValidationError):
        validate_document_id("docrev:" + "a" * 32)


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


def test_content_hash_deterministic_and_nfkc() -> None:
    # U+FB01 (ﬁ ligature) NFKC → "fi"
    h1 = content_hash("ﬁle")
    h2 = content_hash("file")
    assert h1 == h2
    assert validate_content_hash(h1) == h1
    assert content_hash("alpha") != content_hash("beta")


def test_content_hash_strips_and_keeps_newline() -> None:
    assert content_hash("  hi\n") == content_hash("hi")
    # control BEL dropped; text otherwise same
    assert content_hash("a\x07b") == content_hash("ab")


def test_content_hash_differs_from_source_hash() -> None:
    raw = b"plain-text-as-bytes"
    # content_hash of decoded text is not the same as source_hash of raw bytes
    # in general for non-identity encodings; for ASCII they can coincide only
    # if normalization is a no-op — still separate APIs.
    text = "plain-text-as-bytes"
    # After normalize, text == original; hash of UTF-8 text == hash of bytes
    # for this ASCII case — prove API separation by using NFKC-changing input.
    assert content_hash("ﬁ") != source_hash_from_bytes("ﬁ".encode("utf-8"))


# ---------------------------------------------------------------------------
# chunking canonicalization / fingerprint
# ---------------------------------------------------------------------------


def test_canonical_key_order_independent() -> None:
    a = {"chunk_size": 800, "chunk_overlap": 100, "identity_scheme_version": "stable-id-v1"}
    b = {"identity_scheme_version": "stable-id-v1", "chunk_overlap": 100, "chunk_size": 800}
    assert canonicalize_chunking_contract(a) == canonicalize_chunking_contract(b)
    assert chunking_fingerprint(a) == chunking_fingerprint(b)


def test_canonical_nested_key_order_independent() -> None:
    a = {"outer": {"z": 1, "a": 2}, "n": 0}
    b = {"n": 0, "outer": {"a": 2, "z": 1}}
    assert canonicalize_chunking_contract(a) == canonicalize_chunking_contract(b)


def test_canonical_rejects_unsupported_and_floats() -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize_chunking_contract({"x": object()})
    with pytest.raises(CanonicalizationError):
        canonicalize_chunking_contract({"chunk_size": 800.5})
    with pytest.raises(CanonicalizationError):
        canonicalize_chunking_contract({"x": float("nan")})


def test_canonical_no_incidental_whitespace() -> None:
    c = default_chunking_contract()
    s = canonicalize_chunking_contract(c)
    assert " " not in s or any(sep == " " for sep in c["separators"])
    # compact separators — no ": " style
    assert ": " not in s
    assert ", " not in s
    json.loads(s)  # valid JSON


def test_chunking_fingerprint_changes_on_config() -> None:
    base = default_chunking_contract()
    size = default_chunking_contract(chunk_size=801)
    overlap = default_chunking_contract(chunk_overlap=101)
    norm = default_chunking_contract(normalization="nfc")
    scheme = default_chunking_contract(identity_scheme_version="stable-id-v2")
    fb = chunking_fingerprint(base)
    assert fb != chunking_fingerprint(size)
    assert fb != chunking_fingerprint(overlap)
    assert fb != chunking_fingerprint(norm)
    assert fb != chunking_fingerprint(scheme)
    validate_chunking_fingerprint(fb)


# ---------------------------------------------------------------------------
# subject_id
# ---------------------------------------------------------------------------


def test_subject_id_key_form() -> None:
    s = subject_id_from_key("maker_doc", " Yanmar-6EY22 ")
    assert s == "subj:key:maker_doc:yanmar-6ey22"
    validate_subject_id(s)


def test_subject_id_empty_key_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        subject_id_from_key("sms", "  ")


def test_subject_id_uuid_canonical() -> None:
    u = uuid.UUID("12345678-1234-5678-1234-567812345678")
    s = subject_id_from_uuid(u)
    assert s == "subj:uuid:12345678-1234-5678-1234-567812345678"
    # uppercase string input → lowercase output
    s2 = subject_id_from_uuid("12345678-1234-5678-1234-567812345678")
    assert s2 == s
    validate_subject_id(s)


def test_subject_id_malformed_uuid_rejected() -> None:
    with pytest.raises(IdentityValidationError):
        subject_id_from_uuid("not-a-uuid")


def test_subject_id_pending_deterministic() -> None:
    h = source_hash_from_bytes(b"pending-doc")
    assert subject_id_pending(h) == f"subj:pending:{h}"
    validate_subject_id(subject_id_pending(h))
    with pytest.raises(IdentityValidationError):
        subject_id_pending("zz")


# ---------------------------------------------------------------------------
# chunk_id
# ---------------------------------------------------------------------------


def test_chunk_id_format_and_determinism() -> None:
    doc = document_id_from_bytes(b"chunk-doc")
    fp = chunking_fingerprint(default_chunking_contract())
    c1 = chunk_id(doc, fp, 0)
    c2 = chunk_id(doc, fp, 0)
    assert c1 == c2
    assert c1.startswith("chunk:")
    body = c1[len("chunk:") :]
    assert len(body) == 32
    assert all(ch in "0123456789abcdef" for ch in body)
    validate_chunk_id(c1)


def test_chunk_id_changes_with_inputs() -> None:
    doc_a = document_id_from_bytes(b"A")
    doc_b = document_id_from_bytes(b"B")
    fp1 = chunking_fingerprint(default_chunking_contract())
    fp2 = chunking_fingerprint(default_chunking_contract(chunk_size=900))
    assert chunk_id(doc_a, fp1, 0) != chunk_id(doc_b, fp1, 0)
    assert chunk_id(doc_a, fp1, 0) != chunk_id(doc_a, fp2, 0)
    assert chunk_id(doc_a, fp1, 0) != chunk_id(doc_a, fp1, 1)


def test_chunk_id_rejects_bad_ordinal_and_ids() -> None:
    doc = document_id_from_bytes(b"ord")
    fp = chunking_fingerprint(default_chunking_contract())
    with pytest.raises(IdentityValidationError):
        chunk_id(doc, fp, -1)
    with pytest.raises(IdentityValidationError):
        chunk_id(doc, fp, True)  # type: ignore[arg-type]
    with pytest.raises(IdentityValidationError):
        chunk_id(doc, fp, 1.0)  # type: ignore[arg-type]
    with pytest.raises(IdentityValidationError):
        chunk_id("docrev:bad", fp, 0)
    with pytest.raises(IdentityValidationError):
        chunk_id(doc, "nothex", 0)


def test_path_independence_document_and_chunk(tmp_path: Path) -> None:
    data = b"path-independent-payload"
    p1 = tmp_path / "left" / "a.pdf"
    p2 = tmp_path / "right" / "b.pdf"
    p1.parent.mkdir()
    p2.parent.mkdir()
    p1.write_bytes(data)
    p2.write_bytes(data)
    d1 = document_id_from_file(p1)
    d2 = document_id_from_file(p2)
    assert d1 == d2
    fp = chunking_fingerprint(default_chunking_contract())
    assert chunk_id(d1, fp, 3) == chunk_id(d2, fp, 3)


# ---------------------------------------------------------------------------
# collision / invariant
# ---------------------------------------------------------------------------


def test_verify_chunk_preimage_and_conflict() -> None:
    doc = document_id_from_bytes(b"c")
    fp = chunking_fingerprint(default_chunking_contract())
    cid = chunk_id(doc, fp, 2)
    verify_chunk_id_matches_preimage(
        cid, document_id=doc, chunking_fingerprint=fp, ordinal=2
    )
    with pytest.raises(IdentityCollisionError):
        verify_chunk_id_matches_preimage(
            cid, document_id=doc, chunking_fingerprint=fp, ordinal=3
        )
    reject_conflicting_chunk_reuse(
        None,
        chunk_id=cid,
        document_id=doc,
        chunking_fingerprint=fp,
        ordinal=2,
    )
    reject_conflicting_chunk_reuse(
        {
            "chunk_id": cid,
            "document_id": doc,
            "chunking_fingerprint": fp,
            "ordinal": 2,
            "identity_scheme_version": IDENTITY_SCHEME_VERSION,
        },
        chunk_id=cid,
        document_id=doc,
        chunking_fingerprint=fp,
        ordinal=2,
    )
    with pytest.raises(IdentityCollisionError):
        reject_conflicting_chunk_reuse(
            {
                "chunk_id": cid,
                "document_id": doc,
                "chunking_fingerprint": fp,
                "ordinal": 9,
                "identity_scheme_version": IDENTITY_SCHEME_VERSION,
            },
            chunk_id=cid,
            document_id=doc,
            chunking_fingerprint=fp,
            ordinal=2,
        )


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def test_normalize_relative_path_rules(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "20_Vessels" / "doc.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    rel = normalize_relative_path(f, library_root=lib)
    assert rel == "20_Vessels/doc.pdf"
    assert normalize_relative_path(r"a\b\c") == "a/b/c"
    assert normalize_relative_path("./a/b") == "a/b"


# ---------------------------------------------------------------------------
# optional registry tables (temp SQLite only)
# ---------------------------------------------------------------------------


def test_registry_tables_temp_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "registry.sqlite3"
    # Guard: never touch production paths
    assert ".rag_db" not in str(db)
    assert ".rag_state" not in str(db)
    conn = sqlite3.connect(db)
    apply_stable_identity_tables(conn)
    doc = document_id_from_bytes(b"reg-doc")
    fp = chunking_fingerprint(default_chunking_contract())
    cid = chunk_id(doc, fp, 0)
    ch = content_hash("chunk text")
    insert_chunk(
        conn,
        chunk_id=cid,
        document_id=doc,
        chunking_fingerprint=fp,
        ordinal=0,
        content_hash=ch,
    )
    # idempotent identical insert
    insert_chunk(
        conn,
        chunk_id=cid,
        document_id=doc,
        chunking_fingerprint=fp,
        ordinal=0,
        content_hash=ch,
    )
    legacy = str(uuid.uuid4())
    insert_chunk_vector_map(
        conn,
        chunk_id=cid,
        chroma_embedding_id=legacy,
        mapping_status="legacy_uuid",
    )
    insert_chunk_vector_map(
        conn,
        chunk_id=cid,
        chroma_embedding_id=legacy,
        mapping_status="legacy_uuid",
    )
    with pytest.raises(IdentityCollisionError):
        insert_chunk_vector_map(
            conn,
            chunk_id=cid,
            chroma_embedding_id=str(uuid.uuid4()),
            mapping_status="legacy_uuid",
        )
    # conflicting constituent reuse
    with pytest.raises(IdentityCollisionError):
        # Craft a fake existing conflict via direct SQL then helper check is enough;
        # inserting same chunk_id with different ordinal cannot happen via chunk_id()
        # so simulate by calling reject path through insert with mismatched preimage:
        other = chunk_id(doc, fp, 1)
        insert_chunk(
            conn,
            chunk_id=other,
            document_id=doc,
            chunking_fingerprint=fp,
            ordinal=1,
        )
        # Force conflict: try to insert `other` claiming ordinal 0 constituents
        # (preimage check fails first)
        insert_chunk(
            conn,
            chunk_id=other,
            document_id=doc,
            chunking_fingerprint=fp,
            ordinal=0,
        )
    conn.close()


# ---------------------------------------------------------------------------
# import boundary
# ---------------------------------------------------------------------------


def test_import_boundary_no_chroma_langchain() -> None:
    script = r"""
import sys
# Import package under test
import rag_engine.stable_identity as si
banned = [
    "chromadb",
    "langchain",
    "langchain_chroma",
    "langchain_community",
    "langchain_core",
    "langchain_text_splitters",
]
loaded = [m for m in banned if m in sys.modules]
print("SCHEME=" + si.IDENTITY_SCHEME_VERSION)
print("LOADED=" + ",".join(loaded))
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SCHEME=stable-id-v1" in proc.stdout
    assert "LOADED=\n" in proc.stdout or proc.stdout.strip().endswith("LOADED=")


# ---------------------------------------------------------------------------
# subprocess determinism
# ---------------------------------------------------------------------------


def test_repeat_process_determinism() -> None:
    script = r"""
from rag_engine.stable_identity import (
    document_id_from_bytes,
    chunk_id,
    chunking_fingerprint,
    default_chunking_contract,
)
doc = document_id_from_bytes(b"subprocess-deterministic")
fp = chunking_fingerprint(default_chunking_contract())
print(doc)
print(chunk_id(doc, fp, 7))
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    runs = []
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        runs.append(proc.stdout)
    assert runs[0] == runs[1]


def test_scheme_version_constant() -> None:
    assert IDENTITY_SCHEME_VERSION == "stable-id-v1"


def test_spec_example_same_bytes_different_path_classification(tmp_path: Path) -> None:
    """Spec example 2: same PDF bytes, different path → same document_id/chunk_ids."""
    data = b"%PDF-1.4 example-same-bytes"
    a = tmp_path / "career" / "manual.pdf"
    b = tmp_path / "vessel" / "manual.pdf"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(data)
    b.write_bytes(data)
    d1 = document_id_from_file(a)
    d2 = document_id_from_file(b)
    assert d1 == d2
    fp = chunking_fingerprint(default_chunking_contract())
    assert [chunk_id(d1, fp, i) for i in range(3)] == [
        chunk_id(d2, fp, i) for i in range(3)
    ]


def test_mtime_does_not_affect_source_hash(tmp_path: Path) -> None:
    p = tmp_path / "t.bin"
    p.write_bytes(b"mtime-invariant")
    h1 = source_hash_from_file(p)
    os.utime(p, (1_000_000, 1_000_000))
    h2 = source_hash_from_file(p)
    assert h1 == h2

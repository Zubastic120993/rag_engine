#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import uuid
import importlib.metadata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "lc6-operational-controls-v2"
INVENTORY_ROW_SCHEMA_V1 = "inventory-row-v1"
SOURCE_CONTENT_HASH_SCHEMA_V1 = "source-content-hash-v1"
CANDIDATE_MARKER = ".lc6_operational_candidate_v1.json"
SEALED_DISPOSABLE_MARKER = ".orphan_repair_disposable_clone.json"
BACKUP_JOURNAL = "lc6_backup_journal_v2.json"
RESTORE_JOURNAL = "lc6_restore_verify_journal_v2.json"
CANDIDATE_JOURNAL = "lc6_candidate_create_journal_v2.json"
REPAIR_JOURNAL = "lc6_candidate_repair_wrapper_journal_v2.json"
POSTCHECK_JOURNAL = "lc6_candidate_postcheck_journal_v2.json"
SWITCH_JOURNAL = "lc6_selection_switch_journal_v2.json"
ROLLBACK_JOURNAL = "lc6_selection_rollback_journal_v2.json"
AUTHORIZED_RETIRED_DIGEST = "247b28a9ff07169473ddb7ac5f54dc61bfac046796bd7d0b36249fa36e166c90"
AUTHORIZED_RETIRED_VECTOR_IDS = {
    "chunk:7cb9d07e312b2e6080aa112f5a165428",
    "chunk:9c1743c71b1b7c2e9d9aea88d3fbc247",
}
EXPECTED_POST_MUTATION_CHANGED_FILES = {
    "chroma.sqlite3",
    "embedded.json",
    "abda5535-6e53-4124-b725-55046ffb0347/data_level0.bin",
    "abda5535-6e53-4124-b725-55046ffb0347/header.bin",
    "abda5535-6e53-4124-b725-55046ffb0347/index_metadata.pickle",
    "abda5535-6e53-4124-b725-55046ffb0347/length.bin",
    "abda5535-6e53-4124-b725-55046ffb0347/link_lists.bin",
}

DEFAULT_LIBRARY_ROOT = Path("/Users/vladymyrzub/CE_Library")
SEALED_PACKAGE = Path("/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z")
SEALED_SHA256SUMS_HASH = "b9307864fc2dd9eed6778d6028fc95ca8c19f1576d5e5dacee1a772d24e5cd5d"
SEALED_EXECUTOR = SEALED_PACKAGE / "orphan_repair_executor.py"
CERTIFIED_EXTERNAL_BASELINE = Path("/Users/vladymyrzub/LC6_Disposable_Baselines/lc4_restored_baseline_20260904T135023Z")
EXPECTED_PRODUCTION_INVENTORY_V1 = "b189d622935faf6a0aa14830a80fa73af85c49f2d965bb83cdfb23acdb4f992a"
CERTIFIED_BASELINE_MARKER = ".orphan_repair_disposable_clone.json"
EXPECTED_CERTIFIED_BASELINE_CONTENT_ROWS = 19
EXPECTED_CERTIFIED_BASELINE_CONTENT_INVENTORY_V1 = EXPECTED_PRODUCTION_INVENTORY_V1
EXPECTED_CERTIFIED_BASELINE_MARKER_SHA256 = "e148d9b83bbcff31d24f85ad461387457efc994138ff62003aafc2733284d2f1"
EXCLUDED_ARTIFACT_DIRS = {"__pycache__"}
EXCLUDED_ARTIFACT_SUFFIXES = {".pyc"}
TRANSIENT_GENERATION_FILES = {"ingest.lock", "chroma.sqlite3-wal", "chroma.sqlite3-shm"}
SOURCE_CONTENT_CONTROL_ARTIFACTS = {SEALED_DISPOSABLE_MARKER, CANDIDATE_MARKER}
EMBEDDING_DIGEST_VERSION = "lc6-embedding-payload-digest-v2"
EMBEDDING_COLLECTION_PROVIDER = None
AUTHORITATIVE_RUNTIME_PYTHON = Path("/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/python")
FORBIDDEN_RUNTIME_PYTHONS = {Path("/Users/vladymyrzub/CE_Library/Tools/rag_engine/.venv/bin/python")}
DEFAULT_RUNTIME_PYTHON = AUTHORITATIVE_RUNTIME_PYTHON
REQUIRED_RUNTIME_MODULES = ("chromadb",)


class ControlRefusal(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolved(p: str | Path) -> Path:
    return Path(p).expanduser().resolve()


def lexical_absolute(p: str | Path) -> Path:
    """Return an absolute path without dereferencing a venv launcher symlink."""
    pp = Path(p).expanduser()
    if pp.is_absolute():
        return pp
    return Path(os.path.abspath(os.fspath(pp)))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_inventory_schema(schema: Any, *, allow_legacy: bool = False) -> str:
    if schema == INVENTORY_ROW_SCHEMA_V1:
        return INVENTORY_ROW_SCHEMA_V1
    if allow_legacy and schema == "legacy-no-type-v0":
        return "legacy-no-type-v0"
    if schema is None:
        raise ControlRefusal("inventory schema missing/null")
    if not isinstance(schema, str):
        raise ControlRefusal(f"inventory schema must be string, got {type(schema).__name__}")
    if not schema.strip():
        raise ControlRefusal("inventory schema empty")
    raise ControlRefusal(f"unknown inventory schema: {schema}")


def artifact_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_ARTIFACT_DIRS for part in path.parts) or path.suffix in EXCLUDED_ARTIFACT_SUFFIXES


def artifact_inventory_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel_path = p.relative_to(root)
        if artifact_excluded(rel_path):
            continue
        rows.append({"path": rel_path.as_posix(), "type": "file", "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    return rows


def inventory_rows(root: Path, *, schema: str = INVENTORY_ROW_SCHEMA_V1, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    validate_inventory_schema(schema)
    if not root.is_dir():
        raise ControlRefusal(f"directory missing: {root}")
    exclude = exclude or set()
    rows: list[dict[str, Any]] = []
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = p.relative_to(root).as_posix()
        if rel in exclude:
            continue
        rows.append({"path": rel, "type": "file", "bytes": p.stat().st_size, "sha256": sha256_file(p)})
    return rows


def inventory_digest(rows: list[dict[str, Any]], *, schema: str = INVENTORY_ROW_SCHEMA_V1) -> str:
    validate_inventory_schema(schema)
    for row in rows:
        if set(row) != {"path", "type", "bytes", "sha256"}:
            raise ControlRefusal(f"inventory row has invalid fields: {sorted(row)}")
        if row["type"] != "file" or not isinstance(row["path"], str) or not row["path"]:
            raise ControlRefusal(f"invalid inventory row: {row}")
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


def source_content_hash_report(root: Path) -> dict[str, Any]:
    """Return versioned source-content hashes with marker artifacts separate.

    Only the explicitly named operational marker artifacts are excluded from
    source content. Unknown files, including unknown dotfiles, remain in the
    source-content hash set and therefore fail closed if they appear/disappear.
    Journals are outside the candidate tree and are not part of this function.
    """
    root = resolved(root)
    rows = inventory_rows(root, exclude=SOURCE_CONTENT_CONTROL_ARTIFACTS)
    file_hashes = {r["path"]: r["sha256"] for r in rows}
    marker_artifacts: dict[str, Any] = {}
    for rel in sorted(SOURCE_CONTENT_CONTROL_ARTIFACTS):
        path = root / rel
        if path.is_file():
            identity: Any
            try:
                identity = read_json(path)
            except Exception as exc:  # noqa: BLE001 - preserve malformed marker evidence
                identity = {"unreadable_json": type(exc).__name__, "error": str(exc)}
            marker_artifacts[rel] = {"path": rel, "sha256": sha256_file(path), "schema": identity.get("schema") if isinstance(identity, dict) else None, "identity": identity}
    return {
        "schema": SOURCE_CONTENT_HASH_SCHEMA_V1,
        "content_inventory_schema": INVENTORY_ROW_SCHEMA_V1,
        "excluded_control_artifacts": sorted(SOURCE_CONTENT_CONTROL_ARTIFACTS),
        "journal_location": "outside-candidate-tree",
        "content_file_count": len(rows),
        "content_inventory_sha256": inventory_digest(rows),
        "content_file_hashes": file_hashes,
        "marker_artifacts": marker_artifacts,
    }


def source_content_hash_diff(expected: dict[str, str], actual: dict[str, str]) -> dict[str, Any]:
    expected_paths = set(expected)
    actual_paths = set(actual)
    changed = sorted(p for p in expected_paths & actual_paths if expected[p] != actual[p])
    return {
        "expected_only": sorted(expected_paths - actual_paths),
        "actual_only": sorted(actual_paths - expected_paths),
        "changed_paths": [{"path": p, "expected": expected[p], "actual": actual[p]} for p in changed],
        "identical_count": len([p for p in expected_paths & actual_paths if expected[p] == actual[p]]),
    }


def assert_source_content_match(expected: dict[str, str], actual: dict[str, str], *, context: str) -> dict[str, Any]:
    diff = source_content_hash_diff(expected, actual)
    if diff["expected_only"] or diff["actual_only"] or diff["changed_paths"]:
        raise ControlRefusal(f"{context} source-content hash mismatch: {json.dumps(diff, sort_keys=True)}")
    return diff


def certified_baseline_content_inventory(baseline: Path) -> dict[str, Any]:
    """Return certified-baseline content inventory with marker separate.

    The sealed disposable-clone marker is governance evidence, not source
    content. It is excluded from the canonical source-content inventory while
    every other file remains mandatory. Any other omission must fail closed.
    """
    baseline = resolved(baseline)
    marker_path = baseline / CERTIFIED_BASELINE_MARKER
    if not marker_path.is_file():
        raise ControlRefusal(f"certified baseline marker missing: {marker_path}")
    marker_sha = sha256_file(marker_path)
    rows = inventory_rows(baseline, exclude={CERTIFIED_BASELINE_MARKER})
    omitted = sorted(
        p.relative_to(baseline).as_posix()
        for p in baseline.rglob("*")
        if p.is_file() and p.relative_to(baseline).as_posix() not in {CERTIFIED_BASELINE_MARKER, *(r["path"] for r in rows)}
    )
    if omitted:
        raise ControlRefusal(f"certified baseline inventory omitted unexpected files: {omitted}")
    digest = inventory_digest(rows)
    return {
        "schema": INVENTORY_ROW_SCHEMA_V1,
        "content_file_count": len(rows),
        "content_byte_size": sum(r["bytes"] for r in rows),
        "content_inventory_sha256": digest,
        "content_file_hashes": {r["path"]: r["sha256"] for r in rows},
        "marker_path": CERTIFIED_BASELINE_MARKER,
        "marker_sha256": marker_sha,
        "marker_identity": read_json(marker_path),
    }


def assert_certified_baseline_content_inventory(report: dict[str, Any]) -> None:
    if report["content_file_count"] != EXPECTED_CERTIFIED_BASELINE_CONTENT_ROWS:
        raise ControlRefusal(f"certified baseline content row count mismatch: {report['content_file_count']}")
    if report["content_inventory_sha256"] != EXPECTED_CERTIFIED_BASELINE_CONTENT_INVENTORY_V1:
        raise ControlRefusal(f"certified baseline content inventory checksum mismatch: {report['content_inventory_sha256']}")
    if report["marker_sha256"] != EXPECTED_CERTIFIED_BASELINE_MARKER_SHA256:
        raise ControlRefusal(f"certified baseline marker checksum mismatch: {report['marker_sha256']}")


def active_generation() -> Path:
    env = os.environ.get("RAG_DB_PATH")
    if env:
        return resolved(env)
    return resolved(library_root() / ".rag_db")


def library_root() -> Path:
    return resolved(os.environ.get("CE_LIBRARY_ROOT") or DEFAULT_LIBRARY_ROOT)


def generations_root(lib: Path | None = None) -> Path:
    lib = lib or library_root()
    return resolved(lib / ".rag_db_generations")


def wal_shm_state(gen: Path) -> dict[str, bool]:
    return {"wal": (gen / "chroma.sqlite3-wal").exists(), "shm": (gen / "chroma.sqlite3-shm").exists(), "ingest_lock": (gen / "ingest.lock").exists()}


def assert_not_forbidden_destination(dst: Path, *, allow_existing: bool = False) -> None:
    dst = resolved(dst)
    prod = active_generation()
    lib = library_root()
    forbidden_exact = {prod, lib / ".rag_db", generations_root(lib), lib, SEALED_PACKAGE, CERTIFIED_EXTERNAL_BASELINE}
    if dst in {resolved(x) for x in forbidden_exact}:
        raise ControlRefusal(f"forbidden destination: {dst}")
    if SEALED_PACKAGE in [dst, *dst.parents]:
        raise ControlRefusal(f"destination inside sealed package: {dst}")
    if lib in dst.parents:
        raise ControlRefusal(f"destination inside CE Library/source-document territory: {dst}")
    if prod in dst.parents or generations_root(lib) in dst.parents:
        raise ControlRefusal(f"destination inside production/generations root: {dst}")
    if dst.exists() and not allow_existing:
        raise ControlRefusal(f"destination already exists: {dst}")


def assert_mutation_path_under_private_tmp(path: Path) -> None:
    rp = resolved(path)
    if Path("/private/tmp") not in [rp, *rp.parents]:
        raise ControlRefusal(f"mutation test path must be under /private/tmp: {rp}")


def resolve_runtime_python(runtime_python: str | Path | None = None, *, authoritative_runtime: str | Path | None = None, allow_separate: bool = False) -> Path:
    candidate = runtime_python or os.environ.get("LC6_RUNTIME_PYTHON") or DEFAULT_RUNTIME_PYTHON
    rp = lexical_absolute(candidate)
    authoritative = lexical_absolute(authoritative_runtime or AUTHORITATIVE_RUNTIME_PYTHON)
    forbidden = {lexical_absolute(p) for p in FORBIDDEN_RUNTIME_PYTHONS}
    if rp in forbidden:
        raise ControlRefusal(f"forbidden runtime python: {rp}")
    if rp != authoritative and not allow_separate:
        raise ControlRefusal(f"runtime python differs from sealed LC6 authoritative runtime: {rp} != {authoritative}")
    if not rp.is_file() or not os.access(rp, os.X_OK):
        raise ControlRefusal(f"runtime python is not executable: {rp}")
    return rp


def validate_runtime_python(runtime_python: str | Path | None = None, *, required_modules: tuple[str, ...] = REQUIRED_RUNTIME_MODULES, authoritative_runtime: str | Path | None = None, allow_separate: bool = False) -> dict[str, Any]:
    """Validate the interpreter used for Batch B2 Chroma and executor work.

    This is a preflight gate and must run before any backup/restore/candidate
    directory is created. It does not install packages. It proves importability
    and records module versions through the selected interpreter itself.
    """
    rp = resolve_runtime_python(runtime_python, authoritative_runtime=authoritative_runtime, allow_separate=allow_separate)
    script = """
import importlib, importlib.metadata, json, os, sys
mods = {}
for name in MODULES:
    importlib.import_module(name)
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        version = 'not specified'
    mods[name] = version
print(json.dumps({'executable': sys.executable, 'symlink_real_executable_target': os.path.realpath(sys.executable), 'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'is_virtual_environment': sys.prefix != sys.base_prefix, 'version': sys.version, 'modules': mods}, sort_keys=True))
""".replace("MODULES", repr(list(required_modules)))
    proc = subprocess.run([str(rp), "-c", script], text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise ControlRefusal(f"runtime python dependency preflight failed for {rp}: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        payload = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise ControlRefusal(f"runtime python preflight returned invalid JSON: {proc.stdout!r}") from exc
    payload["status"] = "RUNTIME_PREFLIGHT_OK"
    payload["requested_executable"] = str(rp)
    payload["requested_lexical_executable"] = str(rp)
    payload["launcher_realpath"] = os.path.realpath(os.fspath(rp))
    payload["authoritative_runtime"] = str(lexical_absolute(authoritative_runtime or AUTHORITATIVE_RUNTIME_PYTHON))
    payload["sys_executable"] = payload.get("executable")
    payload["sys_prefix"] = payload.get("prefix")
    payload["sys_base_prefix"] = payload.get("base_prefix")
    expected_prefix = str(lexical_absolute(authoritative_runtime or AUTHORITATIVE_RUNTIME_PYTHON).parents[1])
    if not payload.get("is_virtual_environment"):
        raise ControlRefusal(f"runtime python is not executing inside a virtual environment: {rp}")
    if os.path.realpath(str(payload.get("prefix"))) != os.path.realpath(expected_prefix):
        raise ControlRefusal(f"runtime python prefix mismatch: {payload.get('prefix')} != {expected_prefix}")
    return payload


def assert_same_runtime(runtime_report: dict[str, Any], *, current_python: str | Path | None = None, allow_separate: bool = False) -> None:
    current = str(lexical_absolute(current_python or sys.executable))
    selected = str(lexical_absolute(runtime_report.get("requested_lexical_executable") or runtime_report["requested_executable"]))
    if current != selected and not allow_separate:
        raise ControlRefusal(f"subprocess/runtime mismatch: current={current} selected={selected}")


def runtime_preflight_for_batch_b2(runtime_python: str | Path | None = None, *, journal: Path | None = None, current_python: str | Path | None = None, allow_separate: bool = False, authoritative_runtime: str | Path | None = None) -> dict[str, Any]:
    report = validate_runtime_python(runtime_python, authoritative_runtime=authoritative_runtime, allow_separate=allow_separate)
    assert_same_runtime(report, current_python=current_python, allow_separate=allow_separate)
    report.update({"artifact": "batch_b2_runtime_preflight", "created_utc": utc_now(), "allow_separate_runtime": allow_separate})
    if journal is not None:
        write_json_atomic(resolved(journal), report)
    return report


def normalize_selection_snapshot(selection: dict[str, Any]) -> dict[str, Any]:
    """Normalize selection evidence without discarding unknown non-null fields."""
    known = {"CE_LIBRARY_ROOT", "RAG_DB_PATH", "mechanism", "active_generation"}
    rag_db_path = selection.get("RAG_DB_PATH")
    active = selection.get("active_generation") or rag_db_path
    normalized = {
        "schema": "selection-snapshot-v1",
        "CE_LIBRARY_ROOT": selection.get("CE_LIBRARY_ROOT"),
        "RAG_DB_PATH": rag_db_path,
        "mechanism": selection.get("mechanism"),
        "active_generation": active,
        "extra_fields": {k: v for k, v in sorted(selection.items()) if k not in known and v is not None},
    }
    return normalized


def compare_selection_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    nb = normalize_selection_snapshot(before)
    na = normalize_selection_snapshot(after)
    return {"schema": "selection-comparison-v1", "before_normalized": nb, "after_normalized": na, "match": nb == na}


@contextmanager
def synthetic_ingest_lock(gen: Path, acquire: bool):
    if not acquire:
        yield {"acquired": False, "path": str(gen / "ingest.lock")}
        return
    assert_mutation_path_under_private_tmp(gen)
    path = gen / "ingest.lock"
    fd = None
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode())
        yield {"acquired": True, "path": str(path)}
    finally:
        if fd is not None:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def sqlite_structural_checks(gen: Path) -> dict[str, Any]:
    db = gen / "chroma.sqlite3"
    if not db.is_file():
        return {"ok": False, "error": "chroma.sqlite3 missing"}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = sorted(r[0] for r in conn.execute("select name from sqlite_master where type='table'").fetchall())
        out: dict[str, Any] = {"ok": True, "tables": tables}
        for t in ("embeddings", "embedding_metadata", "collections", "embedding_fulltext_search"):
            if t in tables:
                out[f"{t}_count"] = int(conn.execute(f"select count(*) from {t}").fetchone()[0])
        if "collections" in tables:
            cols = [r[1] for r in conn.execute("pragma table_info(collections)").fetchall()]
            if "name" in cols:
                out["collection_names"] = sorted(str(r[0]) for r in conn.execute("select name from collections").fetchall())
        return out
    finally:
        conn.close()


def _assert_chroma_public_api_allowed(gen: Path) -> None:
    gen = resolved(gen)
    if _is_protected_generation_for_chroma(gen):
        raise ControlRefusal(f"Chroma public API embedding read refused for protected generation: {gen}")
    assert_mutation_path_under_private_tmp(gen)


def _is_protected_generation_for_chroma(gen: Path) -> bool:
    gen = resolved(gen)
    protected = {active_generation(), CERTIFIED_EXTERNAL_BASELINE, library_root() / ".rag_db", generations_root()}
    return gen in {resolved(p) for p in protected}


def _collection_from_chroma_public_api(gen: Path, expected_collection: str):
    _assert_chroma_public_api_allowed(gen)
    import chromadb

    client = chromadb.PersistentClient(path=str(gen))
    collections = client.list_collections()
    names = sorted(c.name if hasattr(c, "name") else str(c) for c in collections)
    if names != [expected_collection]:
        raise ControlRefusal(f"expected exactly collection {expected_collection!r}, got {names}")
    return client.get_collection(expected_collection), {"provider": "chromadb.PersistentClient", "chromadb_version": getattr(chromadb, "__version__", "not specified"), "collection_names": names}


def embedding_payload_digest(gen: Path, *, expected_collection: str = "langchain", page_size: int = 1000, collection_provider: Any | None = None) -> dict[str, Any]:
    """Return the stable embedding-payload digest contract.

    Contract: status, algorithm, canonical_byte_format_version, provider,
    collection, ordering, ordered_vector_ids, ordered_id_digest, vector_count,
    dtype, dimensions, per_vector_dimensions, pagination, and sha256. Production
    code retrieves actual vectors through Chroma's public collection API only for
    disposable /private/tmp generations; protected production and certified
    baseline paths are refused. The digest binds each ordered vector/chunk ID,
    dimension, and actual finite float payload encoded as deterministic float32-le.
    """
    gen = resolved(gen)
    provider = collection_provider or EMBEDDING_COLLECTION_PROVIDER
    if provider is None:
        collection, provider_meta = _collection_from_chroma_public_api(gen, expected_collection)
    else:
        collection, provider_meta = provider(gen, expected_collection)
    total = int(collection.count())
    if total < 1:
        raise ControlRefusal("embedding payload digest refused: missing vectors")
    seen: dict[str, list[float]] = {}
    pages = []
    fetched = 0
    for offset in range(0, total, page_size):
        limit = min(page_size, total - offset)
        page = collection.get(include=["embeddings"], limit=limit, offset=offset)
        ids = [str(x) for x in page.get("ids") or []]
        embeddings = page.get("embeddings")
        if embeddings is None or len(ids) != len(embeddings):
            raise ControlRefusal("embedding payload digest refused: missing embeddings or incomplete pagination")
        pages.append({"offset": offset, "limit": limit, "returned": len(ids)})
        fetched += len(ids)
        for embedding_id, vector in zip(ids, embeddings):
            if embedding_id in seen:
                raise ControlRefusal(f"embedding payload digest refused: duplicate ID {embedding_id}")
            if vector is None:
                raise ControlRefusal(f"embedding payload digest refused: missing vector for {embedding_id}")
            floats = [float(v) for v in vector]
            if not floats:
                raise ControlRefusal(f"embedding payload digest refused: missing vector for {embedding_id}")
            if any(not math.isfinite(v) for v in floats):
                raise ControlRefusal(f"embedding payload digest refused: non-finite float for {embedding_id}")
            seen[embedding_id] = floats
    if fetched != total or len(seen) != total:
        raise ControlRefusal("embedding payload digest refused: incomplete pagination")
    ordered_ids = sorted(seen)
    dims = {len(seen[embedding_id]) for embedding_id in ordered_ids}
    if len(dims) != 1:
        raise ControlRefusal(f"embedding payload digest refused: inconsistent dimensions {sorted(dims)}")
    h = hashlib.sha256()
    id_h = hashlib.sha256()
    h.update(EMBEDDING_DIGEST_VERSION.encode("ascii") + b"\n")
    h.update(f"collection={expected_collection}\n".encode("utf-8"))
    h.update(b"ordering=embedding_id\n")
    h.update(b"dtype=float32-le\n")
    per_vector_dimensions: dict[str, int] = {}
    per_vector_payload_sha256: dict[str, str] = {}
    per_vector_dtype: dict[str, str] = {}
    for embedding_id in ordered_ids:
        floats = seen[embedding_id]
        dim = len(floats)
        packed = struct.pack("<" + "f" * dim, *floats)
        per_vector_dimensions[embedding_id] = dim
        per_vector_payload_sha256[embedding_id] = hashlib.sha256(packed).hexdigest()
        per_vector_dtype[embedding_id] = "float32-le"
        id_h.update(embedding_id.encode("utf-8") + b"\0")
        h.update(embedding_id.encode("utf-8") + b"\0")
        h.update(str(dim).encode("ascii") + b"\0")
        h.update(packed)
    return {
        "status": "OK",
        "algorithm": EMBEDDING_DIGEST_VERSION,
        "canonical_byte_format_version": EMBEDDING_DIGEST_VERSION,
        "provider": provider_meta,
        "collection": expected_collection,
        "ordering": "embedding_id",
        "ordered_vector_ids": ordered_ids,
        "ordered_ids": ordered_ids,
        "ordered_id_digest": id_h.hexdigest(),
        "vector_count": len(ordered_ids),
        "row_count": len(ordered_ids),
        "dtype": "float32-le",
        "dimensions": sorted(dims),
        "per_vector_dimensions": per_vector_dimensions,
        "per_vector_dtype": per_vector_dtype,
        "per_vector_payload_sha256": per_vector_payload_sha256,
        "pagination": {"page_size": page_size, "pages": pages, "fetched": fetched, "expected_total": total},
        "sha256": h.hexdigest(),
    }


def embedding_metadata_by_id(gen: Path) -> dict[str, dict[str, Any]]:
    """Return embedding metadata keyed by vector ID using read-only SQLite."""
    gen = resolved(gen)
    conn = sqlite3.connect(f"file:{gen / 'chroma.sqlite3'}?mode=ro", uri=True)
    try:
        table_names = {str(r[0]) for r in conn.execute("select name from sqlite_master where type='table'").fetchall()}
        if not {"embeddings", "embedding_metadata"}.issubset(table_names):
            return {}
        cols = [str(r[1]) for r in conn.execute("pragma table_info(embedding_metadata)").fetchall()]
        value_cols = [c for c in ("string_value", "int_value", "float_value", "bool_value") if c in cols]
        if not value_cols:
            return {}
        select_values = ", ".join(f"m.{c}" for c in value_cols)
        rows = conn.execute(
            f"select e.embedding_id, m.key, {select_values} from embeddings e left join embedding_metadata m on m.id=e.id order by e.embedding_id, m.key"
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            embedding_id = str(row[0])
            key = row[1]
            out.setdefault(embedding_id, {})
            if key is None:
                continue
            values = row[2:]
            value = None
            for candidate in values:
                if candidate is not None:
                    value = candidate
                    break
            out[embedding_id][str(key)] = value
        return out
    finally:
        conn.close()


def _load_sealed_manifest(package_dir: Path = SEALED_PACKAGE) -> dict[str, Any]:
    manifest = resolved(package_dir) / "atomic_orphan_repair_manifest.json"
    if not manifest.is_file():
        raise ControlRefusal(f"sealed manifest missing: {manifest}")
    return read_json(manifest)


def _manifest_affected_tracker_records(tracker: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    affected: dict[str, Any] = {}
    for op in manifest.get("operations") or []:
        digest = str(op.get("digest"))
        affected[digest] = tracker.get(digest)
    return affected


def _set_aware_pre_evidence(candidate: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or _load_sealed_manifest()
    tracker = read_json(candidate / "embedded.json")
    emb = embedding_payload_digest(candidate)
    return {
        "schema": "set-aware-postcheck-pre-evidence-v1",
        "ordered_vector_ids": emb["ordered_vector_ids"],
        "vector_count": emb["vector_count"],
        "dtype": emb["dtype"],
        "per_vector_dtype": emb["per_vector_dtype"],
        "dimensions": emb["dimensions"],
        "per_vector_dimensions": emb["per_vector_dimensions"],
        "per_vector_payload_sha256": emb["per_vector_payload_sha256"],
        "embedding_metadata_by_id": embedding_metadata_by_id(candidate),
        "manifest_affected_tracker_records": _manifest_affected_tracker_records(tracker, manifest),
        "source_content_hash": source_content_hash_report(candidate),
        "unaffected_content_file_hashes": source_content_hash_report(candidate)["content_file_hashes"],
    }


def tracker_orphan_count(gen: Path, lib: Path | None = None) -> dict[str, Any]:
    lib = lib or library_root()
    tracker = gen / "embedded.json"
    if not tracker.is_file():
        return {"ok": False, "error": "embedded.json missing"}
    data = read_json(tracker)
    paths: list[str] = []
    values = data.values() if isinstance(data, dict) else []
    for meta in values:
        if isinstance(meta, dict):
            for p in meta.get("paths") or []:
                paths.append(str(p).replace("\\", "/"))
    missing = sum(1 for p in paths if not (lib / p).is_file())
    return {"ok": True, "tracker_entries": len(data) if isinstance(data, dict) else None, "tracker_paths": len(paths), "missing_files": missing, "detail": f"missing_files={missing} of {len(paths)} tracker paths"}


def _ancestor_pids(pid: int | None = None) -> set[int]:
    pid = pid or os.getpid()
    ancestors = {pid}
    current = pid
    for _ in range(32):
        proc = subprocess.run(["ps", "-o", "ppid=", "-p", str(current)], text=True, capture_output=True, check=False)
        try:
            ppid = int(proc.stdout.strip())
        except ValueError:
            break
        if ppid <= 1 or ppid in ancestors:
            break
        ancestors.add(ppid)
        current = ppid
    return ancestors


def _parse_lsof_fds(lsof_output: str) -> dict[int, list[dict[str, Any]]]:
    by_pid: dict[int, list[dict[str, Any]]] = {}
    current_pid: int | None = None
    current_fd: str | None = None
    current_type: str | None = None
    for raw in lsof_output.splitlines():
        if not raw:
            continue
        tag, value = raw[0], raw[1:]
        if tag == "p":
            try:
                current_pid = int(value)
            except ValueError:
                current_pid = None
            current_fd = None
            current_type = None
        elif tag == "f":
            current_fd = value
        elif tag == "t":
            current_type = value
        elif tag == "n" and current_pid is not None:
            by_pid.setdefault(current_pid, []).append({"fd": current_fd, "type": current_type, "name": value})
    return by_pid


def _fd_is_write(fd: str | None) -> bool:
    return bool(fd) and ("w" in fd.lower() or "u" in fd.lower())


def _process_activity_classification(cmd: str, fds: list[dict[str, Any]], *, protected_pid: bool) -> dict[str, Any]:
    cmd_l = cmd.lower()
    action_words = []
    for token in cmd_l.split():
        if token.startswith("/") or token.startswith("--"):
            break
        action_words.append(token)
        if len(action_words) >= 4:
            break
    action_l = " ".join(action_words)
    writer_tokens = ("ingest", "repair", "executor", "orphan_repair", "chromadb", "chroma", "sqlite3")
    reader_tokens = ("python", "pytest", "ps", "lsof", "cat", "grep", "rg", "read")
    fd_writes = [fd for fd in fds if _fd_is_write(fd.get("fd"))]
    if protected_pid:
        return {"activity": "self_or_wrapper", "writer": False, "ambiguous": False, "reason": "excluded by PID identity", "write_fds": fd_writes}
    if fd_writes:
        return {"activity": "writer", "writer": True, "ambiguous": False, "reason": "process holds relevant file descriptor for writing", "write_fds": fd_writes}
    if any(tok in action_l for tok in writer_tokens):
        return {"activity": "writer", "writer": True, "ambiguous": False, "reason": "executable/action token indicates possible writer", "write_fds": fd_writes}
    if fds:
        return {"activity": "reader", "writer": False, "ambiguous": False, "reason": "only non-write file descriptors observed", "write_fds": fd_writes}
    if any(tok in action_l for tok in reader_tokens):
        return {"activity": "reader", "writer": False, "ambiguous": False, "reason": "harmless reader command-line match only", "write_fds": fd_writes}
    return {"activity": "ambiguous", "writer": False, "ambiguous": True, "reason": "command-line path match without executable/fd proof", "write_fds": fd_writes}


def process_scan(paths: list[Path], *, ps_output: str | None = None, lsof_output: str | None = None, current_pid: int | None = None, ancestor_pids: set[int] | None = None) -> dict[str, Any]:
    needles = [str(p) for p in paths]
    proc_returncode = 0
    if ps_output is None:
        proc = subprocess.run(["ps", "axo", "pid=,command="], text=True, capture_output=True, check=False)
        ps_output = proc.stdout
        proc_returncode = proc.returncode
    if lsof_output is None:
        lsof = subprocess.run(["lsof", "-n", "-F", "pcftn", *needles], text=True, capture_output=True, check=False)
        lsof_output = lsof.stdout if lsof.returncode in (0, 1) else ""
    fd_map = _parse_lsof_fds(lsof_output)
    matches = []
    self_pid = current_pid or os.getpid()
    protected_pids = ancestor_pids if ancestor_pids is not None else _ancestor_pids(self_pid)
    for line in ps_output.splitlines():
        if not any(n in line for n in needles):
            continue
        pid_s, _, cmd = line.strip().partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            pid = None
        protected_pid = bool(pid is not None and pid in protected_pids)
        classification = _process_activity_classification(cmd, fd_map.get(pid or -1, []), protected_pid=protected_pid)
        role = "self_or_wrapper" if protected_pid else "external_match"
        matches.append({"pid": pid, "role": role, "command": cmd, **classification})
    for pid, fds in fd_map.items():
        if any(item["pid"] == pid for item in matches):
            continue
        protected_pid = pid in protected_pids
        classification = _process_activity_classification("", fds, protected_pid=protected_pid)
        matches.append({"pid": pid, "role": "self_or_wrapper" if protected_pid else "external_fd_match", "command": None, **classification})
    return {"ps_exit": proc_returncode, "matches": matches, "active_writers_or_ambiguous": [m for m in matches if m.get("writer") or m.get("ambiguous")]}


def generation_report(gen: Path, lib: Path | None = None, *, include_process: bool = False, require_embedding_digest: bool = False) -> dict[str, Any]:
    exclude = {CERTIFIED_BASELINE_MARKER} if resolved(gen) == resolved(CERTIFIED_EXTERNAL_BASELINE) else set()
    rows = inventory_rows(gen, exclude=exclude)
    inv = inventory_digest(rows)
    if require_embedding_digest or not _is_protected_generation_for_chroma(gen):
        embedding_digest = embedding_payload_digest(gen)
    else:
        embedding_digest = {"status": "DEFERRED_TO_BATCH_B2_DISPOSABLE_COPY", "reason": "protected generation is not opened with Chroma during Batch B1", "provider": None}
    out = {"path": str(gen), "inventory_schema": INVENTORY_ROW_SCHEMA_V1, "file_count": len(rows), "byte_size": sum(r["bytes"] for r in rows), "inventory_sha256": inv, "file_hashes": {r["path"]: r["sha256"] for r in rows}, "wal_shm_lock": wal_shm_state(gen), "sqlite": sqlite_structural_checks(gen), "tracker_orphans": tracker_orphan_count(gen, lib), "embedding_payload_digest": embedding_digest}
    if exclude:
        out["certified_baseline_content_inventory"] = certified_baseline_content_inventory(gen)
    if require_embedding_digest and out["embedding_payload_digest"].get("status") != "OK":
        raise ControlRefusal(f"embedding payload digest unavailable: {out['embedding_payload_digest']}")
    if include_process:
        out["process_scan"] = process_scan([gen])
    return out


def verify_sealed_package(package_dir: Path = SEALED_PACKAGE) -> dict[str, Any]:
    sums = package_dir / "SHA256SUMS"
    if not sums.is_file():
        raise ControlRefusal(f"SHA256SUMS missing: {sums}")
    actual = sha256_file(sums)
    if actual != SEALED_SHA256SUMS_HASH:
        raise ControlRefusal(f"sealed SHA256SUMS hash mismatch: {actual}")
    mismatches = []
    entries = 0
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries += 1
        digest, rel = line.split(maxsplit=1)
        rel = rel.lstrip("*")
        p = package_dir / rel
        if not p.is_file() or sha256_file(p) != digest:
            mismatches.append(rel)
    if mismatches:
        raise ControlRefusal(f"sealed manifest mismatch: {mismatches[:5]}")
    return {
        "status": "SEALED_PACKAGE_OK",
        "ok": True,
        "package_dir": str(package_dir),
        "sha256sums_sha256": actual,
        "manifest_entries": entries,
    }


def verify_baseline_identity(baseline: Path = CERTIFIED_EXTERNAL_BASELINE) -> dict[str, Any]:
    baseline = resolved(baseline)
    if not baseline.is_dir():
        raise ControlRefusal(f"certified external baseline missing: {baseline}")
    before = generation_report(baseline, include_process=True)
    assert_certified_baseline_content_inventory(before["certified_baseline_content_inventory"])
    if before["sqlite"].get("ok") is not True or before["tracker_orphans"].get("ok") is not True:
        raise ControlRefusal("certified external baseline structural verification failed")
    if before["process_scan"].get("active_writers_or_ambiguous"):
        raise ControlRefusal("certified baseline active writer/ambiguous process detected")
    return before


def _record_boundary_journal(journal: Path, payload: dict[str, Any]) -> None:
    payload["journal_fsync_before_first_mutation"] = True
    write_json_atomic(journal, payload)


def _update_boundary_journal(journal: Path, payload: dict[str, Any]) -> None:
    write_json_atomic(journal, payload)


def backup_create(source: Path, dest: Path, journal: Path, *, acquire_lock: bool = False, min_free_bytes: int = 0, inject: str | None = None) -> dict[str, Any]:
    source = resolved(source); dest = resolved(dest); journal = resolved(journal)
    if source != active_generation():
        raise ControlRefusal(f"source must resolve to active production generation: {source} != {active_generation()}")
    assert_not_forbidden_destination(dest)
    assert_mutation_path_under_private_tmp(dest)
    usage = shutil.disk_usage(dest.parent if dest.parent.exists() else Path("/private/tmp"))
    if usage.free < min_free_bytes:
        raise ControlRefusal(f"insufficient free space: {usage.free} < {min_free_bytes}")
    before = generation_report(source, include_process=True, require_embedding_digest=True)
    payload = {"schema": SCHEMA, "artifact": BACKUP_JOURNAL, "status": "BACKUP_STARTED", "created_utc": utc_now(), "source": before, "destination_path": str(dest), "selection": {"RAG_DB_PATH": os.environ.get("RAG_DB_PATH"), "CE_LIBRARY_ROOT": os.environ.get("CE_LIBRARY_ROOT")}}
    _record_boundary_journal(journal, payload)
    if inject == "before_mutation":
        payload.update({"status": "BACKUP_REJECTED", "recovery_owner": "none", "residual_state": dest.exists()})
        _update_boundary_journal(journal, payload)
        raise ControlRefusal("injected backup failure before mutation")
    with synthetic_ingest_lock(source, acquire_lock) as lock_info:
        if inject == "during_mutation":
            dest.mkdir(parents=True, exist_ok=False)
            (dest / "partial.tmp").write_text("partial", encoding="utf-8")
            payload.update({"status": "BACKUP_REJECTED", "lock": lock_info, "failed_artifact_preserved": str(dest), "residual_state": True, "recovery_owner": "operator_review"})
            _update_boundary_journal(journal, payload)
            raise ControlRefusal("injected backup failure during mutation")
        shutil.copytree(source, dest, copy_function=shutil.copy2, ignore=shutil.ignore_patterns(*TRANSIENT_GENERATION_FILES))
    if inject == "after_mutation":
        payload.update({"status": "BACKUP_REJECTED", "lock": lock_info, "failed_artifact_preserved": str(dest), "residual_state": True, "recovery_owner": "operator_review"})
        _update_boundary_journal(journal, payload)
        raise ControlRefusal("injected backup failure after mutation")
    after_src = generation_report(source, require_embedding_digest=True)
    copied = generation_report(dest, require_embedding_digest=True)
    expected = {k: v for k, v in before["file_hashes"].items() if k not in TRANSIENT_GENERATION_FILES}
    if before["file_hashes"] != after_src["file_hashes"]:
        raise ControlRefusal("source changed during backup")
    if expected != copied["file_hashes"]:
        raise ControlRefusal("backup inventory/hash mismatch")
    if before["embedding_payload_digest"] != copied["embedding_payload_digest"]:
        raise ControlRefusal("backup embedding payload digest mismatch")
    payload.update({"status": "BACKUP_OK", "updated_utc": utc_now(), "destination": copied, "lock": lock_info, "recovery_owner": "none", "residual_state": False})
    _update_boundary_journal(journal, payload)
    return payload


def restore_verify(backup: Path, restore_target: Path, journal: Path, *, inject: str | None = None) -> dict[str, Any]:
    backup = resolved(backup); restore_target = resolved(restore_target); journal = resolved(journal)
    assert_not_forbidden_destination(restore_target)
    assert_mutation_path_under_private_tmp(restore_target)
    if not backup.is_dir():
        raise ControlRefusal(f"backup missing: {backup}")
    src = generation_report(backup, require_embedding_digest=True)
    payload = {"schema": SCHEMA, "artifact": RESTORE_JOURNAL, "status": "RESTORE_STARTED", "created_utc": utc_now(), "backup": src, "restore_target_path": str(restore_target)}
    _record_boundary_journal(journal, payload)
    if inject == "before_mutation":
        payload.update({"status": "RESTORE_VERIFY_REJECTED", "residual_state": restore_target.exists(), "failed_artifact_preserved": None})
        _update_boundary_journal(journal, payload)
        raise ControlRefusal("injected restore failure before mutation")
    if inject == "during_mutation":
        restore_target.mkdir(parents=True, exist_ok=False)
        (restore_target / "partial.tmp").write_text("partial", encoding="utf-8")
        payload.update({"status": "RESTORE_VERIFY_REJECTED", "residual_state": True, "failed_artifact_preserved": str(restore_target)})
        _update_boundary_journal(journal, payload)
        raise ControlRefusal("injected restore failure during mutation")
    shutil.copytree(backup, restore_target, copy_function=shutil.copy2)
    restored = generation_report(restore_target, require_embedding_digest=True)
    ok = src["file_hashes"] == restored["file_hashes"] and src["embedding_payload_digest"] == restored["embedding_payload_digest"] and restored["sqlite"].get("ok") is True and restored["tracker_orphans"].get("ok") is True
    if inject == "after_mutation":
        ok = False
    payload.update({"status": "RESTORE_VERIFY_OK" if ok else "RESTORE_VERIFY_FAIL", "updated_utc": utc_now(), "restore_target": restored, "identity_match": src["file_hashes"] == restored["file_hashes"], "failed_artifact_preserved": None if ok else str(restore_target)})
    _update_boundary_journal(journal, payload)
    if not ok:
        raise ControlRefusal("restore verification failed")
    return payload


def candidate_create(source: Path, dest: Path, journal: Path, purpose: str, *, inject: str | None = None) -> dict[str, Any]:
    source = resolved(source); dest = resolved(dest); journal = resolved(journal)
    purpose_text = str(purpose)
    assert_not_forbidden_destination(dest)
    assert_mutation_path_under_private_tmp(dest)
    if source == resolved(CERTIFIED_EXTERNAL_BASELINE):
        raise ControlRefusal("refusing certified external baseline itself as mutation source; use a verified disposable copy")
    src = generation_report(source, require_embedding_digest=True)
    payload = {"schema": SCHEMA, "artifact": CANDIDATE_JOURNAL, "status": "CANDIDATE_CREATE_STARTED", "created_utc": utc_now(), "source": src, "candidate_path": str(dest), "purpose": purpose_text}
    _record_boundary_journal(journal, payload)
    def residual_destination_state() -> dict[str, Any]:
        if not dest.exists():
            return {"exists": False, "type": None, "file_count": 0, "total_bytes": 0, "entries": []}
        entries = []
        total = 0
        if dest.is_dir():
            for path in sorted(dest.rglob("*")):
                rel = path.relative_to(dest).as_posix()
                if path.is_file():
                    size = path.stat().st_size
                    total += size
                    entries.append({"path": rel, "type": "file", "bytes": size, "sha256": sha256_file(path)})
                elif path.is_dir():
                    entries.append({"path": rel, "type": "dir"})
                elif path.is_symlink():
                    entries.append({"path": rel, "type": "symlink", "target": os.readlink(path)})
        return {"exists": True, "type": "dir" if dest.is_dir() else "file", "file_count": sum(1 for item in entries if item.get("type") == "file"), "total_bytes": total, "entries": entries}

    def reject_candidate(reason: str, *, exc: BaseException | None = None) -> None:
        payload.update({
            "status": "CANDIDATE_CREATE_REJECTED",
            "updated_utc": utc_now(),
            "failure_reason": reason,
            "exception_type": type(exc).__name__ if exc else None,
            "exception_message": str(exc) if exc else None,
            "residual_state": dest.exists(),
            "residual_destination_state": residual_destination_state(),
            "failed_artifact_preserved": str(dest) if dest.exists() else None,
            "recovery_owner": "operator",
            "required_recovery_action": "review the preserved failed candidate directory and remove it manually only after evidence is no longer required",
            "partial_candidate_reported": dest.exists(),
            "double_rollback_prevented": True,
            "rollback_attempted": False,
        })
        _update_boundary_journal(journal, payload)
        raise ControlRefusal(reason) from exc
    if inject == "before_mutation":
        reject_candidate("injected candidate failure before mutation")
    if inject == "during_mutation":
        dest.mkdir(parents=True, exist_ok=False)
        (dest / "partial.tmp").write_text("partial", encoding="utf-8")
        reject_candidate("injected candidate failure during mutation")
    shutil.copytree(source, dest, copy_function=shutil.copy2)
    source_content = source_content_hash_report(source)
    marker = {"schema": "lc6-disposable-candidate-marker-v1", "candidate_id": f"lc6-candidate-{uuid.uuid4()}", "created_utc": utc_now(), "source_generation": str(source), "target_generation": str(dest), "purpose": purpose_text, "source_inventory_schema": INVENTORY_ROW_SCHEMA_V1, "source_inventory_sha256": source_content["content_inventory_sha256"], "source_file_hashes": source_content["content_file_hashes"], "source_content_schema": SOURCE_CONTENT_HASH_SCHEMA_V1, "source_content_hash": source_content, "source_embedding_payload_digest": src["embedding_payload_digest"], "disposable": True, "unpromoted": True}
    sealed_marker: dict[str, Any] | None = None
    cand: dict[str, Any] | None = None
    try:
        write_json_atomic(dest / CANDIDATE_MARKER, marker)
        sealed_marker = {"clone_id": marker["candidate_id"], "source_generation": str(source), "creation_time_utc": marker["created_utc"], "purpose": purpose_text, "source_inventory_schema": INVENTORY_ROW_SCHEMA_V1, "source_content_schema": SOURCE_CONTENT_HASH_SCHEMA_V1, "source_inventory_checksum": source_content["content_inventory_sha256"], "source_marker_artifacts": source_content["marker_artifacts"], "target_generation": str(dest), "disposable": True, "unpromoted": True}
        write_json_atomic(dest / SEALED_DISPOSABLE_MARKER, sealed_marker)
        marker["candidate_control_artifacts"] = {SEALED_DISPOSABLE_MARKER: {"path": SEALED_DISPOSABLE_MARKER, "sha256": sha256_file(dest / SEALED_DISPOSABLE_MARKER), "schema": sealed_marker.get("schema"), "identity": sealed_marker}}
        write_json_atomic(dest / CANDIDATE_MARKER, marker)
        cand = generation_report(dest, require_embedding_digest=True)
        if src["embedding_payload_digest"] != cand["embedding_payload_digest"]:
            reject_candidate("candidate embedding payload digest changed during creation")
    except ControlRefusal:
        raise
    except Exception as exc:
        reject_candidate("candidate creation failed after mutation", exc=exc)
    if cand is None or sealed_marker is None:
        reject_candidate("candidate creation failed after mutation")
    if inject == "after_mutation":
        payload["candidate"] = cand
        reject_candidate("injected candidate failure after mutation")
    payload.update({"status": "CANDIDATE_CREATE_OK", "updated_utc": utc_now(), "candidate": cand, "marker": marker, "sealed_executor_marker": sealed_marker, "residual_state": False})
    _update_boundary_journal(journal, payload)
    return payload


def validate_candidate_marker(gen: Path) -> dict[str, Any]:
    gen = resolved(gen)
    if gen == active_generation() or gen == library_root() / ".rag_db" or gen == generations_root() or gen == CERTIFIED_EXTERNAL_BASELINE:
        raise ControlRefusal(f"forbidden candidate target: {gen}")
    mpath = gen / CANDIDATE_MARKER
    if not mpath.is_file():
        raise ControlRefusal(f"candidate marker missing: {mpath}")
    marker = read_json(mpath)
    required = {"schema", "candidate_id", "created_utc", "source_generation", "target_generation", "purpose", "source_inventory_schema", "source_inventory_sha256", "source_file_hashes", "source_content_schema", "source_content_hash", "source_embedding_payload_digest", "candidate_control_artifacts", "disposable", "unpromoted"}
    missing = sorted(required - set(marker))
    if missing:
        raise ControlRefusal(f"candidate marker missing fields: {missing}")
    validate_inventory_schema(marker.get("source_inventory_schema"))
    if marker.get("source_content_schema") != SOURCE_CONTENT_HASH_SCHEMA_V1 or marker.get("source_content_hash", {}).get("schema") != SOURCE_CONTENT_HASH_SCHEMA_V1:
        raise ControlRefusal("candidate marker source-content schema mismatch")
    if marker["schema"] != "lc6-disposable-candidate-marker-v1" or not marker["disposable"] or not marker["unpromoted"]:
        raise ControlRefusal("invalid candidate marker safety fields")
    if resolved(marker["target_generation"]) != gen:
        raise ControlRefusal("candidate marker target_generation mismatch")
    current = generation_report(gen, require_embedding_digest=True)
    current_content = source_content_hash_report(gen)
    marker_hashes = dict(marker["source_content_hash"]["content_file_hashes"])
    if dict(marker.get("source_file_hashes", {})) != marker_hashes:
        diff = source_content_hash_diff(marker_hashes, dict(marker.get("source_file_hashes", {})))
        raise ControlRefusal(f"candidate marker legacy source hashes differ from source-content hashes: {json.dumps(diff, sort_keys=True)}")
    assert_source_content_match(marker_hashes, current_content["content_file_hashes"], context="candidate marker")
    expected_markers = marker.get("candidate_control_artifacts", {}) or marker["source_content_hash"].get("marker_artifacts", {})
    current_markers = current_content.get("marker_artifacts", {})
    sealed_expected = expected_markers.get(SEALED_DISPOSABLE_MARKER)
    sealed_current = current_markers.get(SEALED_DISPOSABLE_MARKER)
    if sealed_expected and sealed_current and sealed_expected.get("sha256") != sealed_current.get("sha256"):
        raise ControlRefusal("candidate sealed disposable marker provenance mismatch")
    if CANDIDATE_MARKER not in current_markers:
        raise ControlRefusal("candidate operational marker provenance missing")
    return marker


def validate_candidate_pre_mutation(candidate: Path, journal: Path) -> dict[str, Any]:
    """Validate and durably journal the mandatory pre-mutation candidate state."""
    candidate = resolved(candidate); journal = resolved(journal)
    marker = validate_candidate_marker(candidate)
    pre = generation_report(candidate, include_process=True, require_embedding_digest=True)
    current_content = source_content_hash_report(candidate)
    assert_source_content_match(dict(marker["source_content_hash"]["content_file_hashes"]), current_content["content_file_hashes"], context="candidate pre-mutation")
    if marker["source_embedding_payload_digest"] != pre["embedding_payload_digest"]:
        raise ControlRefusal("candidate pre-mutation embedding payload digest mismatch")
    set_aware_evidence = _set_aware_pre_evidence(candidate)
    payload = {"schema": SCHEMA, "artifact": "candidate_pre_mutation_validation", "status": "CANDIDATE_PRE_MUTATION_VALIDATED", "created_utc": utc_now(), "candidate_path": str(candidate), "candidate_marker": marker, "source_content_hash": current_content, "pre_state": pre, "set_aware_postcheck_pre_evidence": set_aware_evidence}
    _record_boundary_journal(journal, payload)
    return payload


def _manifest_allowed_metadata_and_tracker_changes(manifest: dict[str, Any], pre_tracker: dict[str, Any], post_tracker: dict[str, Any], pre_meta: dict[str, dict[str, Any]], post_meta: dict[str, dict[str, Any]]) -> dict[str, Any]:
    metadata_failures: list[dict[str, Any]] = []
    tracker_failures: list[dict[str, Any]] = []
    affected_ids: set[str] = set()
    affected_digests: set[str] = set()
    for op in manifest.get("operations") or []:
        digest = str(op.get("digest"))
        affected_digests.add(digest)
        ids = {str(x) for x in (op.get("chunk_ids") or [])}
        affected_ids.update(ids)
        if op.get("op_type") == "RETIRE_UNRECOVERABLE":
            if digest in post_tracker:
                tracker_failures.append({"digest": digest, "reason": "retired digest still present"})
            for cid in ids:
                if cid in post_meta:
                    metadata_failures.append({"id": cid, "reason": "retired id metadata still present"})
            continue
        before = pre_tracker.get(digest)
        after = post_tracker.get(digest)
        if after is None:
            tracker_failures.append({"digest": digest, "reason": "retained digest missing"})
            continue
        expected_paths = [op.get("canonical_live_path")]
        expected_collection = op.get("derived_collection")
        expected_chunk_ids = sorted(str(x) for x in (op.get("chunk_ids") or []))
        if sorted(str(x) for x in (after.get("paths") or [])) != sorted(str(x) for x in expected_paths):
            tracker_failures.append({"digest": digest, "reason": "paths not canonical", "actual": after.get("paths"), "expected": expected_paths})
        if after.get("collection") != expected_collection:
            tracker_failures.append({"digest": digest, "reason": "collection not derived", "actual": after.get("collection"), "expected": expected_collection})
        if sorted(str(x) for x in (after.get("chunk_ids") or [])) != expected_chunk_ids:
            tracker_failures.append({"digest": digest, "reason": "chunk_ids changed unexpectedly", "actual": after.get("chunk_ids"), "expected": expected_chunk_ids})
        if before is not None:
            allowed_after = dict(before)
            allowed_after["paths"] = expected_paths
            allowed_after["collection"] = expected_collection
            if dict(after) != allowed_after:
                tracker_failures.append({"digest": digest, "reason": "tracker fields changed outside allowed path/collection transition", "actual": after, "expected": allowed_after})
        for cid in ids:
            actual = post_meta.get(cid)
            if actual is None:
                metadata_failures.append({"id": cid, "reason": "retained affected id metadata missing"})
                continue
            expected = dict(pre_meta.get(cid) or {})
            expected["source"] = op.get("canonical_live_path")
            expected["collection"] = op.get("derived_collection")
            expected["source_hash"] = digest
            if actual != expected:
                metadata_failures.append({"id": cid, "reason": "metadata differs outside exact manifest transition", "actual": actual, "expected": expected})
    unaffected_ids = sorted((set(pre_meta) | set(post_meta)) - affected_ids)
    unaffected_metadata_changes = [cid for cid in unaffected_ids if pre_meta.get(cid) != post_meta.get(cid)]
    unaffected_digests = sorted((set(pre_tracker) | set(post_tracker)) - affected_digests)
    unaffected_tracker_changes = [digest for digest in unaffected_digests if pre_tracker.get(digest) != post_tracker.get(digest)]
    return {"metadata_failures": metadata_failures, "tracker_failures": tracker_failures, "unaffected_metadata_changes": unaffected_metadata_changes, "unaffected_tracker_changes": unaffected_tracker_changes, "affected_ids": sorted(affected_ids), "affected_digests": sorted(affected_digests)}


def validate_candidate_post_mutation(candidate: Path, journal: Path, pre_state_journal: Path, *, expected_orphans: int | None = None, expected_embedding_digest: str | None = None, require_embedding_digest: bool = True, authorized_retired_ids: set[str] | None = None, authorized_retired_digest: str = AUTHORIZED_RETIRED_DIGEST, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate post-mutation state against the durably journaled pre-state."""
    candidate = resolved(candidate); journal = resolved(journal); pre_state_journal = resolved(pre_state_journal)
    pre_payload = read_json(pre_state_journal)
    if pre_payload.get("status") != "CANDIDATE_PRE_MUTATION_VALIDATED":
        raise ControlRefusal("candidate pre-mutation journal is missing validated status")
    marker = read_json(candidate / CANDIDATE_MARKER)
    pre_marker = pre_payload["candidate_marker"]
    identity_keys = ["schema", "candidate_id", "source_generation", "target_generation", "source_inventory_schema", "source_inventory_sha256", "source_embedding_payload_digest", "disposable", "unpromoted"]
    for key in identity_keys:
        if marker.get(key) != pre_marker.get(key):
            raise ControlRefusal(f"candidate post-mutation marker provenance mismatch: {key}")
    if resolved(marker["target_generation"]) != candidate:
        raise ControlRefusal("candidate post-mutation marker target_generation mismatch")
    manifest = manifest or _load_sealed_manifest()
    authorized_retired_ids = set(authorized_retired_ids or AUTHORIZED_RETIRED_VECTOR_IDS)
    report = generation_report(candidate, include_process=True, require_embedding_digest=require_embedding_digest)
    pre = pre_payload["pre_state"]
    failure_reasons = []
    ok = True
    pre_hashes = dict(pre_payload["source_content_hash"]["content_file_hashes"])
    post_hashes = source_content_hash_report(candidate)["content_file_hashes"]
    pre_paths = set(pre_hashes)
    post_paths = set(post_hashes)
    if pre_paths != post_paths:
        ok = False; failure_reasons.append("unrelated_file_set_change")
    changed_files = sorted(p for p in pre_paths & post_paths if pre_hashes[p] != post_hashes[p])
    allowed_changed_files = set(EXPECTED_POST_MUTATION_CHANGED_FILES)
    if any(path not in allowed_changed_files for path in changed_files):
        ok = False; failure_reasons.append("unrelated_file_hash_change")
    if report["sqlite"].get("ok") is not True:
        ok = False; failure_reasons.append("sqlite_check_failed")
    pre_emb = pre["embedding_payload_digest"]
    emb = report["embedding_payload_digest"]
    pre_ids = set(str(x) for x in pre_emb.get("ordered_vector_ids") or [])
    post_ids = set(str(x) for x in emb.get("ordered_vector_ids") or [])
    removed_ids = sorted(pre_ids - post_ids)
    added_ids = sorted(post_ids - pre_ids)
    retained_ids = sorted(pre_ids & post_ids)
    expected_post_count = int(pre.get("sqlite", {}).get("embeddings_count") or pre_emb.get("vector_count") or 0) - len(authorized_retired_ids)
    if set(removed_ids) != authorized_retired_ids:
        ok = False; failure_reasons.append("unauthorized_removed_vector_ids")
    if added_ids:
        ok = False; failure_reasons.append("unexpected_added_vector_ids")
    if set(retained_ids) != (pre_ids - authorized_retired_ids):
        ok = False; failure_reasons.append("retained_vector_id_set_mismatch")
    if pre["sqlite"].get("embeddings_count") != report["sqlite"].get("embeddings_count") + len(authorized_retired_ids) or report["sqlite"].get("embeddings_count") != expected_post_count:
        ok = False; failure_reasons.append("embedding_row_count_changed")
    if expected_orphans is not None and report["tracker_orphans"].get("missing_files") != expected_orphans:
        ok = False; failure_reasons.append("unexpected_orphan_count")
    if any(report["wal_shm_lock"].values()):
        ok = False; failure_reasons.append("wal_shm_or_lock_residue")
    if require_embedding_digest and emb.get("status") != "OK":
        ok = False; failure_reasons.append("embedding_payload_digest_unavailable")
    for field in ("dtype", "canonical_byte_format_version"):
        if emb.get(field) != pre_emb.get(field):
            ok = False; failure_reasons.append(f"embedding_{field}_changed")
    pre_dims = pre_emb.get("per_vector_dimensions") or {}
    post_dims = emb.get("per_vector_dimensions") or {}
    pre_dtype = pre_emb.get("per_vector_dtype") or {k: pre_emb.get("dtype") for k in pre_ids}
    post_dtype = emb.get("per_vector_dtype") or {k: emb.get("dtype") for k in post_ids}
    pre_payload_hash = pre_emb.get("per_vector_payload_sha256") or {}
    post_payload_hash = emb.get("per_vector_payload_sha256") or {}
    retained_dimension_mismatches = sorted(cid for cid in retained_ids if pre_dims.get(cid) != post_dims.get(cid))
    retained_dtype_mismatches = sorted(cid for cid in retained_ids if pre_dtype.get(cid) != post_dtype.get(cid))
    retained_payload_mismatches = sorted(cid for cid in retained_ids if pre_payload_hash.get(cid) != post_payload_hash.get(cid))
    if retained_dimension_mismatches:
        ok = False; failure_reasons.append("retained_vector_dimension_changed")
    if retained_dtype_mismatches:
        ok = False; failure_reasons.append("retained_vector_dtype_changed")
    if retained_payload_mismatches:
        ok = False; failure_reasons.append("retained_vector_payload_changed")
    pre_meta = pre_payload.get("set_aware_postcheck_pre_evidence", {}).get("embedding_metadata_by_id") or {}
    post_meta = embedding_metadata_by_id(candidate)
    retired_metadata_present = sorted(cid for cid in authorized_retired_ids if cid in post_meta)
    if retired_metadata_present:
        ok = False; failure_reasons.append("retired_vector_metadata_present")
    pre_tracker = read_json(Path(pre_marker["source_generation"]) / "embedded.json") if Path(pre_marker["source_generation"]).is_dir() else {}
    post_tracker = read_json(candidate / "embedded.json")
    transition_check = _manifest_allowed_metadata_and_tracker_changes(manifest, pre_tracker, post_tracker, pre_meta, post_meta)
    retired_ops = [op for op in manifest.get("operations") or [] if op.get("op_type") == "RETIRE_UNRECOVERABLE"]
    if len(retired_ops) != 1 or str(retired_ops[0].get("digest")) != authorized_retired_digest or set(str(x) for x in retired_ops[0].get("chunk_ids", [])) != authorized_retired_ids:
        ok = False; failure_reasons.append("authorized_retirement_contract_mismatch")
    if transition_check["metadata_failures"] or transition_check["unaffected_metadata_changes"]:
        ok = False; failure_reasons.append("embedding_metadata_transition_mismatch")
    if transition_check["tracker_failures"] or transition_check["unaffected_tracker_changes"]:
        ok = False; failure_reasons.append("tracker_metadata_transition_mismatch")
    expected_digest = expected_embedding_digest or pre_emb.get("sha256")
    set_aware_vector_comparison = {"authorized_retired_digest": authorized_retired_digest, "authorized_retired_ids": sorted(authorized_retired_ids), "pre_count": len(pre_ids), "post_count": len(post_ids), "expected_count_change": f"{len(pre_ids)} -> {len(pre_ids) - len(authorized_retired_ids)}", "removed_ids": removed_ids, "added_ids": added_ids, "retained_count": len(retained_ids), "retained_dimension_mismatches": retained_dimension_mismatches, "retained_dtype_mismatches": retained_dtype_mismatches, "retained_payload_mismatches": retained_payload_mismatches, "retired_metadata_present": retired_metadata_present}
    payload = {"schema": SCHEMA, "artifact": POSTCHECK_JOURNAL, "status": "POSTCHECK_OK" if ok else "POSTCHECK_FAIL", "created_utc": utc_now(), "candidate_marker": marker, "pre_state_journal": str(pre_state_journal), "candidate_report": report, "expected_orphans": expected_orphans, "expected_embedding_digest": expected_digest, "changed_files": changed_files, "allowed_changed_files": sorted(allowed_changed_files), "set_aware_vector_comparison": set_aware_vector_comparison, "manifest_transition_check": transition_check, "failure_reasons": failure_reasons}
    write_json_atomic(journal, payload)
    if not ok:
        raise ControlRefusal(f"candidate postcheck failed: {','.join(failure_reasons) if failure_reasons else 'unknown'}")
    return payload


def repair_candidate(candidate: Path, work_dir: Path, journal: Path, *, package_dir: Path = SEALED_PACKAGE, executor: Path = SEALED_EXECUTOR, inject: str | None = None, expected_status: str = "APPLY_OK", runtime_python: str | Path | None = None, allow_separate_runtime: bool = False) -> dict[str, Any]:
    candidate = resolved(candidate); work_dir = resolved(work_dir); journal = resolved(journal)
    assert_mutation_path_under_private_tmp(candidate)
    assert_mutation_path_under_private_tmp(work_dir)
    marker = validate_candidate_marker(candidate)
    if resolved(marker["source_generation"]) == candidate or candidate == resolved(CERTIFIED_EXTERNAL_BASELINE):
        raise ControlRefusal("candidate target equals forbidden source/baseline")
    runtime_report = runtime_preflight_for_batch_b2(runtime_python, current_python=sys.executable, allow_separate=allow_separate_runtime)
    sealed = verify_sealed_package(package_dir)
    pre = generation_report(candidate, require_embedding_digest=True)
    payload = {"schema": SCHEMA, "artifact": REPAIR_JOURNAL, "status": "REPAIR_WRAPPER_STARTED", "created_utc": utc_now(), "runtime": runtime_report, "sealed_package": sealed, "candidate_marker": marker, "pre": pre}
    _record_boundary_journal(journal, payload)
    cmd = [runtime_report["requested_lexical_executable"], str(executor), "apply", "--package-dir", str(package_dir), "--gen-dir", str(candidate), "--work-dir", str(work_dir)]
    if inject:
        cmd.extend(["--inject", inject])
    marker_path = candidate / CANDIDATE_MARKER
    marker_payload = read_json(marker_path)
    marker_stash = work_dir / CANDIDATE_MARKER
    write_json_atomic(marker_stash, marker_payload)
    marker_path.unlink()
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=1800)
    finally:
        if not marker_path.exists():
            write_json_atomic(marker_path, marker_payload)
    post = generation_report(candidate, require_embedding_digest=True)
    status = "REPAIR_WRAPPER_OK" if proc.returncode == 0 else "REPAIR_WRAPPER_FAIL"
    payload.update({"status": status, "updated_utc": utc_now(), "command": cmd, "returncode": proc.returncode, "stdout": proc.stdout[-6000:], "stderr": proc.stderr[-6000:], "post": post, "failed_artifact_preserved": str(work_dir) if status != "REPAIR_WRAPPER_OK" else None, "recovery_owner": "sealed_executor_or_wrapper"})
    _update_boundary_journal(journal, payload)
    if proc.returncode != 0 or expected_status not in proc.stdout:
        raise ControlRefusal("sealed executor did not return expected clean repair result")
    if pre["embedding_payload_digest"] != post["embedding_payload_digest"]:
        raise ControlRefusal("embedding payload digest changed during path-metadata-only repair")
    return payload


def postcheck(candidate: Path, journal: Path, *, expected_orphans: int | None = None, expected_embedding_digest: str | None = None, require_embedding_digest: bool = True, pre_state_journal: Path | None = None) -> dict[str, Any]:
    candidate = resolved(candidate); journal = resolved(journal)
    if pre_state_journal is not None:
        return validate_candidate_post_mutation(candidate, journal, pre_state_journal, expected_orphans=expected_orphans, expected_embedding_digest=expected_embedding_digest, require_embedding_digest=require_embedding_digest)
    marker = validate_candidate_marker(candidate)
    report = generation_report(candidate, include_process=True, require_embedding_digest=require_embedding_digest)
    ok = report["sqlite"].get("ok") is True and report["tracker_orphans"].get("ok") is True and not any(report["wal_shm_lock"].values())
    failure_reasons = []
    if report["sqlite"].get("ok") is not True:
        failure_reasons.append("sqlite_check_failed")
    if report["tracker_orphans"].get("ok") is not True:
        failure_reasons.append("tracker_orphan_check_failed")
    if any(report["wal_shm_lock"].values()):
        failure_reasons.append("wal_shm_or_lock_residue")
    if expected_orphans is not None and report["tracker_orphans"].get("missing_files") != expected_orphans:
        ok = False
        failure_reasons.append("unexpected_orphan_count")
    emb = report["embedding_payload_digest"]
    if require_embedding_digest and emb.get("status") != "OK":
        ok = False
        failure_reasons.append("embedding_payload_digest_unavailable")
    if expected_embedding_digest and emb.get("sha256") != expected_embedding_digest:
        ok = False
        failure_reasons.append("embedding_payload_digest_mismatch")
    payload = {"schema": SCHEMA, "artifact": POSTCHECK_JOURNAL, "status": "POSTCHECK_OK" if ok else "POSTCHECK_FAIL", "created_utc": utc_now(), "candidate_marker": marker, "candidate_report": report, "expected_orphans": expected_orphans, "expected_embedding_digest": expected_embedding_digest, "failure_reasons": failure_reasons}
    write_json_atomic(journal, payload)
    if not ok:
        detail = ",".join(failure_reasons) if failure_reasons else "unknown"
        raise ControlRefusal(f"candidate postcheck failed: {detail}")
    return payload


def postcheck_with_final_snapshots(candidate: Path, journal: Path, pre_state_journal: Path, after_snapshot_journal: Path, *, expected_orphans: int | None = None, require_embedding_digest: bool = True) -> dict[str, Any]:
    """Run set-aware postcheck and always capture after-state protected snapshots."""
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    try:
        result = postcheck(candidate, journal, expected_orphans=expected_orphans, require_embedding_digest=require_embedding_digest, pre_state_journal=pre_state_journal)
        return result
    except Exception as exc:
        error = {"type": type(exc).__name__, "error": str(exc)}
        raise
    finally:
        payload = {
            "schema": SCHEMA,
            "artifact": "batch_b2_after_snapshots_finally_v1",
            "created_utc": utc_now(),
            "postcheck_result_status": result.get("status") if isinstance(result, dict) else None,
            "postcheck_error": error,
            "certified_baseline_after": verify_baseline_identity(CERTIFIED_EXTERNAL_BASELINE),
            "production_after": generation_report(active_generation(), include_process=True, require_embedding_digest=False),
            "selection_after": normalize_selection_snapshot({"RAG_DB_PATH": os.environ.get("RAG_DB_PATH"), "CE_LIBRARY_ROOT": os.environ.get("CE_LIBRARY_ROOT"), "mechanism": "RAG_DB_PATH environment override", "active_generation": str(active_generation())}),
        }
        write_json_atomic(after_snapshot_journal, payload)


def switch_selection(candidate: Path, journal: Path, *, selection_file: Path | None = None, synthetic: bool = False, inject: str | None = None) -> dict[str, Any]:
    candidate = resolved(candidate); journal = resolved(journal)
    marker = validate_candidate_marker(candidate)
    previous = os.environ.get("RAG_DB_PATH")
    if not synthetic or selection_file is None:
        payload = {"schema": SCHEMA, "artifact": SWITCH_JOURNAL, "status": "BLOCKED_MISSING_SELECTION_OWNER", "created_utc": utc_now(), "reason": "RAG_DB_PATH is process environment based; no governed persistent owner file/service integration point is implemented", "candidate": str(candidate), "previous_RAG_DB_PATH": previous}
        write_json_atomic(journal, payload)
        return payload
    selection_file = resolved(selection_file)
    assert_mutation_path_under_private_tmp(selection_file)
    payload = {"schema": SCHEMA, "artifact": SWITCH_JOURNAL, "status": "SWITCH_STARTED_SYNTHETIC_ONLY", "created_utc": utc_now(), "selection_file": str(selection_file), "candidate_marker": marker}
    _record_boundary_journal(journal, payload)
    if inject == "before_mutation":
        payload.update({"status": "SWITCH_REJECTED_SYNTHETIC_ONLY", "residual_state": selection_file.exists()})
        _update_boundary_journal(journal, payload)
        raise ControlRefusal("injected switch failure before mutation")
    old = selection_file.read_text(encoding="utf-8").strip() if selection_file.exists() else previous
    tmp = selection_file.with_name(selection_file.name + ".tmp")
    write_text_atomic(tmp, str(candidate) + "\n")
    if inject == "during_mutation":
        payload.update({"status": "SWITCH_REJECTED_SYNTHETIC_ONLY", "failed_artifact_preserved": str(tmp), "residual_state": tmp.exists(), "previous_selection": old})
        _update_boundary_journal(journal, payload)
        raise ControlRefusal("injected switch failure during mutation")
    os.replace(tmp, selection_file)
    if inject == "after_mutation":
        payload.update({"status": "SWITCH_REJECTED_SYNTHETIC_ONLY", "failed_artifact_preserved": str(selection_file), "residual_state": True, "previous_selection": old})
        _update_boundary_journal(journal, payload)
        raise ControlRefusal("injected switch failure after mutation")
    payload.update({"status": "SWITCH_OK_SYNTHETIC_ONLY", "updated_utc": utc_now(), "previous_selection": old, "new_selection": str(candidate), "rollback_performed": False})
    _update_boundary_journal(journal, payload)
    return payload


def rollback_selection(switch_journal: Path, rollback_journal: Path, *, selection_file: Path | None = None, synthetic: bool = False, inject: str | None = None) -> dict[str, Any]:
    sw = read_json(resolved(switch_journal)); rollback_journal = resolved(rollback_journal)
    if sw.get("rollback_performed"):
        raise ControlRefusal("double rollback refused")
    if not synthetic or selection_file is None or sw.get("status") != "SWITCH_OK_SYNTHETIC_ONLY":
        payload = {"schema": SCHEMA, "artifact": ROLLBACK_JOURNAL, "status": "BLOCKED_MISSING_SELECTION_OWNER", "created_utc": utc_now(), "reason": "no governed persistent selection owner available for real rollback"}
        write_json_atomic(rollback_journal, payload)
        return payload
    selection_file = resolved(selection_file)
    assert_mutation_path_under_private_tmp(selection_file)
    previous = sw.get("previous_selection") or ""
    payload = {"schema": SCHEMA, "artifact": ROLLBACK_JOURNAL, "status": "ROLLBACK_STARTED_SYNTHETIC_ONLY", "created_utc": utc_now(), "selection_file": str(selection_file), "restore_selection": previous}
    _record_boundary_journal(rollback_journal, payload)
    if inject == "before_mutation":
        payload.update({"status": "ROLLBACK_REJECTED_SYNTHETIC_ONLY"})
        _update_boundary_journal(rollback_journal, payload)
        raise ControlRefusal("injected rollback failure before mutation")
    tmp = selection_file.with_name(selection_file.name + ".tmp")
    write_text_atomic(tmp, str(previous) + "\n")
    if inject == "during_mutation":
        payload.update({"status": "ROLLBACK_REJECTED_SYNTHETIC_ONLY", "failed_artifact_preserved": str(tmp)})
        _update_boundary_journal(rollback_journal, payload)
        raise ControlRefusal("injected rollback failure during mutation")
    os.replace(tmp, selection_file)
    sw["rollback_performed"] = True
    write_json_atomic(resolved(switch_journal), sw)
    if inject == "after_mutation":
        payload.update({"status": "ROLLBACK_REJECTED_SYNTHETIC_ONLY", "failed_artifact_preserved": str(selection_file)})
        _update_boundary_journal(rollback_journal, payload)
        raise ControlRefusal("injected rollback failure after mutation")
    payload.update({"status": "ROLLBACK_OK_SYNTHETIC_ONLY", "updated_utc": utc_now(), "restored_selection": previous})
    _update_boundary_journal(rollback_journal, payload)
    return payload


def production_snapshot(journal: Path) -> dict[str, Any]:
    gen = active_generation()
    report = generation_report(gen, include_process=True, require_embedding_digest=True)
    status = "SNAPSHOT_OK"
    if report["inventory_sha256"] != EXPECTED_PRODUCTION_INVENTORY_V1:
        status = "SNAPSHOT_UNEXPECTED_INVENTORY"
    payload = {"schema": SCHEMA, "artifact": "lc6_production_snapshot_v2.json", "status": status, "created_utc": utc_now(), "active_generation": str(gen), "library_root": str(library_root()), "generation": report, "selection": {"RAG_DB_PATH": os.environ.get("RAG_DB_PATH"), "CE_LIBRARY_ROOT": os.environ.get("CE_LIBRARY_ROOT")}}
    write_json_atomic(resolved(journal), payload)
    if status != "SNAPSHOT_OK":
        raise ControlRefusal("production inventory checksum drift")
    return payload


def cleanup_artifact_caches(package_dir: Path) -> dict[str, Any]:
    package_dir = resolved(package_dir)
    removed: list[str] = []
    for p in sorted(package_dir.rglob("*.pyc")):
        if package_dir not in [p, *p.parents]:
            raise ControlRefusal(f"refusing pyc outside package: {p}")
        p.unlink()
        removed.append(str(p.relative_to(package_dir)))
    for p in sorted([x for x in package_dir.rglob("__pycache__") if x.is_dir()], reverse=True):
        if package_dir not in [p, *p.parents]:
            raise ControlRefusal(f"refusing pycache outside package: {p}")
        try:
            p.rmdir()
            removed.append(str(p.relative_to(package_dir)) + "/")
        except OSError:
            pass
    return {"status": "ARTIFACT_HYGIENE_OK", "removed": removed, "remaining_excluded": [r["path"] for r in artifact_inventory_rows(package_dir) if artifact_excluded(Path(r["path"]))]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lc6-operational-controls")
    sub = p.add_subparsers(dest="cmd", required=True)
    def add_path(sp, name): sp.add_argument(name, type=Path)
    s=sub.add_parser("snapshot-production"); add_path(s,"--journal")
    s=sub.add_parser("verify-sealed"); s.add_argument("--package-dir", type=Path, default=SEALED_PACKAGE)
    s=sub.add_parser("verify-baseline"); s.add_argument("--baseline", type=Path, default=CERTIFIED_EXTERNAL_BASELINE)
    s=sub.add_parser("backup-create"); add_path(s,"--source"); add_path(s,"--dest"); add_path(s,"--journal"); s.add_argument("--acquire-lock", action="store_true", help="Synthetic validation only unless separately approved"); s.add_argument("--min-free-bytes", type=int, default=0); s.add_argument("--inject", choices=["before_mutation","during_mutation","after_mutation"])
    s=sub.add_parser("restore-verify"); add_path(s,"--backup"); add_path(s,"--restore-target"); add_path(s,"--journal"); s.add_argument("--inject", choices=["before_mutation","during_mutation","after_mutation"])
    s=sub.add_parser("candidate-create"); add_path(s,"--source"); add_path(s,"--dest"); add_path(s,"--journal"); s.add_argument("--purpose", required=True); s.add_argument("--inject", choices=["before_mutation","during_mutation","after_mutation"])
    s=sub.add_parser("repair-candidate"); add_path(s,"--candidate"); add_path(s,"--work-dir"); add_path(s,"--journal"); s.add_argument("--package-dir", type=Path, default=SEALED_PACKAGE); s.add_argument("--executor", type=Path, default=SEALED_EXECUTOR); s.add_argument("--inject"); s.add_argument("--expected-status", default="APPLY_OK")
    s=sub.add_parser("postcheck-candidate"); add_path(s,"--candidate"); add_path(s,"--journal"); s.add_argument("--expected-orphans", type=int); s.add_argument("--expected-embedding-digest")
    s=sub.add_parser("switch-selection"); add_path(s,"--candidate"); add_path(s,"--journal"); s.add_argument("--selection-file", type=Path); s.add_argument("--synthetic", action="store_true"); s.add_argument("--inject", choices=["before_mutation","during_mutation","after_mutation"])
    s=sub.add_parser("rollback-selection"); add_path(s,"--switch-journal"); add_path(s,"--rollback-journal"); s.add_argument("--selection-file", type=Path); s.add_argument("--synthetic", action="store_true"); s.add_argument("--inject", choices=["before_mutation","during_mutation","after_mutation"])
    s=sub.add_parser("cleanup-artifact-caches"); add_path(s,"--package-dir")
    args = p.parse_args(argv)
    try:
        if args.cmd == "snapshot-production": out = production_snapshot(args.journal)
        elif args.cmd == "verify-sealed": out = verify_sealed_package(args.package_dir)
        elif args.cmd == "verify-baseline": out = verify_baseline_identity(args.baseline)
        elif args.cmd == "backup-create": out = backup_create(args.source,args.dest,args.journal,acquire_lock=args.acquire_lock,min_free_bytes=args.min_free_bytes,inject=args.inject)
        elif args.cmd == "restore-verify": out = restore_verify(args.backup,args.restore_target,args.journal,inject=args.inject)
        elif args.cmd == "candidate-create": out = candidate_create(args.source,args.dest,args.journal,args.purpose,inject=args.inject)
        elif args.cmd == "repair-candidate": out = repair_candidate(args.candidate,args.work_dir,args.journal,package_dir=args.package_dir,executor=args.executor,inject=args.inject,expected_status=args.expected_status)
        elif args.cmd == "postcheck-candidate": out = postcheck(args.candidate,args.journal,expected_orphans=args.expected_orphans,expected_embedding_digest=args.expected_embedding_digest)
        elif args.cmd == "switch-selection": out = switch_selection(args.candidate,args.journal,selection_file=args.selection_file,synthetic=args.synthetic,inject=args.inject)
        elif args.cmd == "rollback-selection": out = rollback_selection(args.switch_journal,args.rollback_journal,selection_file=args.selection_file,synthetic=args.synthetic,inject=args.inject)
        elif args.cmd == "cleanup-artifact-caches": out = cleanup_artifact_caches(args.package_dir)
        else: raise AssertionError(args.cmd)
        print(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return 0 if not str(out.get("status", "")).startswith(("BLOCKED", "POSTCHECK_FAIL", "RESTORE_VERIFY_FAIL", "REPAIR_WRAPPER_FAIL", "REFUSED")) else 2
    except Exception as exc:
        print(json.dumps({"status":"REFUSED","error_type":type(exc).__name__,"error":str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())

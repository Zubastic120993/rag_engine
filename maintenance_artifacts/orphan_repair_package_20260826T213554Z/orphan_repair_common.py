#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine')
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rag_engine.config import chroma_client_settings, embed_model  # type: ignore
from rag_engine.lock import ingest_lock, IngestLockError  # type: ignore
from rag_engine.query import answer, retrieve_with_scores_and_diagnostics  # type: ignore
from rag_engine.scope_rules import explain_path_assignment  # type: ignore

PKG_ROOT = Path(__file__).resolve().parent
CHROMA_WORKER = PKG_ROOT / 'chroma_worker.py'
PROD_LIBRARY = Path('/Users/vladymyrzub/CE_Library')
PROD_GEN = Path('/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_20260814T182037Z_698e0df44604')
GENERATIONS_ROOT = PROD_LIBRARY / '.rag_db_generations'
LEGACY_RAG_DB = PROD_LIBRARY / '.rag_db'
RAG_CLI = REPO_ROOT / 'venv/bin/rag-engine'
RAG_PY = REPO_ROOT / 'venv/bin/python'
RETIRE_DIGEST = '247b28a9ff07169473ddb7ac5f54dc61bfac046796bd7d0b36249fa36e166c90'
RETIRE_CHUNKS = [
    'chunk:7cb9d07e312b2e6080aa112f5a165428',
    'chunk:9c1743c71b1b7c2e9d9aea88d3fbc247',
]
ELLIPSIS_PATTERNS = ('...', '<', '>', '[REPLACE', 'PLACEHOLDER')
DISPOSABLE_MARKER = '.orphan_repair_disposable_clone.json'
INVENTORY_ROW_SCHEMA_V1 = 'inventory-row-v1'
FORBIDDEN_TARGETS = [PROD_GEN, LEGACY_RAG_DB, GENERATIONS_ROOT, PROD_LIBRARY]


class PackageError(RuntimeError):
    pass


class StopExecution(PackageError):
    pass


class TargetRefusal(PackageError):
    pass


class WorkerError(PackageError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def canonical_json_bytes(obj: Any) -> bytes:
    def conv(v: Any):
        if hasattr(v, 'tolist'):
            return v.tolist()
        if isinstance(v, dict):
            return {str(k): conv(val) for k, val in v.items()}
        if isinstance(v, (list, tuple)):
            return [conv(x) for x in v]
        return v
    return json.dumps(conv(obj), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def tree_hashes(root: Path, exclude_relpaths: set[str] | None = None) -> dict[str, str]:
    exclude_relpaths = exclude_relpaths or set()
    out: dict[str, str] = {}
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel in exclude_relpaths:
            continue
        out[rel] = file_sha256(path)
    return out


def inventory_row_schema_from_marker(marker: dict[str, Any]) -> str:
    if 'source_inventory_schema' not in marker:
        return INVENTORY_ROW_SCHEMA_V1

    schema = marker['source_inventory_schema']
    if not isinstance(schema, str):
        raise TargetRefusal(
            f'invalid inventory row schema type: {type(schema).__name__}'
        )
    if not schema or not schema.strip():
        raise TargetRefusal('inventory row schema must be a non-empty string')
    if schema != INVENTORY_ROW_SCHEMA_V1:
        raise TargetRefusal(f'unsupported inventory row schema: {schema}')
    return schema


def inventory_rows(root: Path, exclude_relpaths: set[str] | None = None) -> list[dict[str, Any]]:
    exclude_relpaths = exclude_relpaths or set()
    rows = []
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel in exclude_relpaths:
            continue
        rows.append({'path': rel, 'type': 'file', 'bytes': path.stat().st_size, 'sha256': file_sha256(path)})
    return rows


def inventory_checksum_from_rows(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def env_for(gen_dir: Path, library_root: Path = PROD_LIBRARY) -> dict[str, str]:
    env = os.environ.copy()
    env['RAG_DB_PATH'] = str(gen_dir)
    env['CE_LIBRARY_ROOT'] = str(library_root)
    return env


@contextmanager
def patched_env(gen_dir: Path, library_root: Path = PROD_LIBRARY):
    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(env_for(gen_dir, library_root))
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def rag_json(gen_dir: Path, args: list[str], library_root: Path = PROD_LIBRARY) -> dict[str, Any]:
    proc = subprocess.run([str(RAG_CLI), *args], capture_output=True, text=True, env=env_for(gen_dir, library_root), check=False)
    if proc.returncode not in (0, 1, 2, 3, 4, 5):
        raise PackageError(f'unexpected exit {proc.returncode} for {args}: {proc.stderr}\n{proc.stdout}')
    if not proc.stdout.strip():
        raise PackageError(f'no JSON output for {args}: {proc.stderr}')
    return json.loads(proc.stdout)


def doctor_json(gen_dir: Path) -> dict[str, Any]:
    return rag_json(gen_dir, ['doctor', '--json'])


def scope_stats_json(gen_dir: Path) -> dict[str, Any]:
    return rag_json(gen_dir, ['scope-stats', '--json'])


def count_embeddings(sqlite_path: Path) -> int:
    conn = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True)
    try:
        return int(conn.execute('select count(*) from embeddings').fetchone()[0])
    finally:
        conn.close()


def tracker_load(gen_dir: Path) -> dict[str, Any]:
    return load_json(gen_dir / 'embedded.json')


def chunk_counts_by_id(sqlite_path: Path, chunk_ids: list[str]) -> dict[str, int]:
    conn = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            f"select embedding_id, count(*) from embeddings where embedding_id in ({','.join('?' for _ in chunk_ids)}) group by embedding_id",
            chunk_ids,
        ).fetchall()
        out = {cid: 0 for cid in chunk_ids}
        for cid, n in rows:
            out[str(cid)] = int(n)
        return out
    finally:
        conn.close()


def chunk_rows_by_source_hash(sqlite_path: Path, digest: str) -> list[str]:
    conn = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            """
            select distinct e.embedding_id
            from embeddings e
            join embedding_metadata m on m.id=e.id
            where m.key='source_hash' and m.string_value=?
            order by e.embedding_id
            """,
            (digest,),
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        conn.close()


def embedding_digest_from_payload(payload: dict[str, Any], chunk_ids: list[str]) -> str:
    rows = [{'chunk_id': cid, 'embedding': payload[cid].get('embedding')} for cid in chunk_ids]
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


def parse_orphan_count(doctor: dict[str, Any]) -> int | None:
    for check in doctor.get('checks', []):
        if check.get('name') == 'orphan_sources':
            detail = str(check.get('detail') or '')
            m = re.search(r'missing_files=(\d+)', detail)
            return int(m.group(1)) if m else None
    return None


class InterruptGuard:
    def __init__(self):
        self._old: dict[int, Any] = {}

    def _handler(self, signum, frame):
        raise StopExecution(f'interrupted by signal {signum}')

    def __enter__(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._old[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handler)
        return self

    def __exit__(self, exc_type, exc, tb):
        for sig, old in self._old.items():
            signal.signal(sig, old)
        return False


def validate_no_placeholders(value: Any, where: str = 'root') -> list[str]:
    bad: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            bad.extend(validate_no_placeholders(v, f'{where}.{k}'))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            bad.extend(validate_no_placeholders(v, f'{where}[{i}]'))
    elif isinstance(value, str):
        for p in ELLIPSIS_PATTERNS:
            if p in value:
                bad.append(where)
                break
    return bad


def artifact_inventory(root: Path) -> list[dict[str, Any]]:
    return inventory_rows(root)


def collection_assignment_reason(path: str) -> dict[str, Any]:
    return explain_path_assignment(path)


def expected_scope_counts(manifest: dict[str, Any]) -> dict[str, int]:
    return {
        'deterministic_stale_path_occurrences': int(manifest['counts']['deterministic_stale_path_occurrences']),
        'deterministic_digests': int(manifest['counts']['deterministic_digests']),
        'retirement_digests': int(manifest['counts']['retirement_digests']),
        'affected_digests_total': int(manifest['counts']['affected_digests_total']),
    }


def acquire_lock_for(gen_dir: Path, timeout_s: float = 0.0):
    old = os.environ.copy()
    os.environ.update(env_for(gen_dir))
    ctx = ingest_lock(timeout_s=timeout_s)
    try:
        ctx.__enter__()
    except Exception:
        os.environ.clear()
        os.environ.update(old)
        raise
    return ctx, old


def release_lock(ctx: Any, old_env: dict[str, str]) -> None:
    try:
        ctx.__exit__(None, None, None)
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def resolve_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def ensure_not_forbidden_target(gen_dir: Path) -> None:
    resolved = resolve_path(gen_dir)
    if resolved == PROD_GEN:
        raise TargetRefusal(f'refusing active production generation: {resolved}')
    if resolved == LEGACY_RAG_DB:
        raise TargetRefusal(f'refusing legacy live db root: {resolved}')
    if resolved == GENERATIONS_ROOT:
        raise TargetRefusal(f'refusing generations root: {resolved}')
    if resolved == PROD_LIBRARY:
        raise TargetRefusal(f'refusing CE_Library root: {resolved}')


def marker_path(gen_dir: Path) -> Path:
    return gen_dir / DISPOSABLE_MARKER


def create_disposable_clone_marker(gen_dir: Path, source_generation: Path, purpose: str, source_inventory_checksum: str, clone_id: str | None = None) -> dict[str, Any]:
    ensure_not_forbidden_target(gen_dir)
    payload = {
        'clone_id': clone_id or f'clone-{uuid.uuid4()}',
        'source_generation': str(resolve_path(source_generation)),
        'creation_time_utc': utc_now(),
        'purpose': purpose,
        'source_inventory_schema': INVENTORY_ROW_SCHEMA_V1,
        'source_inventory_checksum': source_inventory_checksum,
        'target_generation': str(resolve_path(gen_dir)),
        'disposable': True,
        'unpromoted': True,
    }
    write_json_atomic(marker_path(gen_dir), payload)
    return payload


def validate_disposable_clone_marker(gen_dir: Path) -> dict[str, Any]:
    ensure_not_forbidden_target(gen_dir)
    path = marker_path(gen_dir)
    if not path.is_file():
        raise TargetRefusal(f'missing disposable clone marker: {path}')
    marker = load_json(path)
    required = ['clone_id', 'source_generation', 'creation_time_utc', 'purpose', 'source_inventory_checksum']
    missing = [k for k in required if not marker.get(k)]
    if missing:
        raise TargetRefusal(f'invalid disposable clone marker missing fields: {missing}')
    if not marker.get('disposable') or not marker.get('unpromoted'):
        raise TargetRefusal('disposable clone marker must declare disposable and unpromoted true')
    inventory_row_schema_from_marker(marker)
    return marker


def copy_generation(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, copy_function=shutil.copy2)


def worker_json(gen_dir: Path, action: str, request: dict[str, Any] | None = None, timeout: int = 600) -> dict[str, Any]:
    request = request or {}
    temp_dir = Path(tempfile.mkdtemp(prefix='orphan_repair_worker_req_'))
    req_path = temp_dir / f'{action}.json'
    write_json_atomic(req_path, request)
    cmd = [str(RAG_PY), str(CHROMA_WORKER), action, '--gen-dir', str(gen_dir), '--request-file', str(req_path)]
    lc6_case20 = request.get('lc6_case20_malformed_output') if isinstance(request, dict) else None
    if isinstance(lc6_case20, dict):
        if lc6_case20.get('case_id'):
            cmd.extend(['--case-id', str(lc6_case20['case_id'])])
        if lc6_case20.get('run_id'):
            cmd.extend(['--run-id', str(lc6_case20['run_id'])])
        if lc6_case20.get('state_dir'):
            cmd.extend(['--state-dir', str(lc6_case20['state_dir'])])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise WorkerError(f'worker {action} exit={proc.returncode} stderr={proc.stderr.strip()} stdout={proc.stdout.strip()}')
    stdout = proc.stdout.strip()
    if not stdout:
        raise WorkerError(f'worker {action} produced no stdout JSON')
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise WorkerError(f'worker {action} malformed JSON stdout: {exc}: {stdout[:400]}') from exc
    if payload.get('status') != 'ok':
        raise WorkerError(f'worker {action} reported failure: {payload}')
    return payload


def similarity_pairs(gen_dir: Path, query: str, scope: str | None, k: int = 8) -> list[dict[str, Any]]:
    return worker_json(gen_dir, 'similarity_pairs', {'query': query, 'scope': scope, 'k': k})['pairs']


def answer_diagnostics(gen_dir: Path, query: str, scope: str) -> dict[str, Any]:
    return worker_json(gen_dir, 'answer_diagnostics', {'query': query, 'scope': scope, 'k': 5})['answer']

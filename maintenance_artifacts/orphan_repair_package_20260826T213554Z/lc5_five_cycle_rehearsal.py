#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orphan_repair_common import (
    DISPOSABLE_MARKER,
    INVENTORY_ROW_SCHEMA_V1,
    PROD_GEN,
    PROD_LIBRARY,
    RETIRE_CHUNKS,
    RETIRE_DIGEST,
    count_embeddings,
    file_sha256,
    inventory_checksum_from_rows,
    inventory_rows,
    inventory_row_schema_from_marker,
    parse_orphan_count,
    tracker_load,
    validate_disposable_clone_marker,
    write_json_atomic,
)

PKG = Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z')
BASELINE_CLONE = Path('/private/tmp/lc4_baseline_parent_bb2_a1u8/raggen_lc4_baseline_20260827T183948Z')
EXPECTED_MARKER_SHA = 'ce064f98f23abc33f3b66ea7c4f0e327ee8bec1e01c9be6121fbb81604c00b38'
EXPECTED_SOURCE_INVENTORY_CHECKSUM = 'b189d622935faf6a0aa14830a80fa73af85c49f2d965bb83cdfb23acdb4f992a'
EXPECTED_EMBEDDED = 'c51c8816585dce66449be2828e4bf0b9e06cba16a5189d8ea2634e3d6ed8af33'
EXPECTED_CHROMA = '71ad868ad404fd8a489c9b57213e37be291a4461753f0ec0ffa288476a1451f6'
EXPECTED_CHUNKS = 125291
EXPECTED_ORPHANS = 55
EXPECTED_CHUNKS_AFTER_APPLY = 125289
EXPECTED_ORPHANS_AFTER_APPLY = 0
RAG_PY = '/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/python'
RAG_CLI = '/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/rag-engine'
CHROMA_WORKER = str(PKG / 'chroma_worker.py')
EXECUTOR = str(PKG / 'orphan_repair_executor.py')
LOCKFILE = 'ingest.lock'
WAL = 'chroma.sqlite3-wal'
SHM = 'chroma.sqlite3-shm'


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def env_for(gen_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env['RAG_DB_PATH'] = str(gen_dir)
    env['CE_LIBRARY_ROOT'] = str(PROD_LIBRARY)
    return env


def run_cmd(cmd: list[str], timeout: int = 600, env: dict[str, str] | None = None, cwd: Path | None = None, allow_rc: set[int] | None = None) -> dict[str, Any]:
    start = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(cwd) if cwd else None)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return {
            'cmd': cmd,
            'pid': proc.pid,
            'returncode': -9,
            'stdout': stdout,
            'stderr': stderr,
            'duration_s': round(time.time() - start, 3),
            'timed_out': True,
        }
    result = {
        'cmd': cmd,
        'pid': proc.pid,
        'returncode': proc.returncode,
        'stdout': stdout,
        'stderr': stderr,
        'duration_s': round(time.time() - start, 3),
        'timed_out': False,
    }
    allowed = allow_rc or {0}
    if proc.returncode not in allowed:
        raise RuntimeError(f'command failed rc={proc.returncode}: {cmd}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}')
    return result


def parse_json_result(result: dict[str, Any]) -> dict[str, Any]:
    text = (result.get('stdout') or '').strip()
    if not text:
        raise RuntimeError(f'empty JSON stdout for command: {result["cmd"]}')
    return json.loads(text)


def worker_call(gen_dir: Path, action: str, request: dict[str, Any] | None = None, timeout: int = 900, allow_rc: set[int] | None = None) -> dict[str, Any]:
    request = request or {}
    req_dir = Path(tempfile.mkdtemp(prefix='lc5_req_', dir='/private/tmp'))
    req_path = req_dir / f'{action}.json'
    write_json_atomic(req_path, request)
    result = run_cmd([RAG_PY, CHROMA_WORKER, action, '--gen-dir', str(gen_dir), '--request-file', str(req_path)], timeout=timeout, allow_rc=allow_rc or {0})
    payload = parse_json_result(result)
    result['json'] = payload
    return result


def rag_json(gen_dir: Path, args: list[str], timeout: int = 900) -> dict[str, Any]:
    result = run_cmd([RAG_CLI, *args], timeout=timeout, env=env_for(gen_dir), allow_rc={0, 1, 2, 3, 4, 5})
    payload = parse_json_result(result)
    result['json'] = payload
    return result


def executor_apply(gen_dir: Path, work_dir: Path) -> dict[str, Any]:
    result = run_cmd([
        RAG_PY,
        EXECUTOR,
        'apply',
        '--package-dir',
        str(PKG),
        '--gen-dir',
        str(gen_dir),
        '--work-dir',
        str(work_dir),
    ], timeout=1800)
    payload = parse_json_result(result)
    result['json'] = payload
    return result


def inventory(root: Path, exclude: set[str] | None = None) -> dict[str, Any]:
    rows = inventory_rows(root, exclude_relpaths=exclude or set())
    total_bytes = sum(int(r['bytes']) for r in rows)
    return {
        'root': str(root),
        'file_count': len(rows),
        'total_bytes': total_bytes,
        'rows': rows,
        'checksum': inventory_checksum_from_rows(rows),
    }


def tree_hashes(root: Path, exclude: set[str] | None = None) -> dict[str, str]:
    exclude = exclude or set()
    out: dict[str, str] = {}
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel in exclude:
            continue
        out[rel] = file_sha256(path)
    return out


def json_sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def payload_digest_from_inspect(payload: dict[str, Any], chunk_ids: list[str], field: str) -> str:
    rows = [{'chunk_id': cid, field: payload[cid].get(field)} for cid in chunk_ids]
    return json_sha(rows)


def metadata_digest_from_inspect(payload: dict[str, Any], chunk_ids: list[str], exclude_keys: set[str] | None = None) -> str:
    exclude_keys = exclude_keys or set()
    rows = []
    for cid in chunk_ids:
        meta = dict(payload[cid].get('metadata') or {})
        for key in exclude_keys:
            meta.pop(key, None)
        rows.append({'chunk_id': cid, 'metadata': meta})
    return json_sha(rows)


def current_worker_processes() -> dict[str, Any]:
    result = run_cmd(['bash', '-lc', "ps -axo pid=,command= | egrep 'chroma_worker.py|orphan_repair_executor.py|rag-engine doctor|rag-engine ask|rag-engine scope-stats' | egrep -v 'egrep'"], timeout=120, allow_rc={0, 1})
    lines = [line.strip() for line in (result['stdout'] or '').splitlines() if line.strip()]
    return {'scan': result, 'lines': lines}


def no_wal_shm(path: Path) -> dict[str, Any]:
    wal = (path / WAL).exists()
    shm = (path / SHM).exists()
    return {'wal': wal, 'shm': shm, 'clear': not wal and not shm}


def ensure_outside_roots(path: Path) -> None:
    resolved = path.resolve()
    if str(resolved).startswith(str(PROD_LIBRARY.resolve())):
        raise RuntimeError(f'path must be outside CE_Library: {resolved}')
    if str(resolved).startswith(str(PROD_GEN.resolve())):
        raise RuntimeError(f'path must be outside production generation: {resolved}')


def verify_marker_sha(path: Path, expected_sha: str) -> str:
    marker = path / DISPOSABLE_MARKER
    sha = file_sha256(marker)
    if sha != expected_sha:
        raise RuntimeError(f'marker sha mismatch: {sha} != {expected_sha}')
    return sha


def baseline_full_inventory() -> dict[str, Any]:
    return inventory(BASELINE_CLONE)


def baseline_source_inventory() -> dict[str, Any]:
    return inventory(BASELINE_CLONE, exclude={DISPOSABLE_MARKER})


def lc4_expected_clone_rows() -> list[dict[str, Any]]:
    payload = json.loads((PKG / 'lc4_clone_inventory.json').read_text(encoding='utf-8'))
    rows = payload['clone_inventory']['rows']
    normalized = []
    for row in rows:
        if set(row) != {'path', 'type', 'bytes', 'sha256'}:
            raise RuntimeError(f'unexpected historical LC4 inventory row schema: {sorted(row)}')
        if row.get('type') != 'file':
            raise RuntimeError(f'unexpected historical LC4 inventory row type for {row.get("path")}: {row.get("type")}')
        normalized.append({
            'path': row['path'],
            'type': row['type'],
            'bytes': row['bytes'],
            'sha256': row['sha256'],
        })
    return normalized


def validate_lc4_baseline_clone(manifest: dict[str, Any]) -> dict[str, Any]:
    baseline = BASELINE_CLONE.resolve()
    ensure_outside_roots(baseline)
    marker = validate_disposable_clone_marker(baseline)
    marker_sha = verify_marker_sha(baseline, EXPECTED_MARKER_SHA)
    marker_schema = inventory_row_schema_from_marker(marker)
    if marker_schema != INVENTORY_ROW_SCHEMA_V1:
        raise RuntimeError(f'unsupported baseline marker inventory schema: {marker_schema}')
    if marker.get('source_generation') != str(PROD_GEN):
        raise RuntimeError('baseline source_generation mismatch')
    if marker.get('purpose') != 'LC4_BASELINE_FOR_LC5':
        raise RuntimeError('baseline purpose mismatch')
    src_inv = baseline_source_inventory()
    expected_rows = lc4_expected_clone_rows()
    if src_inv['rows'] != expected_rows:
        raise RuntimeError('baseline source inventory rows mismatch against LC4 clone artifact')
    computed_checksum = src_inv['checksum']
    expected_checksum = EXPECTED_SOURCE_INVENTORY_CHECKSUM
    marker_checksum = str(marker.get('source_inventory_checksum'))
    if marker_checksum != expected_checksum:
        raise RuntimeError('baseline marker source inventory checksum mismatch')
    if computed_checksum != expected_checksum:
        raise RuntimeError(f'baseline computed source inventory checksum mismatch: {computed_checksum} != {expected_checksum}')
    wal_shm = no_wal_shm(baseline)
    if not wal_shm['clear']:
        raise RuntimeError('baseline has WAL/SHM files')
    embedded_sha = file_sha256(baseline / 'embedded.json')
    chroma_sha = file_sha256(baseline / 'chroma.sqlite3')
    if embedded_sha != EXPECTED_EMBEDDED:
        raise RuntimeError('baseline embedded.json hash mismatch')
    if chroma_sha != EXPECTED_CHROMA:
        raise RuntimeError('baseline chroma.sqlite3 hash mismatch')
    total_chunks = count_embeddings(baseline / 'chroma.sqlite3')
    if total_chunks != EXPECTED_CHUNKS:
        raise RuntimeError(f'baseline chunk count mismatch: {total_chunks}')
    doctor = rag_json(baseline, ['doctor', '--json'])
    orphan_count = parse_orphan_count(doctor['json'])
    if orphan_count != EXPECTED_ORPHANS:
        raise RuntimeError(f'baseline orphan count mismatch: {orphan_count}')
    open_check = worker_call(baseline, 'inspect', {'ids': []}, timeout=300)
    workers = current_worker_processes()
    if workers['lines']:
        raise RuntimeError(f'package workers alive before LC5: {workers["lines"]}')
    full_inv = baseline_full_inventory()
    return {
        'baseline_path': str(baseline),
        'marker': marker,
        'source_inventory_schema': marker_schema,
        'marker_sha256': marker_sha,
        'source_inventory_checksum': computed_checksum,
        'expected_source_inventory_checksum': expected_checksum,
        'marker_source_inventory_checksum': marker_checksum,
        'full_inventory_checksum': full_inv['checksum'],
        'embedded_json_sha256': embedded_sha,
        'chroma_sqlite3_sha256': chroma_sha,
        'total_chunks': total_chunks,
        'orphan_count': orphan_count,
        'doctor_cmd': doctor,
        'open_readonly_cmd': open_check,
        'wal_shm': wal_shm,
        'full_inventory': full_inv,
    }


def read_chunk_counts(sqlite_path: Path, chunk_ids: list[str]) -> dict[str, int]:
    conn = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            f"select embedding_id, count(*) from embeddings where embedding_id in ({','.join('?' for _ in chunk_ids)}) group by embedding_id",
            chunk_ids,
        ).fetchall()
    finally:
        conn.close()
    out = {cid: 0 for cid in chunk_ids}
    for cid, n in rows:
        out[str(cid)] = int(n)
    return out


def create_cycle_marker(cycle_dir: Path, baseline_info: dict[str, Any], cycle_number: int) -> dict[str, Any]:
    marker = {
        'clone_id': f'lc5-cycle-{cycle_number:02d}-{uuid.uuid4().hex[:12]}',
        'source_generation': str(PROD_GEN),
        'source_lc4_baseline_path': str(BASELINE_CLONE.resolve()),
        'creation_time_utc': utc_now(),
        'purpose': 'LC5_APPLY_VALIDATE_ROLLBACK',
        'source_inventory_schema': baseline_info['source_inventory_schema'],
        'source_inventory_checksum': baseline_info['source_inventory_checksum'],
        'lc4_inventory_checksum': baseline_info['source_inventory_checksum'],
        'cycle_number': cycle_number,
        'status': 'DISPOSABLE_LC5_CYCLE',
        'target_generation': str(cycle_dir.resolve()),
        'disposable': True,
        'unpromoted': True,
    }
    write_json_atomic(cycle_dir / DISPOSABLE_MARKER, marker)
    return marker


def ensure_within(parent: Path, child: Path) -> None:
    parent_resolved = parent.resolve()
    child_resolved = child.resolve()
    if parent_resolved not in child_resolved.parents and child_resolved != parent_resolved:
        raise RuntimeError(f'path outside allowed parent: {child_resolved} not under {parent_resolved}')


def create_snapshot(src: Path, snapshot_dir: Path) -> dict[str, Any]:
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    shutil.copytree(src, snapshot_dir, copy_function=shutil.copy2)
    src_inv = inventory(src)
    snap_inv = inventory(snapshot_dir)
    if src_inv['rows'] != snap_inv['rows']:
        raise RuntimeError('rollback snapshot inventory mismatch')
    return {'path': str(snapshot_dir), 'inventory': snap_inv, 'checksum': snap_inv['checksum']}


def restore_snapshot(cycle_dir: Path, snapshot_dir: Path, lc5_parent: Path) -> dict[str, Any]:
    ensure_within(lc5_parent, cycle_dir)
    ensure_within(lc5_parent, snapshot_dir)
    restored_dir = cycle_dir.parent / f'{cycle_dir.name}.restore_tmp'
    if restored_dir.exists():
        shutil.rmtree(restored_dir)
    shutil.copytree(snapshot_dir, restored_dir, copy_function=shutil.copy2)
    if cycle_dir.exists():
        shutil.rmtree(cycle_dir)
    restored_dir.rename(cycle_dir)
    return {'restored_path': str(cycle_dir)}


def manifest_ops(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    deterministic = [op for op in manifest['operations'] if op['op_type'] != 'RETIRE_UNRECOVERABLE']
    retirement = next(op for op in manifest['operations'] if op['op_type'] == 'RETIRE_UNRECOVERABLE')
    return deterministic, retirement


def capture_baseline_state(cycle_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    deterministic_ops, retirement_op = manifest_ops(manifest)
    tracker = tracker_load(cycle_dir)
    doctor = rag_json(cycle_dir, ['doctor', '--json'])
    scope_stats = rag_json(cycle_dir, ['scope-stats', '--json'])
    baseline_rows_cmd = worker_call(cycle_dir, 'validate_manifest_chunks', {'operations': deterministic_ops, 'include_embeddings': True}, timeout=1800)
    baseline_rows = baseline_rows_cmd['json']['rows']
    retirement_inspect = worker_call(cycle_dir, 'inspect', {'ids': RETIRE_CHUNKS, 'include_embeddings': True}, timeout=900)
    retirement_source_hash = worker_call(cycle_dir, 'source_hash_rows', {'digest': RETIRE_DIGEST}, timeout=300)
    retirement_payload = retirement_inspect['json']['payload']
    retirement_record = {
        'digest': RETIRE_DIGEST,
        'chunk_ids': RETIRE_CHUNKS,
        'tracker_entry': tracker.get(RETIRE_DIGEST),
        'source_hash_rows': retirement_source_hash['json']['chunk_ids'],
        'document_digest': payload_digest_from_inspect(retirement_payload, RETIRE_CHUNKS, 'document'),
        'metadata_digest': metadata_digest_from_inspect(retirement_payload, RETIRE_CHUNKS),
        'metadata_digest_excluding_source_collection': metadata_digest_from_inspect(retirement_payload, RETIRE_CHUNKS, {'source', 'collection'}),
        'embedding_digest': json_sha([{'chunk_id': cid, 'embedding': retirement_payload[cid].get('embedding')} for cid in RETIRE_CHUNKS]),
        'payload': retirement_payload,
    }
    affected_tracker_entries = {op['digest']: tracker.get(op['digest']) for op in manifest['operations']}
    return {
        'inventory': inventory(cycle_dir),
        'embedded_sha256': file_sha256(cycle_dir / 'embedded.json'),
        'chroma_sha256': file_sha256(cycle_dir / 'chroma.sqlite3'),
        'total_chunks': count_embeddings(cycle_dir / 'chroma.sqlite3'),
        'orphan_count': parse_orphan_count(doctor['json']),
        'doctor_cmd': doctor,
        'scope_stats_cmd': scope_stats,
        'scope_stats': scope_stats['json'],
        'tracker_subset': affected_tracker_entries,
        'deterministic_rows_cmd': baseline_rows_cmd,
        'deterministic_rows': {row['digest']: row for row in baseline_rows},
        'retirement_record': retirement_record,
        'retirement_inspect_cmd': retirement_inspect,
        'retirement_source_hash_cmd': retirement_source_hash,
        'stale_path_occurrences': sum(len(op.get('stale_old_paths') or []) for op in deterministic_ops),
        'deterministic_digest_count': len(deterministic_ops),
        'affected_digest_count': len(manifest['operations']),
    }


def validate_apply_state(cycle_dir: Path, baseline_state: dict[str, Any], manifest: dict[str, Any], transitions: dict[str, Any], acceptance: dict[str, Any], apply_cmd: dict[str, Any]) -> dict[str, Any]:
    deterministic_ops, _retirement_op = manifest_ops(manifest)
    wal_shm = no_wal_shm(cycle_dir)
    if not wal_shm['clear']:
        raise RuntimeError('WAL/SHM present after apply')
    doctor = rag_json(cycle_dir, ['doctor', '--json'])
    if doctor['json'].get('status') != 'PASS':
        raise RuntimeError('doctor.status != PASS after apply')
    orphan_count = parse_orphan_count(doctor['json'])
    if orphan_count != EXPECTED_ORPHANS_AFTER_APPLY:
        raise RuntimeError(f'orphan count after apply != {EXPECTED_ORPHANS_AFTER_APPLY}: {orphan_count}')
    scope_stats_cmd = rag_json(cycle_dir, ['scope-stats', '--json'])
    scope_stats = scope_stats_cmd['json']
    total_chunks = count_embeddings(cycle_dir / 'chroma.sqlite3')
    if total_chunks != EXPECTED_CHUNKS_AFTER_APPLY:
        raise RuntimeError(f'total chunks after apply != {EXPECTED_CHUNKS_AFTER_APPLY}: {total_chunks}')
    tracker = tracker_load(cycle_dir)
    validate_rows_cmd = worker_call(cycle_dir, 'validate_manifest_chunks', {'operations': deterministic_ops, 'include_embeddings': True}, timeout=1800)
    rows = {row['digest']: row for row in validate_rows_cmd['json']['rows']}
    row_checks = []
    for op in deterministic_ops:
        digest = op['digest']
        before = baseline_state['deterministic_rows'][digest]
        after = rows[digest]
        tracker_entry = tracker.get(digest)
        if tracker_entry is None:
            raise RuntimeError(f'missing tracker entry after apply: {digest}')
        if tracker_entry['paths'] != [op['canonical_live_path']]:
            raise RuntimeError(f'canonical path missing after apply: {digest}')
        if any(stale in tracker_entry['paths'] for stale in op.get('stale_old_paths') or []):
            raise RuntimeError(f'stale path still present after apply: {digest}')
        if after['chunk_ids'] != before['chunk_ids']:
            raise RuntimeError(f'chunk ids changed after apply: {digest}')
        if after['document_digest'] != before['document_digest']:
            raise RuntimeError(f'document digest changed after apply: {digest}')
        if after['embedding_digest'] != before['embedding_digest']:
            raise RuntimeError(f'embedding digest changed after apply: {digest}')
        if after['metadata_digest_excluding_source_collection'] != before['metadata_digest_excluding_source_collection']:
            raise RuntimeError(f'metadata changed beyond source/collection after apply: {digest}')
        if after['source_hashes'] != [digest]:
            raise RuntimeError(f'source_hash drift after apply: {digest}')
        if after['sources'] != [op['canonical_live_path']]:
            raise RuntimeError(f'wrong source metadata after apply: {digest}')
        if after['collections'] != [op['derived_collection']]:
            raise RuntimeError(f'wrong collection metadata after apply: {digest}')
        row_checks.append({
            'digest': digest,
            'chunk_count': len(after['chunk_ids']),
            'canonical_source': op['canonical_live_path'],
            'derived_collection': op['derived_collection'],
            'document_digest_unchanged': True,
            'embedding_digest_unchanged': True,
            'metadata_unchanged_except_source_collection': True,
        })
    retirement_source_rows = worker_call(cycle_dir, 'source_hash_rows', {'digest': RETIRE_DIGEST}, timeout=300)
    retirement_chunk_counts = read_chunk_counts(cycle_dir / 'chroma.sqlite3', RETIRE_CHUNKS)
    if retirement_source_rows['json']['chunk_ids']:
        raise RuntimeError('retirement digest still present after apply')
    if any(v != 0 for v in retirement_chunk_counts.values()):
        raise RuntimeError(f'retired chunks still present after apply: {retirement_chunk_counts}')
    me_c_before = baseline_state['scope_stats']['scopes']['me-c']['chunk_count']
    maker_before = baseline_state['scope_stats']['scopes']['maker-manuals']['chunk_count']
    me_c_after = scope_stats['scopes']['me-c']['chunk_count']
    maker_after = scope_stats['scopes']['maker-manuals']['chunk_count']
    if me_c_after - me_c_before != int(transitions['summary']['maker_manuals_to_me_c_chunk_total']):
        raise RuntimeError('me-c delta mismatch after apply')
    if maker_before - maker_after != int(transitions['summary']['maker_manuals_to_me_c_chunk_total']) + int(transitions['summary']['retirement_chunk_total']):
        raise RuntimeError('maker-manuals delta mismatch after apply')
    if baseline_state['total_chunks'] - total_chunks != int(transitions['summary']['retirement_chunk_total']):
        raise RuntimeError('total chunk delta mismatch after apply')
    retrieval_results = []
    for ctrl in acceptance['positive_controls']:
        answer_cmd = worker_call(cycle_dir, 'answer_diagnostics', {'query': ctrl['query'], 'scope': ctrl['scope'], 'k': 5}, timeout=900)
        answer_payload = answer_cmd['json']['answer']
        top_path = answer_payload['sources'][0]['path'] if answer_payload['sources'] else None
        ok = answer_payload['status'] == 'ok' and top_path == ctrl['expected_top_path']
        if not ok:
            raise RuntimeError(f'positive retrieval control failed: {ctrl["name"]}')
        pairs_cmd = worker_call(cycle_dir, 'similarity_pairs', {'query': ctrl['query'], 'scope': ctrl['scope'], 'k': 8}, timeout=900)
        retrieval_results.append({'control': ctrl, 'answer_cmd': answer_cmd, 'pairs_cmd': pairs_cmd, 'ok': True})
    for ctrl in acceptance['negative_controls']:
        answer_cmd = worker_call(cycle_dir, 'answer_diagnostics', {'query': ctrl['query'], 'scope': ctrl['scope'], 'k': 5}, timeout=900)
        answer_payload = answer_cmd['json']['answer']
        ok = answer_payload['status'] == ctrl['expected_status']
        if not ok:
            raise RuntimeError(f'negative retrieval control failed: {ctrl["name"]}')
        retrieval_results.append({'control': ctrl, 'answer_cmd': answer_cmd, 'ok': True})
    for ctrl in acceptance['no_regression_controls']:
        answer_cmd = worker_call(cycle_dir, 'answer_diagnostics', {'query': ctrl['original_query'], 'scope': ctrl['intended_scope'], 'k': 5}, timeout=900)
        answer_payload = answer_cmd['json']['answer']
        ok = answer_payload['status'] == 'no_coverage' and answer_payload['gate'] == 'final_confidence_failed'
        if not ok:
            raise RuntimeError(f'no-regression control failed: {ctrl["name"]}')
        retrieval_results.append({'control': ctrl, 'answer_cmd': answer_cmd, 'ok': True})
    reopened = worker_call(cycle_dir, 'inspect', {'ids': deterministic_ops[0]['chunk_ids'][:1]}, timeout=300)
    worker_scan = current_worker_processes()
    if worker_scan['lines']:
        raise RuntimeError(f'worker process remained alive after apply validation: {worker_scan["lines"]}')
    return {
        'apply_cmd': apply_cmd,
        'doctor_cmd': doctor,
        'scope_stats_cmd': scope_stats_cmd,
        'total_chunks': total_chunks,
        'orphan_count': orphan_count,
        'wal_shm': wal_shm,
        'validate_rows_cmd': validate_rows_cmd,
        'row_checks': row_checks,
        'retirement_source_hash_cmd': retirement_source_rows,
        'retirement_chunk_counts': retirement_chunk_counts,
        'collection_deltas': {
            'me_c_before': me_c_before,
            'me_c_after': me_c_after,
            'maker_manuals_before': maker_before,
            'maker_manuals_after': maker_after,
            'me_c_delta': me_c_after - me_c_before,
            'maker_manuals_delta': maker_after - maker_before,
            'total_chunk_delta': total_chunks - baseline_state['total_chunks'],
        },
        'retrieval_results': retrieval_results,
        'reopen_cmd': reopened,
        'physical_changes_recorded_by_executor': apply_cmd['json']['validation'].get('non_sqlite_physical_changes'),
        'worker_scan_after_apply': worker_scan,
    }


def validate_rollback_state(cycle_dir: Path, pre_mutation_state: dict[str, Any], manifest: dict[str, Any], lc5_parent: Path) -> dict[str, Any]:
    ensure_within(lc5_parent, cycle_dir)
    wal_shm = no_wal_shm(cycle_dir)
    if not wal_shm['clear']:
        raise RuntimeError('WAL/SHM present after rollback')
    current_inventory = inventory(cycle_dir)
    physical_equal = current_inventory['rows'] == pre_mutation_state['inventory']['rows']
    if not physical_equal:
        raise RuntimeError('rollback inventory does not match pre-mutation baseline')
    doctor = rag_json(cycle_dir, ['doctor', '--json'])
    orphan_count = parse_orphan_count(doctor['json'])
    if orphan_count != EXPECTED_ORPHANS:
        raise RuntimeError(f'orphan count after rollback != {EXPECTED_ORPHANS}: {orphan_count}')
    total_chunks = count_embeddings(cycle_dir / 'chroma.sqlite3')
    if total_chunks != EXPECTED_CHUNKS:
        raise RuntimeError(f'total chunks after rollback != {EXPECTED_CHUNKS}: {total_chunks}')
    embedded_sha = file_sha256(cycle_dir / 'embedded.json')
    chroma_sha = file_sha256(cycle_dir / 'chroma.sqlite3')
    if embedded_sha != pre_mutation_state['embedded_sha256']:
        raise RuntimeError('embedded.json hash mismatch after rollback')
    if chroma_sha != pre_mutation_state['chroma_sha256']:
        raise RuntimeError('chroma.sqlite3 hash mismatch after rollback')
    tracker = tracker_load(cycle_dir)
    for digest, record in pre_mutation_state['tracker_subset'].items():
        if tracker.get(digest) != record:
            raise RuntimeError(f'tracker subset mismatch after rollback: {digest}')
    retirement_rows = worker_call(cycle_dir, 'source_hash_rows', {'digest': RETIRE_DIGEST}, timeout=300)
    if retirement_rows['json']['chunk_ids'] != RETIRE_CHUNKS:
        raise RuntimeError('retirement digest rows not restored after rollback')
    retire_chunk_counts = read_chunk_counts(cycle_dir / 'chroma.sqlite3', RETIRE_CHUNKS)
    if any(v != 1 for v in retire_chunk_counts.values()):
        raise RuntimeError(f'retired chunks not restored after rollback: {retire_chunk_counts}')
    scope_stats_cmd = rag_json(cycle_dir, ['scope-stats', '--json'])
    if scope_stats_cmd['json']['scopes'] != pre_mutation_state['scope_stats']['scopes']:
        raise RuntimeError('scope stats mismatch after rollback')
    deterministic_ops, _ = manifest_ops(manifest)
    reopen_cmd = worker_call(cycle_dir, 'inspect', {'ids': deterministic_ops[0]['chunk_ids'][:1]}, timeout=300)
    worker_scan = current_worker_processes()
    if worker_scan['lines']:
        raise RuntimeError(f'worker process remained alive after rollback validation: {worker_scan["lines"]}')
    return {
        'inventory_checksum': current_inventory['checksum'],
        'physical_equal_to_pre_mutation': physical_equal,
        'logical_equivalence_basis': 'exact inventory equality' if physical_equal else 'not applicable',
        'doctor_cmd': doctor,
        'scope_stats_cmd': scope_stats_cmd,
        'orphan_count': orphan_count,
        'total_chunks': total_chunks,
        'embedded_sha256': embedded_sha,
        'chroma_sha256': chroma_sha,
        'retirement_source_hash_cmd': retirement_rows,
        'retirement_chunk_counts': retire_chunk_counts,
        'reopen_cmd': reopen_cmd,
        'worker_scan_after_rollback': worker_scan,
    }


def safe_delete_cycle_dir(path: Path, lc5_parent: Path) -> dict[str, Any]:
    ensure_within(lc5_parent, path)
    validate_disposable_clone_marker(path)
    shutil.rmtree(path)
    return {'path': str(path), 'deleted': not path.exists(), 'kind': 'cycle_dir'}


def safe_delete_snapshot(path: Path, lc5_parent: Path) -> dict[str, Any]:
    ensure_within(lc5_parent, path)
    shutil.rmtree(path)
    return {'path': str(path), 'deleted': not path.exists(), 'kind': 'rollback_snapshot'}


def package_state(manifest: dict[str, Any], transitions: dict[str, Any], acceptance: dict[str, Any], baseline_info: dict[str, Any]) -> dict[str, Any]:
    return {
        'manifest': manifest,
        'transitions': transitions,
        'acceptance': acceptance,
        'baseline_info': baseline_info,
    }


def load_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = json.loads((PKG / 'atomic_orphan_repair_manifest.json').read_text(encoding='utf-8'))
    transitions = json.loads((PKG / 'collection_transition_report.json').read_text(encoding='utf-8'))
    acceptance = json.loads((PKG / 'retrieval_acceptance_tests.json').read_text(encoding='utf-8'))
    return manifest, transitions, acceptance


def write_sha256sums() -> str:
    files = sorted(p for p in PKG.iterdir() if p.is_file() and p.name != 'SHA256SUMS')
    lines = [f'{file_sha256(p)}  {p.name}' for p in files]
    (PKG / 'SHA256SUMS').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return file_sha256(PKG / 'SHA256SUMS')


def production_check() -> dict[str, Any]:
    return {
        'active_generation_path': str(PROD_GEN),
        'embedded_json_sha256': file_sha256(PROD_GEN / 'embedded.json'),
        'chroma_sqlite3_sha256': file_sha256(PROD_GEN / 'chroma.sqlite3'),
        'total_chunks': count_embeddings(PROD_GEN / 'chroma.sqlite3'),
        'active_generation_selection': {
            'mechanism': 'task-designated production generation path remained unchanged; subprocess env overrides were cycle-local only',
            'value': str(PROD_GEN),
            'unchanged': True,
        },
    }


def cycle_report_name(cycle_number: int) -> Path:
    return PKG / f'lc5_cycle_{cycle_number:02d}_report.json'


def main() -> None:
    manifest, transitions, acceptance = load_inputs()
    baseline_info: dict[str, Any] | None = None
    lc5_parent: Path | None = None
    cycle_summaries: list[dict[str, Any]] = []
    apply_summary: list[dict[str, Any]] = []
    rollback_summary: list[dict[str, Any]] = []
    retrieval_summary: list[dict[str, Any]] = []
    cleanup_summary: list[dict[str, Any]] = []
    overall_status = 'LC5_FAIL'
    blocker: str | None = None
    try:
        baseline_info = validate_lc4_baseline_clone(manifest)
        lc5_parent = Path(tempfile.mkdtemp(prefix='lc5_', dir='/private/tmp')).resolve()
        base_full_inventory = baseline_info['full_inventory']
        for cycle_number in range(1, 6):
            cycle_id = f'cycle_{cycle_number:02d}_{uuid.uuid4().hex[:10]}'
            cycle_dir = lc5_parent / cycle_id
            work_dir = lc5_parent / f'{cycle_id}_work'
            shutil.copytree(BASELINE_CLONE, cycle_dir, copy_function=shutil.copy2)
            cloned_inventory = inventory(cycle_dir)
            if cloned_inventory['rows'] != base_full_inventory['rows']:
                raise RuntimeError(f'cycle {cycle_number:02d} clone inventory mismatch before marker replacement')
            cycle_marker = create_cycle_marker(cycle_dir, baseline_info, cycle_number)
            cycle_inventory_pre = inventory(cycle_dir)
            worker_accept = worker_call(cycle_dir, 'inspect', {'ids': []}, timeout=300)
            worker_refuse_prod = worker_call(PROD_GEN, 'inspect', {'ids': []}, timeout=300, allow_rc={3})
            if worker_refuse_prod['json'].get('status') != 'refused':
                raise RuntimeError(f'cycle {cycle_number:02d} production refusal check failed')
            worker_scan_before_snapshot = current_worker_processes()
            if worker_scan_before_snapshot['lines']:
                raise RuntimeError(f'cycle {cycle_number:02d} worker alive before snapshot: {worker_scan_before_snapshot["lines"]}')
            snapshot = create_snapshot(cycle_dir, work_dir / 'rollback_snapshot')
            baseline_state = capture_baseline_state(cycle_dir, manifest)
            if baseline_state['total_chunks'] != EXPECTED_CHUNKS:
                raise RuntimeError(f'cycle {cycle_number:02d} baseline chunk count mismatch')
            if baseline_state['orphan_count'] != EXPECTED_ORPHANS:
                raise RuntimeError(f'cycle {cycle_number:02d} baseline orphan count mismatch')
            if baseline_state['deterministic_digest_count'] != 40:
                raise RuntimeError(f'cycle {cycle_number:02d} deterministic digest count mismatch')
            if baseline_state['affected_digest_count'] != 41:
                raise RuntimeError(f'cycle {cycle_number:02d} affected digest count mismatch')
            if baseline_state['stale_path_occurrences'] != 54:
                raise RuntimeError(f'cycle {cycle_number:02d} stale path occurrence mismatch')
            apply_cmd = executor_apply(cycle_dir, work_dir)
            if apply_cmd['json'].get('status') != 'APPLY_OK':
                raise RuntimeError(f'cycle {cycle_number:02d} apply failed: {apply_cmd["json"]}')
            apply_validation = validate_apply_state(cycle_dir, baseline_state, manifest, transitions, acceptance, apply_cmd)
            worker_scan_before_restore = current_worker_processes()
            if worker_scan_before_restore['lines']:
                raise RuntimeError(f'cycle {cycle_number:02d} worker alive before rollback restore: {worker_scan_before_restore["lines"]}')
            restore_info = restore_snapshot(cycle_dir, work_dir / 'rollback_snapshot', lc5_parent)
            rollback_validation = validate_rollback_state(cycle_dir, baseline_state, manifest, lc5_parent)
            cycle_report = {
                'cycle_number': cycle_number,
                'cycle_id': cycle_marker['clone_id'],
                'cycle_dir': str(cycle_dir),
                'work_dir': str(work_dir),
                'status': 'PASS',
                'worker_accept_cmd': worker_accept,
                'worker_refuse_production_cmd': worker_refuse_prod,
                'worker_scan_before_snapshot': worker_scan_before_snapshot,
                'snapshot': snapshot,
                'baseline_capture': baseline_state,
                'apply_validation': apply_validation,
                'restore_info': restore_info,
                'rollback_validation': rollback_validation,
            }
            write_json_atomic(cycle_report_name(cycle_number), cycle_report)
            cleanup_items = [
                safe_delete_cycle_dir(cycle_dir, lc5_parent),
                safe_delete_snapshot(work_dir / 'rollback_snapshot', lc5_parent),
            ]
            cleanup_summary.extend(cleanup_items)
            cycle_report['cleanup'] = cleanup_items
            write_json_atomic(cycle_report_name(cycle_number), cycle_report)
            cycle_summaries.append({'cycle_number': cycle_number, 'status': 'PASS', 'cycle_id': cycle_marker['clone_id']})
            apply_summary.append({
                'cycle_number': cycle_number,
                'status': 'PASS',
                'chunks_after_apply': apply_validation['total_chunks'],
                'orphans_after_apply': apply_validation['orphan_count'],
                'collection_deltas': apply_validation['collection_deltas'],
                'physical_changes_recorded_by_executor': apply_validation['physical_changes_recorded_by_executor'],
            })
            rollback_summary.append({
                'cycle_number': cycle_number,
                'status': 'PASS',
                'chunks_after_rollback': rollback_validation['total_chunks'],
                'orphans_after_rollback': rollback_validation['orphan_count'],
                'physical_equal_to_pre_mutation': rollback_validation['physical_equal_to_pre_mutation'],
                'logical_equivalence_basis': rollback_validation['logical_equivalence_basis'],
            })
            retrieval_summary.append({
                'cycle_number': cycle_number,
                'status': 'PASS',
                'controls': [
                    {
                        'name': item['control']['name'],
                        'expected': item['control'].get('expected_top_path') or item['control'].get('expected_status') or 'no_regression:no_coverage',
                        'ok': item['ok'],
                    }
                    for item in apply_validation['retrieval_results']
                ],
                'reopen_after_apply_pid': apply_validation['reopen_cmd']['pid'],
                'reopen_after_rollback_pid': rollback_validation['reopen_cmd']['pid'],
            })
        overall_status = 'LC5_PASS'
    except Exception as exc:
        blocker = f'{type(exc).__name__}: {exc}'
        if baseline_info is None:
            overall_status = 'LC5_FAIL_BASELINE'
        else:
            overall_status = 'LC5_FAIL'
            failed_cycle = len(cycle_summaries) + 1
            cycle_summaries.append({'cycle_number': failed_cycle, 'status': 'FAIL', 'error': blocker})
    production = production_check()
    production_ok = (
        production['embedded_json_sha256'] == EXPECTED_EMBEDDED and
        production['chroma_sqlite3_sha256'] == EXPECTED_CHROMA and
        production['total_chunks'] == EXPECTED_CHUNKS and
        production['active_generation_selection']['unchanged'] is True
    )
    production_report = {
        'status': 'production_unchanged_after_lc5' if production_ok else 'production_changed_or_unverified_after_lc5',
        **production,
    }
    write_json_atomic(PKG / 'lc5_apply_validation_summary.json', {
        'status': overall_status,
        'cycles': apply_summary,
        'expected_after_apply': {
            'chunks': EXPECTED_CHUNKS_AFTER_APPLY,
            'orphan_paths': EXPECTED_ORPHANS_AFTER_APPLY,
            'me_c_delta': 444,
            'maker_manuals_delta': -446,
            'total_chunk_delta': -2,
        },
    })
    write_json_atomic(PKG / 'lc5_rollback_validation_summary.json', {
        'status': overall_status,
        'cycles': rollback_summary,
        'expected_after_rollback': {
            'chunks': EXPECTED_CHUNKS,
            'orphan_paths': EXPECTED_ORPHANS,
            'embedded_json_sha256': EXPECTED_EMBEDDED,
            'chroma_sqlite3_sha256': EXPECTED_CHROMA,
        },
    })
    write_json_atomic(PKG / 'lc5_retrieval_controls_report.json', {
        'status': overall_status,
        'cycles': retrieval_summary,
    })
    write_json_atomic(PKG / 'lc5_production_unchanged_report.json', production_report)
    final_report = {
        'status': overall_status,
        'lc5_parent': str(lc5_parent) if lc5_parent else None,
        'baseline_validation': baseline_info,
        'cycle_results': cycle_summaries,
        'apply_summary': apply_summary,
        'rollback_summary': rollback_summary,
        'retrieval_summary': retrieval_summary,
        'cleanup_summary': cleanup_summary,
        'production_report': production_report,
        'blocker': blocker,
    }
    write_json_atomic(PKG / 'lc5_five_cycle_report.json', final_report)
    write_sha256sums()
    print(json.dumps({
        'status': overall_status,
        'cycles_completed': len([c for c in cycle_summaries if c['status'] == 'PASS']),
        'lc5_parent': str(lc5_parent) if lc5_parent else None,
        'blocker': blocker,
    }, indent=2))


if __name__ == '__main__':
    main()

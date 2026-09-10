#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from orphan_repair_common import (
    DISPOSABLE_MARKER,
    InterruptGuard,
    IngestLockError,
    PackageError,
    RETIRE_CHUNKS,
    RETIRE_DIGEST,
    TargetRefusal,
    acquire_lock_for,
    answer_diagnostics,
    artifact_inventory,
    chunk_rows_by_source_hash,
    count_embeddings,
    create_disposable_clone_marker,
    doctor_json,
    expected_scope_counts,
    file_sha256,
    inventory_checksum_from_rows,
    inventory_rows,
    parse_orphan_count,
    release_lock,
    scope_stats_json,
    tracker_load,
    tree_hashes,
    validate_disposable_clone_marker,
    validate_no_placeholders,
    worker_json,
    write_json_atomic,
)

PKG_DEFAULT = Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z')
LOCKFILE_NAME = 'ingest.lock'
READ_ACTIVITY_FILES = {'ask_events.jsonl'}


class ExecutorRefusal(PackageError):
    pass


def canonical_manifest_sha(manifest: dict[str, Any]) -> str:
    payload = {k: v for k, v in manifest.items() if k != 'manifest_payload_sha256'}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def load_package(package_dir: Path) -> dict[str, Any]:
    manifest = json.loads((package_dir / 'atomic_orphan_repair_manifest.json').read_text())
    baseline = json.loads((package_dir / 'production_baseline.json').read_text())
    transitions = json.loads((package_dir / 'collection_transition_report.json').read_text())
    acceptance = json.loads((package_dir / 'retrieval_acceptance_tests.json').read_text())
    return {
        'manifest': manifest,
        'baseline': baseline,
        'transitions': transitions,
        'acceptance': acceptance,
    }


def log_append(log_path: Path, event: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')


def worker_process_record(gen_dir: Path, action: str, request: dict[str, Any] | None = None, timeout: int = 600, extra_args: list[str] | None = None) -> dict[str, Any]:
    request = request or {}
    temp_dir = Path(tempfile.mkdtemp(prefix='orphan_repair_worker_req_'))
    req_path = temp_dir / f'{action}.json'
    write_json_atomic(req_path, request)
    cmd = [sys.executable, str(PKG_DEFAULT / 'chroma_worker.py'), action, '--gen-dir', str(gen_dir), '--request-file', str(req_path), *(extra_args or [])]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    payload = None
    parse_error = None
    stdout = proc.stdout.strip()
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            parse_error = str(exc)
    else:
        parse_error = 'empty stdout'
    return {
        'cmd': cmd,
        'returncode': proc.returncode,
        'stdout': proc.stdout,
        'stderr': proc.stderr,
        'json_payload': payload,
        'json_parse_error': parse_error,
        'request_file': str(req_path),
    }


def verify_manifest_and_scope(pkg: dict[str, Any]) -> None:
    manifest = pkg['manifest']
    if manifest.get('schema_version') != 'orphan-repair-manifest-v2':
        raise ExecutorRefusal('manifest schema_version mismatch')
    if manifest.get('manifest_payload_sha256') != canonical_manifest_sha(manifest):
        raise ExecutorRefusal('manifest payload checksum mismatch')
    bad = validate_no_placeholders(manifest)
    if bad:
        raise ExecutorRefusal(f'manifest contains placeholder/ellipsis fields: {bad[:10]}')
    counts = expected_scope_counts(manifest)
    if counts != {
        'deterministic_stale_path_occurrences': 54,
        'deterministic_digests': 40,
        'retirement_digests': 1,
        'affected_digests_total': 41,
    }:
        raise ExecutorRefusal(f'unexpected manifest counts: {counts}')
    if int(manifest['counts'].get('total_orphan_path_occurrences', -1)) != 55:
        raise ExecutorRefusal('manifest total_orphan_path_occurrences != 55')
    if len(manifest['operations']) != 41:
        raise ExecutorRefusal('manifest operations count != 41')


def verify_target_is_disposable(gen_dir: Path, baseline_hashes: dict[str, str]) -> dict[str, Any]:
    marker = validate_disposable_clone_marker(gen_dir)
    current = {k: v for k, v in tree_hashes(gen_dir, exclude_relpaths={LOCKFILE_NAME}).items() if k != DISPOSABLE_MARKER}
    expected = {k: v for k, v in baseline_hashes.items() if k not in {DISPOSABLE_MARKER, LOCKFILE_NAME}}
    if current != expected:
        raise ExecutorRefusal('current generation file hashes do not match approved baseline')
    return marker


def verify_preconditions(gen_dir: Path, pkg: dict[str, Any], log_path: Path) -> dict[str, Any]:
    manifest = pkg['manifest']
    baseline = pkg['baseline']
    verify_manifest_and_scope(pkg)
    marker = verify_target_is_disposable(gen_dir, baseline['file_hashes'])

    if (gen_dir / 'chroma.sqlite3-wal').exists() or (gen_dir / 'chroma.sqlite3-shm').exists():
        raise ExecutorRefusal('unexpected WAL/SHM present before mutation')

    tracker = tracker_load(gen_dir)
    for op in manifest['operations']:
        digest = op['digest']
        chunk_ids = list(op['chunk_ids'])
        if op['op_type'] != 'RETIRE_UNRECOVERABLE':
            canonical = op['canonical_live_path']
            if any(x in canonical for x in ('...', '<', '>')):
                raise ExecutorRefusal(f'canonical path contains placeholder: {canonical}')
            full = Path(manifest['production']['library_root']) / canonical
            if not full.is_file():
                raise ExecutorRefusal(f'canonical file missing: {canonical}')
            if file_sha256(full) != digest or op['verified_live_sha256'] != digest:
                raise ExecutorRefusal(f'canonical file sha mismatch: {canonical}')
            conflict_digests = []
            for other_digest, rec in tracker.items():
                if other_digest == digest:
                    continue
                if canonical in (rec.get('paths') or []):
                    conflict_digests.append(other_digest)
            if conflict_digests:
                raise ExecutorRefusal(f'unhandled tracker conflict for {canonical}: {conflict_digests}')
        rows = worker_json(gen_dir, 'inspect', {'ids': chunk_ids, 'include_embeddings': False})['payload']
        if sorted(rows) != sorted(chunk_ids):
            raise ExecutorRefusal(f'chunk ids missing in Chroma for digest {digest}')
        source_hash_ids = sorted(worker_json(gen_dir, 'source_hash_rows', {'digest': digest})['chunk_ids'])
        if source_hash_ids != sorted(chunk_ids):
            raise ExecutorRefusal(f'tracker chunk ids and Chroma source_hash rows differ for {digest}')
        for cid in chunk_ids:
            meta = rows[cid]['metadata']
            if meta.get('source_hash') != digest:
                raise ExecutorRefusal(f'source_hash mismatch for {digest} {cid}')
        if op['op_type'] == 'RETIRE_UNRECOVERABLE':
            if sorted(chunk_ids) != sorted(RETIRE_CHUNKS):
                raise ExecutorRefusal('retirement chunk list mismatch')
            owners = [d for d, rec in tracker.items() if any(cid in (rec.get('chunk_ids') or []) for cid in chunk_ids)]
            if sorted(set(owners)) != [RETIRE_DIGEST]:
                raise ExecutorRefusal(f'retirement chunks shared by other digests: {owners}')
    log_append(log_path, {'event': 'preconditions_ok', 'marker_clone_id': marker['clone_id']})
    return {'pre_hashes': tree_hashes(gen_dir), 'baseline_total_chunks': int(baseline['actual_total_chunk_count_sqlite']), 'marker': marker}


def snapshot_generation(gen_dir: Path, work_dir: Path) -> Path:
    snap = work_dir / 'generation_snapshot'
    if snap.exists():
        shutil.rmtree(snap)
    shutil.copytree(gen_dir, snap, copy_function=shutil.copy2, ignore=shutil.ignore_patterns(LOCKFILE_NAME))
    return snap


def restore_generation(gen_dir: Path, snapshot_dir: Path) -> None:
    if not snapshot_dir.exists():
        raise PackageError(f'snapshot missing: {snapshot_dir}')
    tmp_restore = gen_dir.parent / f'{gen_dir.name}.restore_tmp'
    if tmp_restore.exists():
        shutil.rmtree(tmp_restore)
    shutil.copytree(snapshot_dir, tmp_restore, copy_function=shutil.copy2)
    if gen_dir.exists():
        shutil.rmtree(gen_dir)
    tmp_restore.rename(gen_dir)


def mutate_generation(gen_dir: Path, pkg: dict[str, Any], work_dir: Path, log_path: Path, inject: str | None = None, case_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    manifest = pkg['manifest']
    snapshot_dir = snapshot_generation(gen_dir, work_dir)
    static_hashes_before = {
        k: v for k, v in tree_hashes(gen_dir, exclude_relpaths={LOCKFILE_NAME}).items()
        if k not in {'embedded.json', 'chroma.sqlite3', DISPOSABLE_MARKER, *READ_ACTIVITY_FILES}
    }
    if inject == 'failure_before_first_mutation':
        raise PackageError('injected failure before first mutation')
    worker_request: dict[str, Any] = {'operations': manifest['operations'], 'inject': inject}
    if inject == 'lc6_case20_malformed_worker_output':
        if case_id != 'LC6-20-worker-timeout-or-malformed-output' or not run_id:
            raise ExecutorRefusal('LC6-20 malformed-output injection requires exact case_id and run_id')
        state_dir = work_dir / 'lc6_case20_malformed_worker_output'
        state_dir.mkdir(parents=True, exist_ok=True)
        worker_request['inject'] = None
        worker_request['lc6_case20_malformed_output'] = {
            'case_id': case_id,
            'run_id': run_id,
            'clone_path': str(gen_dir.resolve()),
            'state_dir': str(state_dir.resolve()),
            'expected_worker_action': 'apply_manifest_ops',
        }
        write_json_atomic(work_dir / 'lc6_case20_worker_request.json', worker_request['lc6_case20_malformed_output'])
    result = worker_json(gen_dir, 'apply_manifest_ops', worker_request, timeout=900)
    write_json_atomic(gen_dir / 'embedded.json', result['tracker_after'])
    if inject == 'interruption_during_execution':
        raise KeyboardInterrupt('injected interrupt')
    log_append(log_path, {'event': 'mutation_worker_ok', 'worker_pid': result['worker_pid']})
    return {'snapshot_dir': str(snapshot_dir), 'static_hashes_before': static_hashes_before, 'retired': bool(result.get('retired')), 'worker': result}


def validate_success(gen_dir: Path, pkg: dict[str, Any], mutation_info: dict[str, Any], log_path: Path, inject: str | None = None) -> dict[str, Any]:
    manifest = pkg['manifest']
    baseline = pkg['baseline']
    transitions = pkg['transitions']
    acceptance = pkg['acceptance']

    if inject == 'forced_doctor_failure':
        raise ExecutorRefusal('injected forced doctor failure')

    doctor = doctor_json(gen_dir)
    stats = scope_stats_json(gen_dir)
    orphan_count = parse_orphan_count(doctor)
    total_chunks = count_embeddings(gen_dir / 'chroma.sqlite3')
    if doctor.get('status') != 'PASS':
        raise ExecutorRefusal('doctor.status != PASS')
    if orphan_count != 0:
        raise ExecutorRefusal(f'orphan count expected 0 got {orphan_count}')
    if total_chunks != int(baseline['actual_total_chunk_count_sqlite']) - 2:
        raise ExecutorRefusal(f'total chunks expected baseline-2 got {total_chunks}')

    tracker = tracker_load(gen_dir)
    validation_rows = worker_json(gen_dir, 'validate_manifest_chunks', {'operations': manifest['operations'], 'include_embeddings': False}, timeout=900)['rows']
    rows_by_digest = {row['digest']: row for row in validation_rows}
    moved_chunks = 0
    for op in manifest['operations']:
        digest = op['digest']
        row = rows_by_digest[digest]
        if op['op_type'] == 'RETIRE_UNRECOVERABLE':
            if digest in tracker:
                raise ExecutorRefusal('retirement digest still in tracker')
            if row['remaining_chunk_ids']:
                raise ExecutorRefusal(f'retirement chunks still present: {row["remaining_chunk_ids"]}')
            continue
        if tracker[digest]['paths'] != [op['canonical_live_path']]:
            raise ExecutorRefusal(f'tracker path not canonical for {digest}')
        if tracker[digest].get('collection') != op['derived_collection']:
            raise ExecutorRefusal(f'tracker collection not updated for {digest}')
        first = row['first_chunk']
        if not first or first['metadata'].get('source') != op['canonical_live_path']:
            raise ExecutorRefusal(f'source metadata incorrect for {digest}')
        if first['metadata'].get('collection') != op['derived_collection']:
            raise ExecutorRefusal(f'collection metadata incorrect for {digest}')
        if first['metadata'].get('source_hash') != digest:
            raise ExecutorRefusal(f'source_hash drift for {digest}')
        if op['tracker_collection'] != op['derived_collection']:
            moved_chunks += len(op['chunk_ids'])

    if moved_chunks != int(transitions['summary']['collection_change_chunk_total']):
        raise ExecutorRefusal('collection transition chunk total mismatch')

    static_hashes_after = {
        k: v for k, v in tree_hashes(gen_dir, exclude_relpaths={LOCKFILE_NAME}).items()
        if k not in {'embedded.json', 'chroma.sqlite3', DISPOSABLE_MARKER, *READ_ACTIVITY_FILES}
    }
    changed = []
    before = mutation_info['static_hashes_before']
    keys = sorted(set(before) | set(static_hashes_after))
    for key in keys:
        if before.get(key) != static_hashes_after.get(key):
            changed.append({
                'path': key,
                'before': before.get(key),
                'after': static_hashes_after.get(key),
            })

    results = []
    for ctrl in acceptance['positive_controls']:
        r = answer_diagnostics(gen_dir, ctrl['query'], ctrl['scope'])
        top = r['sources'][0]['path'] if r['sources'] else None
        ok = r['status'] == 'ok' and top == ctrl['expected_top_path']
        results.append({'control': ctrl, 'result': r, 'ok': ok})
        if not ok:
            raise ExecutorRefusal(f'positive retrieval control failed: {ctrl["name"]}')
    for ctrl in acceptance['negative_controls']:
        r = answer_diagnostics(gen_dir, ctrl['query'], ctrl['scope'])
        ok = r['status'] == ctrl['expected_status']
        results.append({'control': ctrl, 'result': r, 'ok': ok})
        if not ok:
            raise ExecutorRefusal(f'negative retrieval control failed: {ctrl["name"]}')
    for ctrl in acceptance['no_regression_controls']:
        r = answer_diagnostics(gen_dir, ctrl['original_query'], ctrl['intended_scope'])
        ok = r['status'] == 'no_coverage' and r['gate'] == 'final_confidence_failed'
        results.append({'control': ctrl, 'result': r, 'ok': ok})
        if not ok:
            raise ExecutorRefusal(f'no-regression retrieval control failed: {ctrl["name"]}')
    if inject == 'forced_retrieval_failure':
        raise ExecutorRefusal('injected forced retrieval acceptance failure')
    log_append(log_path, {'event': 'success_validated'})
    return {
        'doctor': doctor,
        'scope_stats': stats,
        'orphan_count': orphan_count,
        'total_chunks': total_chunks,
        'retrieval_controls': results,
        'post_hashes': tree_hashes(gen_dir),
        'non_sqlite_physical_changes': changed,
    }


def run_apply(package_dir: Path, gen_dir: Path, work_dir: Path, inject: str | None = None, case_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    pkg = load_package(package_dir)
    log_path = work_dir / 'execution_log.jsonl'
    mutation_info: dict[str, Any] | None = None
    write_json_atomic(work_dir / 'executor_input.json', {'package_dir': str(package_dir), 'gen_dir': str(gen_dir), 'inject': inject, 'case_id': case_id, 'run_id': run_id})
    lock_ctx = None
    old_env = None
    try:
        with InterruptGuard():
            lock_ctx, old_env = acquire_lock_for(gen_dir, timeout_s=0)
            log_append(log_path, {'event': 'lock_acquired'})
            pre = verify_preconditions(gen_dir, pkg, log_path)
            mutation_info = mutate_generation(gen_dir, pkg, work_dir, log_path, inject=inject, case_id=case_id, run_id=run_id)
            post = validate_success(gen_dir, pkg, mutation_info, log_path, inject=inject)
            result = {'status': 'APPLY_OK', 'inject': inject, 'preconditions': pre, 'validation': post, 'artifact_inventory': artifact_inventory(work_dir)}
            write_json_atomic(work_dir / 'apply_result.json', result)
            return result
    except (ExecutorRefusal, IngestLockError, PackageError, KeyboardInterrupt, TargetRefusal) as exc:
        if mutation_info and mutation_info.get('snapshot_dir'):
            restore_generation(gen_dir, Path(mutation_info['snapshot_dir']))
            log_append(log_path, {'event': 'restored_snapshot_after_failure'})
        result = {'status': 'APPLY_REJECTED', 'inject': inject, 'error_type': type(exc).__name__, 'error': str(exc), 'gen_hashes_after_rejection': tree_hashes(gen_dir)}
        write_json_atomic(work_dir / 'apply_failure.json', result)
        return result
    finally:
        if lock_ctx is not None and old_env is not None:
            release_lock(lock_ctx, old_env)
            log_append(log_path, {'event': 'lock_released'})


def run_rollback(gen_dir: Path, work_dir: Path) -> dict[str, Any]:
    validate_disposable_clone_marker(gen_dir)
    snapshot_dir = work_dir / 'generation_snapshot'
    restore_generation(gen_dir, snapshot_dir)
    result = {
        'status': 'ROLLBACK_OK',
        'post_restore_hashes': tree_hashes(gen_dir),
        'post_restore_total_chunks': count_embeddings(gen_dir / 'chroma.sqlite3'),
        'post_restore_doctor': doctor_json(gen_dir),
    }
    write_json_atomic(work_dir / 'rollback_result.json', result)
    return result


def run_interrupt_probe(package_dir: Path, gen_dir: Path, work_dir: Path, case_id: str, run_id: str, state_dir: Path) -> dict[str, Any]:
    validate_disposable_clone_marker(gen_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_dir.resolve() != work_dir.resolve() and work_dir.resolve() not in state_dir.resolve().parents:
        raise ExecutorRefusal('state_dir must remain within work_dir')
    log_path = work_dir / 'execution_log.jsonl'
    pkg = load_package(package_dir=package_dir)
    selected_op = next(op for op in pkg['manifest']['operations'] if op['op_type'] != 'RETIRE_UNRECOVERABLE')
    selected_chunk_id = str(selected_op['chunk_ids'][0])
    approved_chunk = {
        'digest': selected_op['digest'],
        'canonical_live_path': selected_op['canonical_live_path'],
        'tracker_collection': selected_op['tracker_collection'],
        'derived_collection': selected_op['derived_collection'],
        'selected_chunk_id': selected_chunk_id,
        'authorization_basis': 'first bounded chunk ID from approved manifest deterministic affected set',
    }
    snapshot_dir = snapshot_generation(gen_dir, work_dir)
    pre_hashes = tree_hashes(gen_dir)
    pre_collection = worker_process_record(
        gen_dir,
        'collection_summary',
        {'selected_chunk_id': selected_chunk_id},
        timeout=120,
    )
    pre_payload = pre_collection['json_payload'] or {}
    if pre_collection['returncode'] != 0 or pre_payload.get('status') != 'ok':
        raise ExecutorRefusal('interrupt-probe collection summary failed before mutation')
    if pre_payload.get('collection_count') != 1:
        raise ExecutorRefusal(f"interrupt-probe requires exactly one existing collection, found {pre_payload.get('collection_names')}")
    if pre_payload.get('selected_chunk_exists') is not True:
        raise ExecutorRefusal(f'interrupt-probe approved chunk missing: {selected_chunk_id}')
    interrupt_request = {
        'selected_chunk_id': selected_chunk_id,
        'expected_collection_name': pre_payload.get('collection_names', [None])[0],
        'interrupt_context': {
            'state_dir': str(state_dir),
            'case_id': case_id,
            'run_id': run_id,
            'clone_path': str(gen_dir.resolve()),
        },
    }
    worker = worker_process_record(
        gen_dir,
        'case11_interrupt_probe',
        interrupt_request,
        timeout=180,
        extra_args=['--case-id', case_id, '--run-id', run_id, '--state-dir', str(state_dir)],
    )
    checkpoint_path = state_dir / 'mutation_checkpoint.json'
    signal_path = state_dir / 'signal_received.json'
    worker_failure_observed = worker['returncode'] != 0
    recovered = False
    if worker_failure_observed:
        restore_generation(gen_dir, snapshot_dir)
        recovered = True
        log_append(log_path, {'event': 'restored_snapshot_after_interrupt_probe_failure', 'worker_returncode': worker['returncode']})
    post_collection = worker_process_record(
        gen_dir,
        'collection_summary',
        {'selected_chunk_id': selected_chunk_id},
        timeout=120,
    )
    post_payload = post_collection['json_payload'] or {}
    result = {
        'status': 'INTERRUPT_PROBE_RECOVERED' if recovered and checkpoint_path.exists() and signal_path.exists() else 'INTERRUPT_PROBE_FAILED',
        'case_id': case_id,
        'run_id': run_id,
        'approved_chunk': approved_chunk,
        'pre_collection_summary': pre_payload,
        'worker_process': worker,
        'checkpoint_path': str(checkpoint_path),
        'checkpoint_exists': checkpoint_path.exists(),
        'signal_path': str(signal_path),
        'signal_exists': signal_path.exists(),
        'executor_recovery_performed': recovered,
        'rollback_owner': 'executor' if recovered else 'none',
        'pre_hashes': pre_hashes,
        'post_hashes': tree_hashes(gen_dir),
        'post_collection_summary': post_payload,
        'selected_chunk_metadata_after_recovery': post_payload.get('selected_chunk_metadata'),
    }
    write_json_atomic(work_dir / 'interrupt_probe_result.json', result)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['apply', 'rollback', 'mark-disposable', 'interrupt-probe'])
    ap.add_argument('--package-dir', default=str(PKG_DEFAULT))
    ap.add_argument('--gen-dir', required=True)
    ap.add_argument('--work-dir')
    ap.add_argument('--inject')
    ap.add_argument('--source-generation')
    ap.add_argument('--purpose')
    ap.add_argument('--source-inventory-checksum')
    ap.add_argument('--case-id')
    ap.add_argument('--run-id')
    ap.add_argument('--state-dir')
    args = ap.parse_args()

    package_dir = Path(args.package_dir)
    gen_dir = Path(args.gen_dir)

    if args.action == 'mark-disposable':
        if not args.source_generation or not args.purpose or not args.source_inventory_checksum:
            raise SystemExit('mark-disposable requires --source-generation --purpose --source-inventory-checksum')
        payload = create_disposable_clone_marker(gen_dir, Path(args.source_generation), args.purpose, args.source_inventory_checksum)
        print(json.dumps(payload, indent=2))
        return

    if not args.work_dir:
        raise SystemExit('--work-dir required for apply/rollback')
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.action == 'apply':
        payload = run_apply(package_dir, gen_dir, work_dir, inject=args.inject, case_id=args.case_id, run_id=args.run_id)
    elif args.action == 'interrupt-probe':
        if not args.case_id or not args.run_id or not args.state_dir:
            raise SystemExit('interrupt-probe requires --case-id --run-id --state-dir')
        payload = run_interrupt_probe(package_dir, gen_dir, work_dir, args.case_id, args.run_id, Path(args.state_dir))
    else:
        payload = run_rollback(gen_dir, work_dir)
    print(json.dumps(payload, indent=2))


if __name__ == '__main__':
    main()

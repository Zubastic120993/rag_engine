#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine')
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rag_engine.config import persist_dir  # type: ignore
from rag_engine.lock import ingest_lock, ingest_lock_file  # type: ignore

PKG = Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z')
PROD_GEN = Path('/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_20260814T182037Z_698e0df44604').resolve()
DISPOSABLE_MARKER = '.orphan_repair_disposable_clone.json'
EXCLUDE = {'ingest.lock'}
EXPECTED_EMBEDDED = 'c51c8816585dce66449be2828e4bf0b9e06cba16a5189d8ea2634e3d6ed8af33'
EXPECTED_CHROMA = '71ad868ad404fd8a489c9b57213e37be291a4461753f0ec0ffa288476a1451f6'
EXPECTED_CHUNKS = 125291
EXPECTED_ORPHANS = 55


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def count_chunks(sqlite_path: Path) -> int:
    conn = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True)
    try:
        return int(conn.execute('select count(*) from embeddings').fetchone()[0])
    finally:
        conn.close()


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


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


def path_type(path: Path) -> str:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return 'symlink'
    if stat.S_ISREG(st.st_mode):
        return 'file'
    if stat.S_ISDIR(st.st_mode):
        return 'dir'
    return 'other'


def inventory(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if rel in EXCLUDE:
            continue
        typ = path_type(path)
        if typ == 'dir':
            continue
        row: dict[str, Any] = {'path': rel, 'type': typ}
        if typ == 'symlink':
            target = os.readlink(path)
            target_path = Path(target)
            resolved = (path.parent / target_path).resolve() if not target_path.is_absolute() else target_path.resolve()
            row['target'] = target
            row['resolved_target'] = str(resolved)
            if not str(resolved).startswith(str(root)):
                raise RuntimeError(f'unsafe external symlink: {rel} -> {resolved}')
            raise RuntimeError(f'symlink not allowed in generation snapshot: {rel}')
        row['bytes'] = path.stat().st_size
        row['sha256'] = file_sha256(path)
        total_bytes += int(row['bytes'])
        rows.append(row)
    return {
        'root': str(root),
        'file_count': len(rows),
        'total_bytes': total_bytes,
        'rows': rows,
        'checksum': hashlib.sha256(canonical_json_bytes(rows)).hexdigest(),
    }


def atomic_rename(src: Path, dst: Path) -> None:
    if dst.exists():
        raise RuntimeError(f'refusing to overwrite existing clone: {dst}')
    src.rename(dst)


def selection_info() -> dict[str, Any]:
    resolved = persist_dir().resolve()
    return {
        'mechanism': 'rag_engine persist_dir() with explicit task-set RAG_DB_PATH during lock/copy; default fallback is library_root/.rag_db',
        'requested_generation': str(PROD_GEN),
        'resolved_persist_dir': str(resolved),
        'matches_requested_generation': resolved == PROD_GEN,
    }


def manifest_checksum() -> str:
    return file_sha256(PKG / 'atomic_orphan_repair_manifest.json')


def marker_payload(final_clone: Path, source_checksum: str) -> dict[str, Any]:
    return {
        'clone_id': f'lc4-{final_clone.name}',
        'source_generation': str(PROD_GEN),
        'creation_time_utc': utc_now(),
        'purpose': 'LC4_BASELINE_FOR_LC5',
        'source_inventory_checksum': source_checksum,
        'source_core_hashes': {
            'embedded.json': EXPECTED_EMBEDDED,
            'chroma.sqlite3': EXPECTED_CHROMA,
        },
        'expected_chunk_count': EXPECTED_CHUNKS,
        'expected_orphan_count': EXPECTED_ORPHANS,
        'package_manifest_checksum': manifest_checksum(),
        'status': 'VERIFIED_DISPOSABLE_BASELINE',
        'disposable': True,
        'unpromoted': True,
        'target_generation': str(final_clone.resolve()),
    }


def write_sha256sums() -> None:
    files = sorted(p for p in PKG.iterdir() if p.is_file() and p.name != 'SHA256SUMS')
    lines = [f'{file_sha256(p)}  {p.name}' for p in files]
    (PKG / 'SHA256SUMS').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> None:
    pre_embedded = file_sha256(PROD_GEN / 'embedded.json')
    pre_chroma = file_sha256(PROD_GEN / 'chroma.sqlite3')
    pre_chunks = count_chunks(PROD_GEN / 'chroma.sqlite3')
    if pre_embedded != EXPECTED_EMBEDDED or pre_chroma != EXPECTED_CHROMA or pre_chunks != EXPECTED_CHUNKS:
        result = {
            'status': 'LC4_STALE_BASELINE',
            'production': {
                'embedded.json': pre_embedded,
                'chroma.sqlite3': pre_chroma,
                'total_chunks': pre_chunks,
                'active_generation_selection': selection_info(),
            },
        }
        write_json_atomic(PKG / 'lc4_snapshot_report.json', result)
        print(json.dumps(result, indent=2))
        return

    old_env = os.environ.copy()
    os.environ['RAG_DB_PATH'] = str(PROD_GEN)
    acquired_at = utc_now()
    final_clone: Path | None = None
    temp_clone: Path | None = None
    source_inv_before: dict[str, Any] | None = None
    clone_inv: dict[str, Any] | None = None
    source_inv_after: dict[str, Any] | None = None
    lock_path_str: str | None = None
    failure: str | None = None
    try:
        with ingest_lock(timeout_s=0):
            lock_path = ingest_lock_file().resolve()
            lock_path_str = str(lock_path)
            if lock_path.parent != PROD_GEN:
                raise RuntimeError(f'lock path not in production generation: {lock_path}')
            if (PROD_GEN / 'chroma.sqlite3-wal').exists() or (PROD_GEN / 'chroma.sqlite3-shm').exists():
                raise RuntimeError('unsafe WAL/SHM present during snapshot')
            if not selection_info()['matches_requested_generation']:
                raise RuntimeError('active generation selection changed before snapshot')
            source_inv_before = inventory(PROD_GEN)
            temp_parent = Path(tempfile.mkdtemp(prefix='lc4_baseline_parent_', dir='/private/tmp'))
            temp_clone = temp_parent / f'raggen_lc4_baseline_tmp_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}'
            shutil.copytree(PROD_GEN, temp_clone, copy_function=shutil.copy2, symlinks=True, ignore=shutil.ignore_patterns('ingest.lock'))
            clone_inv = inventory(temp_clone)
            source_inv_after = inventory(PROD_GEN)
            if source_inv_before['checksum'] != source_inv_after['checksum']:
                raise RuntimeError('production inventory changed during copy')
            if source_inv_before['rows'] != clone_inv['rows']:
                raise RuntimeError('clone inventory does not match source inventory')
            final_clone = temp_parent / f'raggen_lc4_baseline_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}'
            atomic_rename(temp_clone, final_clone)
            marker = marker_payload(final_clone, source_inv_before['checksum'])
            write_json_atomic(final_clone / DISPOSABLE_MARKER, marker)
    except Exception as exc:
        failure = f'{type(exc).__name__}: {exc}'
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    if failure is not None:
        fail_report = {
            'status': 'LC4_FAIL',
            'resolved_production_path': str(PROD_GEN),
            'active_generation_selection': selection_info(),
            'lock': {
                'path': lock_path_str,
                'acquired_at_utc': acquired_at,
                'result': 'failed_released_or_not_acquired',
                'belongs_to_production_generation': bool(lock_path_str and Path(lock_path_str).parent == PROD_GEN),
            },
            'wal_shm': {
                'chroma.sqlite3-wal': (PROD_GEN / 'chroma.sqlite3-wal').exists(),
                'chroma.sqlite3-shm': (PROD_GEN / 'chroma.sqlite3-shm').exists(),
            },
            'source_inventory_checksum_before': source_inv_before['checksum'] if source_inv_before else None,
            'source_inventory_checksum_after': source_inv_after['checksum'] if source_inv_after else None,
            'clone_inventory_checksum': clone_inv['checksum'] if clone_inv else None,
            'final_disposable_clone_path': str(final_clone) if final_clone else None,
            'error': failure,
            'no_production_mutation_performed': True,
        }
        write_json_atomic(PKG / 'lc4_source_inventory.json', {
            'lock_path': lock_path_str,
            'source_inventory_before': source_inv_before,
            'source_inventory_after': source_inv_after,
        })
        write_json_atomic(PKG / 'lc4_clone_inventory.json', {
            'clone_path': str(final_clone) if final_clone else None,
            'clone_inventory': clone_inv,
        })
        write_json_atomic(PKG / 'lc4_snapshot_report.json', fail_report)
        write_json_atomic(PKG / 'lc4_production_unchanged_report.json', {
            'status': 'LC4_FAIL',
            'active_generation_selection': selection_info(),
            'embedded.json': file_sha256(PROD_GEN / 'embedded.json'),
            'chroma.sqlite3': file_sha256(PROD_GEN / 'chroma.sqlite3'),
            'total_chunks': count_chunks(PROD_GEN / 'chroma.sqlite3'),
            'matches_expected': file_sha256(PROD_GEN / 'embedded.json') == EXPECTED_EMBEDDED and file_sha256(PROD_GEN / 'chroma.sqlite3') == EXPECTED_CHROMA and count_chunks(PROD_GEN / 'chroma.sqlite3') == EXPECTED_CHUNKS,
            'expected_orphan_count_reference': EXPECTED_ORPHANS,
            'no_production_mutation_performed': True,
            'error': failure,
        })
        write_sha256sums()
        print(json.dumps({'status': 'LC4_FAIL', 'error': failure}, indent=2))
        return

    post_embedded = file_sha256(PROD_GEN / 'embedded.json')
    post_chroma = file_sha256(PROD_GEN / 'chroma.sqlite3')
    post_chunks = count_chunks(PROD_GEN / 'chroma.sqlite3')

    marker = json.loads((final_clone / DISPOSABLE_MARKER).read_text(encoding='utf-8')) if final_clone else None
    marker_sha = file_sha256(final_clone / DISPOSABLE_MARKER) if final_clone else None

    write_json_atomic(PKG / 'lc4_source_inventory.json', {
        'lock_path': str((PROD_GEN / 'ingest.lock').resolve()),
        'source_inventory_before': source_inv_before,
        'source_inventory_after': source_inv_after,
    })
    write_json_atomic(PKG / 'lc4_clone_inventory.json', {
        'clone_path': str(final_clone) if final_clone else None,
        'clone_inventory': clone_inv,
    })
    snap_report = {
        'status': 'LC4_PASS',
        'resolved_production_path': str(PROD_GEN),
        'active_generation_selection': selection_info(),
        'lock': {
            'path': str((PROD_GEN / 'ingest.lock').resolve()),
            'acquired_at_utc': acquired_at,
            'result': 'acquired_and_released',
            'belongs_to_production_generation': True,
        },
        'wal_shm': {
            'chroma.sqlite3-wal': False,
            'chroma.sqlite3-shm': False,
            'result': 'clear',
        },
        'source_inventory_checksum_before': source_inv_before['checksum'] if source_inv_before else None,
        'source_inventory_checksum_after': source_inv_after['checksum'] if source_inv_after else None,
        'clone_inventory_checksum': clone_inv['checksum'] if clone_inv else None,
        'inventory_match': bool(source_inv_before and clone_inv and source_inv_before['rows'] == clone_inv['rows']),
        'source_stable_during_copy': bool(source_inv_before and source_inv_after and source_inv_before['rows'] == source_inv_after['rows']),
        'file_count': source_inv_before['file_count'] if source_inv_before else None,
        'total_bytes': source_inv_before['total_bytes'] if source_inv_before else None,
        'final_disposable_clone_path': str(final_clone) if final_clone else None,
        'marker': marker,
        'marker_sha256': marker_sha,
    }
    prod_report = {
        'status': 'production_unchanged_after_lc4',
        'active_generation_selection': selection_info(),
        'embedded.json': post_embedded,
        'chroma.sqlite3': post_chroma,
        'total_chunks': post_chunks,
        'matches_expected': post_embedded == EXPECTED_EMBEDDED and post_chroma == EXPECTED_CHROMA and post_chunks == EXPECTED_CHUNKS,
        'expected_orphan_count_reference': EXPECTED_ORPHANS,
        'no_production_mutation_performed': True,
    }
    write_json_atomic(PKG / 'lc4_snapshot_report.json', snap_report)
    write_json_atomic(PKG / 'lc4_production_unchanged_report.json', prod_report)
    write_sha256sums()
    print(json.dumps({
        'status': 'LC4_PASS',
        'clone_path': str(final_clone),
        'source_checksum': source_inv_before['checksum'] if source_inv_before else None,
        'clone_checksum': clone_inv['checksum'] if clone_inv else None,
    }, indent=2))


if __name__ == '__main__':
    main()

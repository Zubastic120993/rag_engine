#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from orphan_repair_common import (
    CHROMA_WORKER,
    DISPOSABLE_MARKER,
    PROD_GEN,
    PROD_LIBRARY,
    PROD_GEN,
    RAG_PY,
    create_disposable_clone_marker,
    file_sha256,
    inventory_checksum_from_rows,
    inventory_rows,
    tree_hashes,
    write_json_atomic,
)

PKG = Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z')
REPORT_PATH = PKG / 'lifecycle_ten_sequence_smoke_test.json'
REFUSAL_PATH = PKG / 'production_target_refusal_test.json'
REFACTOR_PATH = PKG / 'subprocess_lifecycle_refactor_report.json'
BASELINE_PATH = PKG / 'lifecycle_production_baseline.json'


def run_worker(action: str, gen_dir: Path, request: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    req_dir = Path(tempfile.mkdtemp(prefix='lc3_worker_req_'))
    req = req_dir / f'{action}.json'
    write_json_atomic(req, request)
    proc = subprocess.run([str(RAG_PY), str(CHROMA_WORKER), action, '--gen-dir', str(gen_dir), '--request-file', str(req)], capture_output=True, text=True, timeout=timeout, check=False)
    payload = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = {'malformed_stdout': proc.stdout}
    return {'action': action, 'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr, 'payload': payload}


def make_baseline_temp_db(root: Path) -> tuple[Path, dict[str, Any]]:
    base = root / 'baseline_db'
    base.mkdir(parents=True, exist_ok=True)
    marker = create_disposable_clone_marker(base, PROD_GEN, 'lc3 lifecycle smoke baseline temp db', 'temp-seed-baseline')
    seed = run_worker('seed_test_collection', base, {'collection_name': 'test', 'id': 'a', 'document': 'alpha', 'metadata': {'source': 'x', 'collection': 't'}, 'embedding': [0.1, 0.2, 0.3]})
    if seed['returncode'] != 0:
        raise RuntimeError(seed)
    marker = json.loads((base / DISPOSABLE_MARKER).read_text())
    marker['source_inventory_checksum'] = inventory_checksum_from_rows(inventory_rows(base, exclude_relpaths={DISPOSABLE_MARKER}))
    write_json_atomic(base / DISPOSABLE_MARKER, marker)
    return base, marker


def run_smoke() -> dict[str, Any]:
    tmp_root = Path(tempfile.mkdtemp(prefix='lc3_smoke_'))
    baseline_db, marker = make_baseline_temp_db(tmp_root)
    baseline_hashes = tree_hashes(baseline_db)
    sequences = []
    for idx in range(1, 11):
        seq_dir = tmp_root / f'seq_{idx:02d}'
        shutil.copytree(baseline_db, seq_dir, copy_function=shutil.copy2)
        marker_data = json.loads((seq_dir / DISPOSABLE_MARKER).read_text())
        marker_data['clone_id'] = f"{marker_data['clone_id']}-seq-{idx:02d}"
        marker_data['purpose'] = f'lc3 smoke sequence {idx:02d}'
        write_json_atomic(seq_dir / DISPOSABLE_MARKER, marker_data)
        inspect_before = run_worker('inspect', seq_dir, {'ids': ['a'], 'include_embeddings': False})
        update = run_worker('mutate_metadata', seq_dir, {'ids': ['a'], 'metadatas': [{'source': f'seq-{idx:02d}', 'collection': 't'}]})
        inspect_after = run_worker('inspect', seq_dir, {'ids': ['a'], 'include_embeddings': False})
        after_source = None
        if inspect_after.get('payload') and inspect_after['payload'].get('status') == 'ok':
            after_source = inspect_after['payload']['payload']['a']['metadata'].get('source')
        sequences.append({
            'sequence': idx,
            'gen_dir': str(seq_dir),
            'inspect_before': inspect_before,
            'update': update,
            'inspect_after': inspect_after,
            'expected_source_after': f'seq-{idx:02d}',
            'observed_source_after': after_source,
            'ok': inspect_before['returncode'] == 0 and update['returncode'] == 0 and inspect_after['returncode'] == 0 and after_source == f'seq-{idx:02d}',
        })
    overall_ok = all(x['ok'] for x in sequences)
    return {
        'tmp_root': str(tmp_root),
        'baseline_db': str(baseline_db),
        'baseline_marker': marker,
        'baseline_hashes': baseline_hashes,
        'sequence_count': len(sequences),
        'sequences': sequences,
        'overall_ok': overall_ok,
    }


def run_refusals() -> dict[str, Any]:
    forbidden = [
        PROD_GEN,
        PROD_LIBRARY / '.rag_db',
        PROD_LIBRARY / '.rag_db_generations',
        PROD_LIBRARY,
    ]
    rows = []
    for path in forbidden:
        result = run_worker('inspect', path, {'ids': ['a']})
        payload = result.get('payload') or {}
        rows.append({
            'target': str(path),
            'returncode': result['returncode'],
            'status': payload.get('status'),
            'opened_chroma': payload.get('opened_chroma'),
            'reason': payload.get('reason'),
            'ok': result['returncode'] == 3 and payload.get('status') == 'refused' and payload.get('opened_chroma') is False,
        })
    return {'results': rows, 'overall_ok': all(r['ok'] for r in rows)}


def run_refactor_report(smoke: dict[str, Any], refusal: dict[str, Any]) -> dict[str, Any]:
    baseline = json.loads(BASELINE_PATH.read_text())
    return {
        'status': 'lc3_refactor_complete' if smoke['overall_ok'] and refusal['overall_ok'] else 'lc3_refactor_incomplete',
        'architecture': {
            'parent_opens_chroma': False,
            'worker_script': str(CHROMA_WORKER),
            'worker_actions_used_in_lc3': ['seed_test_collection', 'inspect', 'mutate_metadata'],
            'executor_uses_worker_for': ['precondition_chunk_inspection', 'source_hash_checks', 'manifest_ops_mutation', 'post_mutation_chunk_validation', 'retrieval_checks'],
            'disposable_clone_marker': DISPOSABLE_MARKER,
            'worker_boundary': 'one subprocess per bounded Chroma operation; natural process exit is the lifecycle boundary',
            'forbidden_targets': [str(PROD_GEN), str(PROD_LIBRARY / '.rag_db'), str(PROD_LIBRARY / '.rag_db_generations'), str(PROD_LIBRARY)],
        },
        'production_hashes_rechecked': {
            'embedded_json_sha256': file_sha256(PROD_GEN / 'embedded.json'),
            'chroma_sqlite3_sha256': file_sha256(PROD_GEN / 'chroma.sqlite3'),
            'matches_initial_baseline': file_sha256(PROD_GEN / 'embedded.json') == baseline['embedded_json_sha256'] and file_sha256(PROD_GEN / 'chroma.sqlite3') == baseline['chroma_sqlite3_sha256'],
        },
        'smoke_overall_ok': smoke['overall_ok'],
        'refusal_overall_ok': refusal['overall_ok'],
    }


def main() -> None:
    smoke = run_smoke()
    refusal = run_refusals()
    refactor = run_refactor_report(smoke, refusal)
    write_json_atomic(REPORT_PATH, smoke)
    write_json_atomic(REFUSAL_PATH, refusal)
    write_json_atomic(REFACTOR_PATH, refactor)
    print(json.dumps({'smoke_ok': smoke['overall_ok'], 'refusal_ok': refusal['overall_ok']}, indent=2))


if __name__ == '__main__':
    main()

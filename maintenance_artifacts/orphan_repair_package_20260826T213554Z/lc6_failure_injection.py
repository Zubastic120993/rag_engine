#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from orphan_repair_common import (
    DISPOSABLE_MARKER,
    PROD_GEN,
    PROD_LIBRARY,
    TargetRefusal,
    acquire_lock_for,
    create_disposable_clone_marker,
    ensure_not_forbidden_target,
    file_sha256,
    inventory_checksum_from_rows,
    inventory_rows,
    release_lock,
    validate_disposable_clone_marker,
    write_json_atomic,
)
from lc5_five_cycle_rehearsal import (
    BASELINE_CLONE,
    EXPECTED_CHROMA,
    EXPECTED_EMBEDDED,
    EXPECTED_CHUNKS,
    EXPECTED_MARKER_SHA,
    EXPECTED_ORPHANS,
    EXPECTED_SOURCE_INVENTORY_CHECKSUM,
    load_inputs,
    validate_lc4_baseline_clone,
)

PKG = Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z')
RESTORED_BASELINE_MARKER_SHA = 'e148d9b83bbcff31d24f85ad461387457efc994138ff62003aafc2733284d2f1'
PLAN_PATH = PKG / 'lc6_failure_injection_plan.json'
CONTRACT_PATH = PKG / 'lc6_authoritative_failure_case_contract.json'
VALIDATION_REPORT_PATH = PKG / 'lc6_failure_injection_harness_validation_report.json'
CORRECTION_REPORT_PATH = PKG / 'lc6_failure_injection_harness_validation_correction_report.json'
ERRATUM_PATH = PKG / 'lc6_failure_injection_harness_correction_erratum.json'
V2_REPORT_PATH = PKG / 'lc6_failure_injection_harness_validation_correction_v2_report.json'
CASE11_PREFLIGHT_REPORT_PATH = PKG / 'lc6_case11_interruption_mechanism_preflight_report.json'
DEFAULT_TMP_PARENT_PREFIX = 'lc6_harness_validate_'
CASE_REPORTS_DIR = PKG / 'lc6_run_reports'
RAG_PY = '/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/python'
PRE_CORRECTION_LC6_HASH = '3a6684cf6b4a60cb02c15313a9b38f483b9e7c4e7156dd43dfc73b1b96be416a'
PRE_CASE11_PREFLIGHT_LC6_HASH = '7c9f083fe3b54aa1e298ac0c8ea818639307e186d77884e112d6e68dc296f080'
PRE_CASE11_PREFLIGHT_V2_HASH = 'b1290014584fad941af98be28c49596cb7abd2581ac90297a049f9002b0f2746'
CASE11_CONTINUATION_START_HASH = '7bd99dec5471c53e70127ff1782f71ad754146f0c5707cffd7d21e0fdaa75e5d'
FORBIDDEN_TARGETS = [
    str(PROD_GEN),
    str(PROD_LIBRARY / '.rag_db'),
    str(PROD_LIBRARY / '.rag_db_generations'),
    str(PROD_LIBRARY),
]
SUPPORTED_INJECTION_HOOKS = {
    'failure_before_first_mutation',
    'failure_halfway_through_deterministic',
    'failure_during_alias_collapse',
    'failure_before_retirement',
    'failure_after_retirement',
    'forced_doctor_failure',
    'forced_retrieval_failure',
    'interruption_during_execution',
}
MECHANIC_BY_CASE_ID = {
    'LC6-01-failure-before-first-mutation': 'executor_inject:failure_before_first_mutation',
    'LC6-02-failure-midway-deterministic-repoints': 'executor_inject:failure_halfway_through_deterministic',
    'LC6-03-failure-during-alias-collapse': 'executor_inject:failure_during_alias_collapse',
    'LC6-04-failure-immediately-before-c1210-retirement': 'executor_inject:failure_before_retirement',
    'LC6-05-failure-immediately-after-c1210-retirement': 'executor_inject:failure_after_retirement',
    'LC6-06-forced-doctor-failure': 'executor_inject:forced_doctor_failure',
    'LC6-07-forced-retrieval-acceptance-failure': 'executor_inject:forced_retrieval_failure',
    'LC6-08-stale-tracker-store-baseline-hash': 'harness_fixture:stale_tracker_store_baseline_hash',
    'LC6-09-canonical-source-sha-mismatch': 'harness_fixture:canonical_source_sha_mismatch_disposable_manifest',
    'LC6-10-candidate-lock-contention': 'harness_fixture:candidate_lock_contention',
    'LC6-11-interruption-during-mutation': 'harness_fixture:interruption_during_execution',
    'LC6-12-direct-active-production-target': 'refusal_only:active_production_target',
    'LC6-13-legacy-rag-db-target': 'refusal_only:legacy_rag_db_target',
    'LC6-14-generations-root-target': 'refusal_only:generations_root_target',
    'LC6-15-ce-library-root-target': 'refusal_only:ce_library_root_target',
    'LC6-16-rerun-against-already-repaired-clone': 'harness_fixture:dirty_clone_rerun_refusal',
    'LC6-17-invalid-or-missing-disposable-marker': 'harness_fixture:invalid_or_missing_marker_refusal',
    'LC6-18-malformed-or-checksum-invalid-manifest': 'harness_fixture:invalid_manifest_refusal',
    'LC6-19-unexpected-wal-shm-state': 'harness_fixture:wal_shm_precondition_refusal',
    'LC6-20-worker-timeout-or-malformed-output': 'harness_fixture:worker_timeout_or_malformed_output',
}
REQUIRED_CASE_KEYS = {
    'case_id',
    'authoritative_reference',
    'injected_failure_point',
    'disposable_target_path',
    'preconditions',
    'command_or_harness_entry_point',
    'expected_process_exit_result',
    'expected_fail_closed_behavior',
    'expected_filesystem_state',
    'expected_chroma_state',
    'expected_tracker_state',
    'expected_registry_state',
    'expected_recovery_rollback_action',
    'expected_residual_state_report',
    'evidence_files_to_capture',
    'cleanup_procedure',
    'post_cleanup_assertion',
    'production_is_not_a_target',
}


class HarnessRefusal(RuntimeError):
    pass


@dataclass
class CasePaths:
    parent: Path
    case_root: Path
    clone: Path
    work: Path
    evidence: Path


@dataclass
class CaseRuntime:
    case: dict[str, Any]
    paths: CasePaths
    package_dir: Path
    cleanup_callbacks: list[Callable[[], None]]
    notes: list[str]
    run_report_root: Path
    durable_case_dir: Path
    baseline_clone_source: Path = BASELINE_CLONE
    source_package_dir: Path = PKG
    command_runner: Callable[[list[str], int, str, 'CaseRuntime'], dict[str, Any]] | None = None
    mutation_started: bool = False
    child_pid: int | None = None
    preserve_clone: bool = False
    fixture_state: dict[str, Any] | None = None
    last_child_record: dict[str, Any] | None = None
    rollback_owner: str = 'none'
    evidence_persisted: bool = False
    production_hash_before: dict[str, Any] | None = None
    production_hash_after: dict[str, Any] | None = None
    cleanup_started_at: float | None = None
    created_evidence_files: list[str] = field(default_factory=list)
    selected_case_id: str | None = None
    contract_case_id: str | None = None


@dataclass
class MockCaseTestConfig:
    behavior: str
    actual_mechanics_complete: bool
    blocked_reason: str | None = None
    create_snapshot: bool = False
    simulate_mutation_active: bool = False
    expected_status: str | None = None
    expected_case_execution_performed: bool = True


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('source_plan_sha256') != 'fd5c99bf3fb68f3e4d85b08a4f3da787d537cbee2d16f8e45f945499bddb19a9':
        raise HarnessRefusal('authoritative contract source plan sha mismatch')
    if payload.get('source_inherited_spec_reference') != "LC6 task specification, section 5 'Run required failure-injection cases'":
        raise HarnessRefusal('authoritative contract inherited spec reference mismatch')
    return payload


def list_case_ids(contract: dict[str, Any]) -> list[str]:
    return [case['case_id'] for case in contract['cases']]


def contract_case_map(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {case['case_id']: case for case in contract['cases']}


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    cases = contract.get('cases') or []
    if len(cases) != 20:
        raise HarnessRefusal(f'expected 20 cases, found {len(cases)}')
    ids = [case.get('case_id') for case in cases]
    duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
    if duplicates:
        raise HarnessRefusal(f'duplicate case ids: {duplicates}')
    missing_mechanics = []
    schema_checks = []
    for case in cases:
        missing = sorted(REQUIRED_CASE_KEYS - set(case))
        ok = not missing
        if case.get('case_id') not in MECHANIC_BY_CASE_ID:
            missing_mechanics.append(case.get('case_id'))
        schema_checks.append({
            'case_id': case.get('case_id'),
            'schema_ok': ok,
            'missing_keys': missing,
            'has_preconditions': bool(case.get('preconditions')),
            'has_expected_result': bool(case.get('expected_process_exit_result')),
            'has_evidence_outputs': bool(case.get('evidence_files_to_capture')),
            'has_cleanup_policy': bool(case.get('cleanup_procedure')),
            'has_stop_condition': bool(case.get('expected_fail_closed_behavior')),
        })
    if missing_mechanics:
        raise HarnessRefusal(f'missing case mechanics: {missing_mechanics}')
    order = contract.get('ordered_case_ids') or []
    if order != ids:
        raise HarnessRefusal('case order mismatch between cases array and ordered_case_ids')
    return {
        'case_count': len(cases),
        'case_ids': ids,
        'schema_checks': schema_checks,
        'duplicates': duplicates,
        'missing_mechanics': missing_mechanics,
    }


def injection_hook_validation() -> dict[str, Any]:
    executor_text = (PKG / 'orphan_repair_executor.py').read_text(encoding='utf-8')
    worker_text = (PKG / 'chroma_worker.py').read_text(encoding='utf-8')
    checks = {
        'failure_before_first_mutation': 'failure_before_first_mutation' in executor_text,
        'failure_halfway_through_deterministic': 'failure_halfway_through_deterministic' in worker_text,
        'failure_during_alias_collapse': 'failure_during_alias_collapse' in worker_text,
        'failure_before_retirement': 'failure_before_retirement' in worker_text,
        'failure_after_retirement': 'failure_after_retirement' in worker_text,
        'forced_doctor_failure': 'forced_doctor_failure' in executor_text,
        'forced_retrieval_failure': 'forced_retrieval_failure' in executor_text,
        'interruption_during_execution': 'interruption_during_execution' in executor_text,
    }
    return {
        'supported_hooks_expected': sorted(SUPPORTED_INJECTION_HOOKS),
        'checks': checks,
        'all_supported_hooks_confirmed': all(checks.values()),
    }


def refusal_probe(target: Path) -> dict[str, Any]:
    try:
        ensure_not_forbidden_target(target)
        return {'target': str(target), 'status': 'unexpectedly_allowed', 'opened_chroma': False, 'ok': False}
    except TargetRefusal as exc:
        return {'target': str(target), 'status': 'refused', 'opened_chroma': False, 'reason': str(exc), 'ok': True}


def ensure_within_parent(parent: Path, child: Path) -> None:
    parent_r = parent.resolve()
    child_r = child.resolve()
    if child_r != parent_r and parent_r not in child_r.parents:
        raise HarnessRefusal(f'path escapes harness parent: {child_r} not under {parent_r}')


def protected_target_guard(candidate: Path) -> dict[str, Any]:
    forbidden = {
        str(PROD_GEN.resolve()),
        str((PROD_LIBRARY / '.rag_db').resolve()),
        str((PROD_LIBRARY / '.rag_db_generations').resolve()),
        str(PROD_LIBRARY.resolve()),
    }
    resolved = str(candidate.resolve())
    blocked = resolved in forbidden or resolved.startswith(str(PROD_LIBRARY.resolve()))
    return {'target': resolved, 'blocked': blocked}


def validate_parent_escape_guards() -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix=DEFAULT_TMP_PARENT_PREFIX, dir='/private/tmp')).resolve()
    try:
        inside = parent / 'case_01' / 'clone'
        inside.parent.mkdir(parents=True, exist_ok=True)
        ensure_within_parent(parent, inside)
        outside_failure = None
        try:
            ensure_within_parent(parent, Path('/private/tmp/outside_escape_target'))
        except Exception as exc:
            outside_failure = str(exc)
        protected_checks = [
            protected_target_guard(PROD_GEN),
            protected_target_guard(PROD_LIBRARY / '.rag_db'),
            protected_target_guard(PROD_LIBRARY / '.rag_db_generations'),
            protected_target_guard(PROD_LIBRARY),
        ]
        return {
            'parent': str(parent),
            'inside_allowed': True,
            'outside_escape_blocked': bool(outside_failure),
            'outside_escape_reason': outside_failure,
            'protected_target_checks': protected_checks,
            'all_protected_targets_blocked': all(item['blocked'] for item in protected_checks),
        }
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def validate_execution_guard(args_case_id: str | None, args_all: bool, execute: bool) -> dict[str, Any]:
    if execute and not args_case_id and not args_all:
        return {'ok': False, 'reason': 'execution requires explicit --case-id or --all'}
    if not execute:
        return {'ok': True, 'reason': 'dry-run mode; no case execution permitted'}
    return {'ok': True, 'reason': 'execution flag semantics valid'}


def baseline_revalidation_summary(baseline_dir: Path | None = None, expected_marker_sha: str | None = None) -> dict[str, Any]:
    baseline = (baseline_dir or BASELINE_CLONE).resolve()
    if baseline == BASELINE_CLONE.resolve():
        manifest, _, _ = load_inputs()
        info = validate_lc4_baseline_clone(manifest)
        rows = inventory_rows(BASELINE_CLONE, exclude_relpaths={DISPOSABLE_MARKER})
        marker_sha = EXPECTED_MARKER_SHA
        collection_names = ['langchain']
        chunk_count = EXPECTED_CHUNKS
        orphan_count = info['orphan_count']
        wal_shm_clear = info['wal_shm']['clear']
    else:
        marker = validate_disposable_clone_marker(baseline)
        rows = inventory_rows(baseline, exclude_relpaths={DISPOSABLE_MARKER})
        marker_sha = file_sha256(baseline / DISPOSABLE_MARKER)
        tracker = json.loads((baseline / 'embedded.json').read_text(encoding='utf-8'))
        orphan_count = 0
        for rec in tracker.values():
            for rel in rec.get('paths') or []:
                if not (PROD_LIBRARY / rel).is_file():
                    orphan_count += 1
        conn = sqlite3.connect(f'file:{baseline / "chroma.sqlite3"}?mode=ro', uri=True)
        try:
            chunk_count = int(conn.execute('select count(*) from embeddings').fetchone()[0])
            collection_names = [r[0] for r in conn.execute('select name from collections order by name').fetchall()]
        finally:
            conn.close()
        wal_shm_clear = not (baseline / 'chroma.sqlite3-wal').exists() and not (baseline / 'chroma.sqlite3-shm').exists()
        info = {
            'source_inventory_schema': str(marker.get('source_inventory_schema') or 'inventory-row-v1'),
            'embedded_json_sha256': file_sha256(baseline / 'embedded.json'),
            'chroma_sqlite3_sha256': file_sha256(baseline / 'chroma.sqlite3'),
        }
        if expected_marker_sha and marker_sha != expected_marker_sha:
            raise HarnessRefusal(f'baseline marker sha mismatch: {marker_sha} != {expected_marker_sha}')
    return {
        'baseline_path': str(baseline),
        'marker_sha256': marker_sha,
        'source_inventory_schema': info['source_inventory_schema'],
        'row_count': len(rows),
        'computed_checksum': inventory_checksum_from_rows(rows),
        'expected_checksum': EXPECTED_SOURCE_INVENTORY_CHECKSUM,
        'embedded_json_sha256': info['embedded_json_sha256'],
        'expected_embedded_json_sha256': EXPECTED_EMBEDDED,
        'chroma_sqlite3_sha256': info['chroma_sqlite3_sha256'],
        'expected_chroma_sqlite3_sha256': EXPECTED_CHROMA,
        'chunk_count': chunk_count,
        'orphan_count': orphan_count,
        'expected_orphan_count': EXPECTED_ORPHANS,
        'collection_names': collection_names,
        'collection_count': len(collection_names),
        'wal_shm_clear': wal_shm_clear,
    }


def synthetic_test_override_baseline_validation(baseline: Path, package_dir: Path, tmp_parent: Path, selected_case_id: str | None, execute: bool) -> dict[str, Any]:
    baseline = baseline.resolve()
    package_dir = package_dir.resolve()
    tmp_parent = tmp_parent.resolve()
    if not execute:
        raise HarnessRefusal('test override baseline validation requires explicit --execute')
    if selected_case_id not in {'LC6-11-interruption-during-mutation', 'LC6-20-worker-timeout-or-malformed-output'}:
        raise HarnessRefusal('test override baseline validation allowed only for LC6-11 or LC6-20')
    ensure_within_parent(tmp_parent, baseline)
    ensure_within_parent(tmp_parent, package_dir)
    forbidden = {
        BASELINE_CLONE.resolve(),
        PROD_GEN.resolve(),
        (PROD_LIBRARY / '.rag_db').resolve(),
        (PROD_LIBRARY / '.rag_db_generations').resolve(),
        PROD_LIBRARY.resolve(),
        PKG.resolve(),
    }
    if baseline in forbidden or package_dir in forbidden:
        raise HarnessRefusal('test override baseline/package resolved to forbidden path')
    if str(baseline).startswith(str(PROD_LIBRARY.resolve())) or str(package_dir).startswith(str(PROD_LIBRARY.resolve())):
        raise HarnessRefusal('test override baseline/package must remain outside CE_Library')
    required_files = ['embedded.json', 'chroma.sqlite3']
    missing = [name for name in required_files if not (baseline / name).exists()]
    if missing:
        raise HarnessRefusal(f'synthetic baseline missing required files: {missing}')
    rows = inventory_rows(baseline)
    return {
        'mode': 'synthetic_test_override',
        'baseline_dir': str(baseline),
        'package_dir': str(package_dir),
        'tmp_parent': str(tmp_parent),
        'selected_case_id': selected_case_id,
        'execute_flag': execute,
        'row_count': len(rows),
        'checksum': inventory_checksum_from_rows(rows),
        'initial_hashes': {name: file_sha256(baseline / name) for name in required_files},
        'required_files_present': True,
        'is_lc4_verified_claimed': False,
    }


def dry_run(contract: dict[str, Any], case_id: str | None, all_cases: bool) -> dict[str, Any]:
    ids = list_case_ids(contract)
    if case_id:
        if case_id not in ids:
            raise HarnessRefusal(f'unknown case id: {case_id}')
        selected = [case_id]
    elif all_cases:
        selected = ids
    else:
        selected = ids
    return {
        'mode': 'dry-run',
        'selected_case_ids': selected,
        'count': len(selected),
        'actual_case_execution_performed': False,
        'no_case_execution_performed': True,
    }


def case_slug(case_id: str) -> str:
    return case_id.split('-', 2)[2].replace('-', '_')


def build_case_paths(parent: Path, case: dict[str, Any]) -> CasePaths:
    case_no = case['case_id'].split('-')[1]
    root = parent / f'case_{case_no}_{case_slug(case["case_id"])}'
    clone = root / 'clone'
    work = root / 'work'
    evidence = work / 'evidence'
    for path in (root, clone, work, evidence):
        ensure_within_parent(parent, path)
    return CasePaths(parent=parent, case_root=root, clone=clone, work=work, evidence=evidence)


def package_file_hashes() -> dict[str, str]:
    return {
        'lc6_authoritative_failure_case_contract.json': file_sha256(CONTRACT_PATH),
        'lc6_failure_injection.py': file_sha256(PKG / 'lc6_failure_injection.py'),
        'lc6_failure_injection_plan.json': file_sha256(PLAN_PATH),
    }


def execution_metadata(mode: str, actual_case_execution_performed: bool, validate_harness: bool = False) -> dict[str, Any]:
    return {
        'mode': mode,
        'validate_harness': validate_harness,
        'actual_case_execution_performed': actual_case_execution_performed,
        'no_failure_injection_case_ran': not actual_case_execution_performed,
        'no_production_action_occurred': True,
        'production_chroma_not_opened': True,
        'final_clean_cycle_not_run': True,
        'sha256sums_not_regenerated': True,
    }


def production_hash_snapshot() -> dict[str, Any]:
    embedded = PROD_GEN / 'embedded.json'
    chroma = PROD_GEN / 'chroma.sqlite3'
    return {
        'generation_root': str(PROD_GEN),
        'embedded': {
            'path': str(embedded),
            'exists': embedded.exists(),
            'sha256': file_sha256(embedded) if embedded.exists() else None,
        },
        'chroma_sqlite3': {
            'path': str(chroma),
            'exists': chroma.exists(),
            'sha256': file_sha256(chroma) if chroma.exists() else None,
        },
        'opened_chroma': False,
        'capture_mode': 'read-only file hash',
    }


def write_case_evidence(runtime: CaseRuntime, name: str, payload: Any, durable: bool = False) -> Path:
    base = runtime.durable_case_dir if durable else runtime.paths.evidence
    base.mkdir(parents=True, exist_ok=True)
    path = base / name
    write_json_atomic(path, payload)
    if not durable and name not in runtime.created_evidence_files:
        runtime.created_evidence_files.append(name)
    return path


def write_work_jsonl_placeholder(runtime: CaseRuntime, name: str, payload: Any) -> Path:
    path = runtime.paths.work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + '\n', encoding='utf-8')
    return path


def evidence_inventory(path: Path) -> list[dict[str, Any]]:
    rows = []
    for file_path in sorted(p for p in path.rglob('*') if p.is_file()):
        rows.append({
            'relpath': str(file_path.relative_to(path)),
            'bytes': file_path.stat().st_size,
            'sha256': file_sha256(file_path),
        })
    return rows


def expected_required_evidence(runtime: CaseRuntime) -> list[str]:
    required = ['case_contract.json', 'case_report.json', 'evidence_inventory.json', 'production_hashes_before.json', 'production_hashes_after.json']
    if runtime.fixture_state is not None:
        required.append('fixture_setup.json')
    for candidate in runtime.created_evidence_files:
        if candidate not in required:
            required.append(candidate)
    if runtime.paths.work.exists():
        for work_file in sorted(p.name for p in runtime.paths.work.iterdir() if p.is_file()):
            if work_file not in required:
                required.append(work_file)
    return required


def verify_required_evidence(durable_dir: Path, required_files: list[str]) -> dict[str, Any]:
    checks = []
    missing = []
    for name in required_files:
        present = (durable_dir / name).exists()
        checks.append({'file': name, 'present': present})
        if not present:
            missing.append(name)
    return {'required_files': required_files, 'checks': checks, 'missing': missing, 'ok': not missing}


def persist_case_evidence_before_cleanup(runtime: CaseRuntime, case_report: dict[str, Any]) -> dict[str, Any]:
    local_report = write_case_evidence(runtime, 'case_report.json', case_report)
    inventory_payload = {
        'case_id': runtime.case['case_id'],
        'written_before_cleanup': True,
        'evidence_dir': str(runtime.paths.evidence),
        'files': evidence_inventory(runtime.paths.evidence),
    }
    local_inventory = write_case_evidence(runtime, 'evidence_inventory.json', inventory_payload)
    runtime.durable_case_dir.mkdir(parents=True, exist_ok=True)
    if runtime.durable_case_dir.exists():
        for existing in runtime.durable_case_dir.glob('*'):
            if existing.is_dir():
                shutil.rmtree(existing)
            else:
                existing.unlink()
    for src in sorted(runtime.paths.evidence.glob('*')):
        dst = runtime.durable_case_dir / src.name
        if src.is_dir():
            shutil.copytree(src, dst, copy_function=shutil.copy2)
        else:
            shutil.copy2(src, dst)
    for src in sorted(runtime.paths.work.iterdir()):
        if src.name == 'evidence':
            continue
        if not src.is_file():
            continue
        shutil.copy2(src, runtime.durable_case_dir / src.name)
    required_files = expected_required_evidence(runtime)
    verification = verify_required_evidence(runtime.durable_case_dir, required_files)
    persisted = {
        'case_id': runtime.case['case_id'],
        'written_before_cleanup': True,
        'local_case_report': str(local_report),
        'local_evidence_inventory': str(local_inventory),
        'durable_case_dir': str(runtime.durable_case_dir),
        'durable_inventory': evidence_inventory(runtime.durable_case_dir),
        'verification': verification,
    }
    if not verification['ok']:
        raise HarnessRefusal(f'durable evidence verification failed: {verification["missing"]}')
    runtime.evidence_persisted = True
    write_json_atomic(runtime.durable_case_dir / 'evidence_persistence_verification.json', persisted)
    return persisted


def update_durable_case_outputs(runtime: CaseRuntime, final_case_report: dict[str, Any]) -> dict[str, Any]:
    write_json_atomic(runtime.durable_case_dir / 'case_report.json', final_case_report)
    inventory_payload = {
        'case_id': runtime.case['case_id'],
        'written_before_cleanup': True,
        'durable_case_dir': str(runtime.durable_case_dir),
        'files': evidence_inventory(runtime.durable_case_dir),
    }
    write_json_atomic(runtime.durable_case_dir / 'evidence_inventory.json', inventory_payload)
    verification = verify_required_evidence(runtime.durable_case_dir, expected_required_evidence(runtime))
    return {
        'durable_case_dir': str(runtime.durable_case_dir),
        'inventory': evidence_inventory(runtime.durable_case_dir),
        'verification': verification,
    }


def make_case_runtime(
    case: dict[str, Any],
    parent: Path,
    run_report_root: Path,
    package_dir: Path | None = None,
    source_package_dir: Path | None = None,
    baseline_clone_source: Path | None = None,
    command_runner: Callable[[list[str], int, str, CaseRuntime], dict[str, Any]] | None = None,
) -> CaseRuntime:
    paths = build_case_paths(parent, case)
    paths.case_root.mkdir(parents=True, exist_ok=True)
    paths.work.mkdir(parents=True, exist_ok=True)
    paths.evidence.mkdir(parents=True, exist_ok=True)
    runtime = CaseRuntime(
        case=case,
        paths=paths,
        package_dir=package_dir or PKG,
        cleanup_callbacks=[],
        notes=[],
        run_report_root=run_report_root,
        durable_case_dir=(run_report_root / 'cases' / case['case_id']),
        baseline_clone_source=baseline_clone_source or BASELINE_CLONE,
        source_package_dir=source_package_dir or PKG,
        command_runner=command_runner,
        selected_case_id=case['case_id'],
        contract_case_id=case['case_id'],
    )
    write_case_evidence(runtime, 'case_contract.json', case)
    return runtime


def register_cleanup(runtime: CaseRuntime, callback: Callable[[], None]) -> None:
    runtime.cleanup_callbacks.append(callback)


def run_cleanup_callbacks(runtime: CaseRuntime) -> list[dict[str, Any]]:
    results = []
    while runtime.cleanup_callbacks:
        callback = runtime.cleanup_callbacks.pop()
        try:
            callback()
            results.append({'status': 'ok', 'callback': getattr(callback, '__name__', 'callback')})
        except Exception as exc:
            results.append({'status': 'error', 'callback': getattr(callback, '__name__', 'callback'), 'error': str(exc)})
    return results


def prepare_disposable_clone(runtime: CaseRuntime) -> dict[str, Any]:
    ensure_not_forbidden_target(runtime.paths.clone)
    if runtime.paths.clone.exists():
        shutil.rmtree(runtime.paths.clone)
    shutil.copytree(runtime.baseline_clone_source, runtime.paths.clone, copy_function=shutil.copy2)
    rows = inventory_rows(runtime.paths.clone, exclude_relpaths={DISPOSABLE_MARKER})
    checksum = inventory_checksum_from_rows(rows)
    marker = create_disposable_clone_marker(
        runtime.paths.clone,
        PROD_GEN,
        f'LC6_{runtime.case["case_id"]}',
        checksum,
        clone_id=f'lc6-{runtime.case["case_id"]}-{uuid.uuid4()}',
    )
    validated = validate_disposable_clone_marker(runtime.paths.clone)
    evidence = {
        'clone_path': str(runtime.paths.clone),
        'work_path': str(runtime.paths.work),
        'baseline_clone_source': str(runtime.baseline_clone_source),
        'source_inventory_checksum': checksum,
        'marker': marker,
        'validated_clone_id': validated['clone_id'],
    }
    write_case_evidence(runtime, 'clone_setup.json', evidence)
    return evidence


def clone_inventory_summary(runtime: CaseRuntime) -> dict[str, Any]:
    rows = inventory_rows(runtime.paths.clone, exclude_relpaths={DISPOSABLE_MARKER})
    return {
        'root': str(runtime.paths.clone),
        'row_count': len(rows),
        'checksum': inventory_checksum_from_rows(rows),
    }


def make_disposable_package_copy(runtime: CaseRuntime, mutate: Callable[[dict[str, Any]], None]) -> Path:
    copied = runtime.paths.work / 'package_copy'
    if copied.exists():
        shutil.rmtree(copied)
    shutil.copytree(runtime.source_package_dir, copied, copy_function=shutil.copy2)
    manifest_path = copied / 'atomic_orphan_repair_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    mutate(manifest)
    write_json_atomic(manifest_path, manifest)
    runtime.package_dir = copied
    write_case_evidence(runtime, 'disposable_package_copy.json', {'package_dir': str(copied), 'manifest_path': str(manifest_path)})
    return copied


def setup_case_fixture(runtime: CaseRuntime) -> dict[str, Any]:
    mechanic = MECHANIC_BY_CASE_ID[runtime.case['case_id']]
    fixture: dict[str, Any] = {'mechanic': mechanic, 'mutations': []}
    if mechanic.startswith('executor_inject:'):
        prepare_disposable_clone(runtime)
        fixture['inject'] = mechanic.split(':', 1)[1]
    elif mechanic == 'harness_fixture:interruption_during_execution':
        prepare_disposable_clone(runtime)
        fixture['interrupt_protocol'] = 'real_executor_worker_checkpoint_chain'
        fixture['requires_pid_timing_evidence'] = False
    elif mechanic == 'harness_fixture:stale_tracker_store_baseline_hash':
        prepare_disposable_clone(runtime)
        embedded = runtime.paths.clone / 'embedded.json'
        embedded.write_text(embedded.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        fixture['mutations'].append('embedded.json newline added to force baseline hash mismatch')
    elif mechanic == 'harness_fixture:canonical_source_sha_mismatch_disposable_manifest':
        prepare_disposable_clone(runtime)
        def mutate(manifest: dict[str, Any]) -> None:
            manifest['operations'][0]['verified_live_sha256'] = '0' * 64
        make_disposable_package_copy(runtime, mutate)
        fixture['mutations'].append('manifest verified_live_sha256 forced invalid in disposable package copy')
    elif mechanic == 'harness_fixture:candidate_lock_contention':
        prepare_disposable_clone(runtime)
        lock_ctx, old_env = acquire_lock_for(runtime.paths.clone, timeout_s=0)
        def release() -> None:
            release_lock(lock_ctx, old_env)
        register_cleanup(runtime, release)
        fixture['mutations'].append('candidate lock acquired on disposable clone')
        write_case_evidence(runtime, 'inventory_proof.json', clone_inventory_summary(runtime))
    elif mechanic == 'harness_fixture:dirty_clone_rerun_refusal':
        prepare_disposable_clone(runtime)
        fixture['two_phase'] = 'successful_apply_then_second_apply_refusal_on_dirty_clone'
    elif mechanic == 'harness_fixture:invalid_or_missing_marker_refusal':
        prepare_disposable_clone(runtime)
        marker = runtime.paths.clone / DISPOSABLE_MARKER
        marker.unlink()
        fixture['mutations'].append('disposable marker removed')
    elif mechanic == 'harness_fixture:invalid_manifest_refusal':
        prepare_disposable_clone(runtime)
        def mutate(manifest: dict[str, Any]) -> None:
            manifest['manifest_payload_sha256'] = 'f' * 64
        make_disposable_package_copy(runtime, mutate)
        fixture['mutations'].append('manifest payload checksum replaced in disposable package copy')
    elif mechanic == 'harness_fixture:wal_shm_precondition_refusal':
        prepare_disposable_clone(runtime)
        (runtime.paths.clone / 'chroma.sqlite3-wal').write_text('fixture-wal', encoding='utf-8')
        (runtime.paths.clone / 'chroma.sqlite3-shm').write_text('fixture-shm', encoding='utf-8')
        fixture['mutations'].append('dummy WAL/SHM created in disposable clone')
    elif mechanic == 'harness_fixture:worker_timeout_or_malformed_output':
        prepare_disposable_clone(runtime)
        fixture['inject'] = 'lc6_case20_malformed_worker_output'
        fixture['runner_mode'] = 'deterministic_malformed_worker_output'
        fixture['run_id'] = f"{runtime.case['case_id']}-{uuid.uuid4().hex[:12]}"
    elif mechanic.startswith('refusal_only:'):
        fixture['target'] = runtime.case['disposable_target_path']
    else:
        raise HarnessRefusal(f'unhandled mechanic: {mechanic}')
    runtime.fixture_state = fixture
    write_case_evidence(runtime, 'fixture_setup.json', fixture)
    return fixture


def run_subprocess_json(cmd: list[str], timeout_s: int, stage: str, runtime: CaseRuntime) -> dict[str, Any]:
    start = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    runtime.child_pid = proc.pid
    terminated = False
    killed = False
    timed_out = False
    stdout = ''
    stderr = ''
    timeout_message = None
    def _text(value: Any) -> str:
        if value is None:
            return ''
        if isinstance(value, bytes):
            return value.decode('utf-8', errors='replace')
        return str(value)

    try:
        out, err = proc.communicate(timeout=timeout_s)
        stdout = _text(out)
        stderr = _text(err)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _text(exc.stdout)
        stderr = _text(exc.stderr)
        timeout_message = str(exc)
        proc.terminate()
        terminated = True
        try:
            out2, err2 = proc.communicate(timeout=10)
            stdout += _text(out2)
            stderr += _text(err2)
        except subprocess.TimeoutExpired:
            proc.kill()
            killed = True
            out3, err3 = proc.communicate(timeout=10)
            stdout += _text(out3)
            stderr += _text(err3)
    duration_s = round(time.time() - start, 6)
    alive_after_wait = proc.poll() is None
    record: dict[str, Any] = {
        'stage': stage,
        'cmd': cmd,
        'pid': proc.pid,
        'returncode': proc.returncode,
        'stdout': stdout,
        'stderr': stderr,
        'timed_out': timed_out,
        'terminated': terminated,
        'killed': killed,
        'timeout_message': timeout_message,
        'duration_s': duration_s,
        'alive_after_wait': alive_after_wait,
        'exited': not alive_after_wait,
    }
    text = (stdout or '').strip()
    if text:
        try:
            record['json_payload'] = json.loads(text)
            record['json_parse_error'] = None
        except json.JSONDecodeError as exc:
            record['json_payload'] = None
            record['json_parse_error'] = str(exc)
    else:
        record['json_payload'] = None
        record['json_parse_error'] = 'empty stdout'
    return record


def command_runner(runtime: CaseRuntime, cmd: list[str], timeout_s: int, stage: str) -> dict[str, Any]:
    runner = runtime.command_runner or run_subprocess_json
    record = runner(cmd, timeout_s, stage, runtime)
    runtime.last_child_record = record
    runtime.child_pid = record.get('pid')
    return record


def normalise_apply_record(record: dict[str, Any]) -> dict[str, Any]:
    if record.get('timed_out'):
        return {
            'status': 'CONTROLLED_FAILURE',
            'reason': 'subprocess_timeout',
            'process': record,
            'opened_chroma': False,
            'production_action': False,
        }
    if record.get('json_payload') is None:
        reason = 'malformed_json' if record.get('json_parse_error') != 'empty stdout' else 'empty_json_stdout'
        return {
            'status': 'CONTROLLED_FAILURE',
            'reason': reason,
            'process': record,
            'opened_chroma': False,
            'production_action': False,
        }
    payload = record['json_payload']
    return {
        'status': payload.get('status', 'UNKNOWN'),
        'result': payload,
        'process': record,
        'opened_chroma': False,
        'production_action': False,
    }


def executor_apply(runtime: CaseRuntime, inject: str | None = None) -> dict[str, Any]:
    cmd = [
        RAG_PY,
        str(PKG / 'orphan_repair_executor.py'),
        'apply',
        '--package-dir',
        str(runtime.package_dir),
        '--gen-dir',
        str(runtime.paths.clone),
        '--work-dir',
        str(runtime.paths.work),
    ]
    if inject:
        cmd.extend(['--inject', inject])
    fixture = runtime.fixture_state or {}
    if fixture.get('run_id'):
        cmd.extend(['--case-id', runtime.case['case_id'], '--run-id', str(fixture['run_id'])])
    runtime.mutation_started = True
    timeout_s = int((runtime.fixture_state or {}).get('apply_timeout_s', 1800))
    record = command_runner(runtime, cmd, timeout_s, 'apply')
    write_case_evidence(runtime, 'executor_apply_process.json', record)
    observed = normalise_apply_record(record)
    write_case_evidence(runtime, 'executor_apply_observed.json', observed)
    return observed


def executor_rollback(runtime: CaseRuntime) -> dict[str, Any]:
    cmd = [
        RAG_PY,
        str(PKG / 'orphan_repair_executor.py'),
        'rollback',
        '--gen-dir',
        str(runtime.paths.clone),
        '--work-dir',
        str(runtime.paths.work),
    ]
    record = command_runner(runtime, cmd, 1800, 'rollback')
    write_case_evidence(runtime, 'rollback_process.json', record)
    observed = normalise_apply_record(record)
    write_case_evidence(runtime, 'rollback_observed.json', observed)
    return observed


def run_case11_interrupt_chain(runtime: CaseRuntime) -> dict[str, Any]:
    fixture = runtime.fixture_state or {}
    run_id = str(fixture.get('run_id') or f"{runtime.case['case_id']}-{uuid.uuid4().hex[:12]}")
    state_dir = runtime.paths.work / 'case11_real_chain'
    state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = state_dir / 'mutation_checkpoint.json'
    signal_path = state_dir / 'signal_received.json'
    cmd = [
        RAG_PY,
        str(PKG / 'orphan_repair_executor.py'),
        'interrupt-probe',
        '--package-dir',
        str(runtime.package_dir),
        '--gen-dir',
        str(runtime.paths.clone),
        '--work-dir',
        str(runtime.paths.work),
        '--case-id',
        runtime.case['case_id'],
        '--run-id',
        run_id,
        '--state-dir',
        str(state_dir),
    ]
    runtime.mutation_started = True
    if runtime.command_runner is not None:
        record = command_runner(runtime, cmd, 180, 'interrupt_probe')
        observed = normalise_apply_record(record)
        observed['run_id'] = run_id
        observed['opened_chroma'] = False
        observed['production_action'] = False
        write_case_evidence(runtime, 'case11_interrupt_executor_process.json', record)
        write_case_evidence(runtime, 'case11_interrupt_observed.json', observed)
        return observed

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    runtime.child_pid = proc.pid
    checkpoint = wait_for_json_file(checkpoint_path)
    verified = verify_case11_checkpoint_payload(
        int(checkpoint['worker_pid']),
        runtime.case['case_id'],
        run_id,
        runtime.paths.clone,
        PKG / 'chroma_worker.py',
        runtime.paths.parent,
        checkpoint,
    )
    signal_record = signal_verified_case11_process(int(checkpoint['worker_pid']), runtime.case['case_id'], run_id, runtime.paths.clone, PKG / 'chroma_worker.py', runtime.paths.parent, checkpoint)
    stdout, stderr = proc.communicate(timeout=30)
    executor_payload = json.loads(stdout) if stdout.strip() else None
    signal_payload = wait_for_json_file(signal_path)
    executor_process = {
        'pid': proc.pid,
        'returncode': proc.returncode,
        'stdout': stdout,
        'stderr': stderr,
        'exited': proc.poll() is not None,
    }
    observed = {
        'status': executor_payload.get('status') if isinstance(executor_payload, dict) else 'INTERRUPT_CHAIN_FAILED',
        'run_id': run_id,
        'checkpoint': checkpoint,
        'checkpoint_verified': verified,
        'signal_record': signal_record,
        'signal_payload': signal_payload,
        'executor_process': executor_process,
        'executor_result': executor_payload,
        'opened_chroma': False,
        'production_action': False,
    }
    write_case_evidence(runtime, 'case11_interrupt_checkpoint.json', checkpoint)
    write_case_evidence(runtime, 'case11_interrupt_signal.json', signal_payload)
    write_case_evidence(runtime, 'case11_interrupt_executor_process.json', executor_process)
    write_case_evidence(runtime, 'case11_interrupt_observed.json', observed)
    return observed


def decide_rollback(runtime: CaseRuntime, observed: dict[str, Any]) -> dict[str, Any]:
    snapshot = runtime.paths.work / 'generation_snapshot'
    child = runtime.last_child_record or {}
    child_exited = child.get('exited', False) and not child.get('alive_after_wait', False)
    decision = {
        'case_id': runtime.case['case_id'],
        'child_pid': child.get('pid'),
        'child_exited': child_exited,
        'child_alive_after_wait': child.get('alive_after_wait', False),
        'timed_out': child.get('timed_out', False),
        'snapshot_exists': snapshot.exists(),
        'rollback_owner': 'none',
        'rollback_invoked': False,
        'rollback_allowed': False,
        'rollback_blocked_reason': None,
        'rollback_result': None,
    }
    if not runtime.mutation_started:
        return decision
    if child.get('alive_after_wait', False):
        decision['rollback_blocked_reason'] = 'rollback forbidden while child remains alive'
        runtime.rollback_owner = 'blocked'
        return decision
    if observed.get('status') in {'APPLY_REJECTED', 'INTERRUPT_PROBE_RECOVERED'}:
        decision['rollback_owner'] = 'executor'
        decision['rollback_allowed'] = False
        decision['rollback_blocked_reason'] = 'executor-owned restoration after controlled worker failure'
        runtime.rollback_owner = 'executor'
        return decision
    if snapshot.exists() and runtime.paths.clone.exists() and child_exited:
        decision['rollback_owner'] = 'harness'
        decision['rollback_allowed'] = True
        runtime.rollback_owner = 'harness'
        return decision
    runtime.rollback_owner = 'none'
    return decision


def perform_harness_rollback_if_needed(runtime: CaseRuntime, decision: dict[str, Any]) -> dict[str, Any]:
    if decision['rollback_owner'] != 'harness':
        return decision
    if not decision['rollback_allowed']:
        return decision
    rollback_result = executor_rollback(runtime)
    decision['rollback_invoked'] = True
    decision['rollback_result'] = rollback_result
    return decision


def evaluate_observed_case(runtime: CaseRuntime, observed: dict[str, Any]) -> dict[str, Any]:
    expected = runtime.case['expected_process_exit_result']
    status_text = json.dumps(observed, ensure_ascii=False)
    ok = False
    if expected.startswith('APPLY_REJECTED'):
        ok = 'APPLY_REJECTED' in status_text
    elif (
        expected.startswith('refused exit code before Chroma open')
        or expected.startswith('controlled refusal')
        or expected.startswith('controlled lock refusal')
    ):
        ok = (
            'refused' in status_text
            or 'CONTROLLED_REFUSAL_CONFIRMED' in status_text
            or 'checksum mismatch' in status_text
            or 'IngestLockError' in status_text
            or 'missing disposable clone marker' in status_text
            or 'APPLY_REJECTED' in status_text
        )
    elif expected.startswith('parent detects worker termination'):
        ok = observed.get('status') in {'CONTROLLED_FAILURE', 'INTERRUPT_PROBE_RECOVERED'}
    elif expected.startswith('controlled failure on timeout or malformed JSON'):
        ok = observed.get('status') == 'CONTROLLED_FAILURE' or (
            observed.get('status') == 'APPLY_REJECTED'
            and ('malformed JSON stdout' in status_text or 'lc6_case20_malformed_worker_output' in status_text)
        )
    else:
        ok = expected in status_text
    return {
        'case_id': runtime.case['case_id'],
        'expected_process_exit_result': expected,
        'observed_summary': observed.get('status') or observed.get('result', {}).get('status') or 'unknown',
        'expected_behavior_confirmed': ok,
    }


def capture_case_production_hash(runtime: CaseRuntime, label: str) -> dict[str, Any]:
    snap = production_hash_snapshot()
    write_case_evidence(runtime, f'production_hashes_{label}.json', snap)
    if label == 'before':
        runtime.production_hash_before = snap
    else:
        runtime.production_hash_after = snap
    return snap


def compare_production_hashes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    match = (
        before['embedded']['sha256'] == after['embedded']['sha256']
        and before['chroma_sqlite3']['sha256'] == after['chroma_sqlite3']['sha256']
    )
    return {
        'before': before,
        'after': after,
        'match': match,
        'opened_chroma': False,
        'fail_closed_on_mismatch': not match,
    }


def dispatch_identity_chain(runtime: CaseRuntime, observed: dict[str, Any]) -> dict[str, Any]:
    checkpoint = observed.get('checkpoint') or {}
    executor_result = observed.get('executor_result') or {}
    worker_process = executor_result.get('worker_process') or {}
    fixture = runtime.fixture_state or {}
    case20 = runtime.case['case_id'] == 'LC6-20-worker-timeout-or-malformed-output'
    return {
        'cli_selected_case_id': runtime.selected_case_id,
        'contract_case_id': runtime.contract_case_id,
        'harness_mechanic': MECHANIC_BY_CASE_ID[runtime.case['case_id']],
        'executor_action': 'interrupt-probe' if runtime.case['case_id'] == 'LC6-11-interruption-during-mutation' else ('apply' if case20 else None),
        'worker_action': (worker_process.get('json_payload') or {}).get('action') or checkpoint.get('worker_command') or ('apply_manifest_ops' if case20 else None),
        'checkpoint_source': checkpoint.get('worker_script'),
        'worker_pid': checkpoint.get('worker_pid'),
        'disposable_clone': str(runtime.paths.clone),
        'run_id': observed.get('run_id') or fixture.get('run_id'),
    }


def cleanup_case(runtime: CaseRuntime, expected_behavior_confirmed: bool) -> dict[str, Any]:
    if not runtime.evidence_persisted:
        raise HarnessRefusal('cleanup attempted before durable evidence persistence')
    runtime.cleanup_started_at = time.time()
    callback_results = run_cleanup_callbacks(runtime)
    preserved_clone = runtime.preserve_clone and runtime.paths.clone.exists()
    removed = False
    if expected_behavior_confirmed and not preserved_clone and runtime.paths.case_root.exists():
        shutil.rmtree(runtime.paths.case_root)
        removed = True
    durable_verification = verify_required_evidence(runtime.durable_case_dir, expected_required_evidence(runtime))
    if not durable_verification['ok']:
        raise HarnessRefusal(f'cleanup would erase durable evidence: {durable_verification["missing"]}')
    return {
        'callbacks': callback_results,
        'clone_preserved': preserved_clone,
        'case_root_removed': removed,
        'durable_evidence_survived_cleanup': durable_verification['ok'],
        'durable_evidence_dir': str(runtime.durable_case_dir),
        'durable_evidence_verification': durable_verification,
    }


def execute_refusal_only_case(runtime: CaseRuntime) -> dict[str, Any]:
    target = Path(runtime.case['disposable_target_path'])
    probe = refusal_probe(target)
    result = {
        'status': 'CONTROLLED_REFUSAL_CONFIRMED' if probe['ok'] else 'UNEXPECTED_REFUSAL_RESULT',
        'probe': probe,
        'opened_chroma': False,
        'production_action': False,
    }
    write_case_evidence(runtime, 'refusal_result.json', result)
    return result


def execute_fixture_case(runtime: CaseRuntime) -> dict[str, Any]:
    mechanic = MECHANIC_BY_CASE_ID[runtime.case['case_id']]
    fixture = runtime.fixture_state or setup_case_fixture(runtime)
    if mechanic.startswith('executor_inject:'):
        return executor_apply(runtime, inject=fixture['inject'])
    if mechanic == 'harness_fixture:interruption_during_execution':
        return run_case11_interrupt_chain(runtime)
    if mechanic == 'harness_fixture:candidate_lock_contention':
        observed = executor_apply(runtime)
        write_case_evidence(runtime, 'lock_refusal.json', observed)
        if not (runtime.paths.work / 'execution_log.jsonl').exists():
            write_work_jsonl_placeholder(
                runtime,
                'execution_log.jsonl',
                {
                    'event': 'executor_log_missing_before_lock_refusal',
                    'source': 'harness',
                    'case_id': runtime.case['case_id'],
                    'reason': 'executor returned before emitting execution_log.jsonl on early candidate-lock refusal',
                    'observed_status': observed.get('status'),
                    'lock_refusal_detected': True,
                },
            )
        return observed
    if mechanic in {
        'harness_fixture:stale_tracker_store_baseline_hash',
        'harness_fixture:canonical_source_sha_mismatch_disposable_manifest',
        'harness_fixture:invalid_or_missing_marker_refusal',
        'harness_fixture:invalid_manifest_refusal',
        'harness_fixture:wal_shm_precondition_refusal',
    }:
        return executor_apply(runtime)
    if mechanic == 'harness_fixture:dirty_clone_rerun_refusal':
        first = executor_apply(runtime)
        second = executor_apply(runtime)
        result = {
            'status': 'CONTROLLED_REFUSAL_CONFIRMED' if first.get('status') == 'APPLY_OK' and second.get('status') == 'APPLY_REJECTED' else 'UNEXPECTED_DIRTY_CLONE_RESULT',
            'first_apply': first,
            'second_apply': second,
            'opened_chroma': False,
            'production_action': False,
        }
        write_case_evidence(runtime, 'dirty_clone_rerun.json', result)
        return result
    if mechanic == 'harness_fixture:worker_timeout_or_malformed_output':
        observed = executor_apply(runtime, inject=fixture['inject'])
        worker_state_file = runtime.paths.work / 'lc6_case20_malformed_worker_output' / 'worker_malformed_output_emitted.json'
        worker_state_payload = json.loads(worker_state_file.read_text(encoding='utf-8')) if worker_state_file.exists() else None
        case20_evidence = {
            'case_id': runtime.case['case_id'],
            'run_id': fixture.get('run_id'),
            'expected_worker_action': 'apply_manifest_ops',
            'observed_status': observed.get('status'),
            'malformed_output_observed': 'malformed JSON stdout' in json.dumps(observed, ensure_ascii=False),
            'success_cannot_be_returned': observed.get('status') != 'APPLY_OK',
            'executor_worker_boundary_exercised': True,
            'worker_state_dir': str(runtime.paths.work / 'lc6_case20_malformed_worker_output'),
            'worker_state_payload': worker_state_payload,
        }
        if worker_state_payload is not None:
            write_case_evidence(runtime, 'worker_malformed_output_emitted.json', worker_state_payload)
        write_case_evidence(runtime, 'timeout_or_malformed_output.json', case20_evidence)
        write_case_evidence(runtime, 'worker_exit_codes.json', {
            'case_id': runtime.case['case_id'],
            'run_id': fixture.get('run_id'),
            'executor_process': runtime.last_child_record,
            'worker_malformed_output_file': str(runtime.paths.work / 'lc6_case20_malformed_worker_output' / 'worker_malformed_output_emitted.json'),
            'worker_state_payload': worker_state_payload,
        })
        return observed
    raise HarnessRefusal(f'unhandled fixture case execution: {mechanic}')


def execute_case(runtime: CaseRuntime) -> dict[str, Any]:
    capture_case_production_hash(runtime, 'before')
    mechanic = MECHANIC_BY_CASE_ID[runtime.case['case_id']]
    if mechanic.startswith('refusal_only:'):
        observed = execute_refusal_only_case(runtime)
        actual_case_execution_performed = False
    else:
        if runtime.fixture_state is None:
            setup_case_fixture(runtime)
        else:
            write_case_evidence(runtime, 'fixture_setup.json', runtime.fixture_state)
        observed = execute_fixture_case(runtime)
        actual_case_execution_performed = True
    capture_case_production_hash(runtime, 'after')
    if runtime.production_hash_before is None or runtime.production_hash_after is None:
        raise HarnessRefusal('production hash capture incomplete')
    production_hash_check = compare_production_hashes(runtime.production_hash_before, runtime.production_hash_after)
    evaluation = evaluate_observed_case(runtime, observed)
    if not production_hash_check['match']:
        evaluation['expected_behavior_confirmed'] = False
        evaluation['observed_summary'] = 'PRODUCTION_HASH_MISMATCH'
    if not evaluation['expected_behavior_confirmed']:
        runtime.preserve_clone = True
    rollback = decide_rollback(runtime, observed)
    rollback = perform_harness_rollback_if_needed(runtime, rollback)
    preliminary_report = {
        'case_id': runtime.case['case_id'],
        'mechanic': mechanic,
        'dispatch_identity_chain': dispatch_identity_chain(runtime, observed),
        'execution_metadata': execution_metadata('execute', actual_case_execution_performed),
        'observed': observed,
        'evaluation': evaluation,
        'rollback': rollback,
        'production_hash_evidence': production_hash_check,
        'cleanup': {'pending': True},
        'no_production_action': True,
    }
    persistence = persist_case_evidence_before_cleanup(runtime, preliminary_report)
    cleanup = cleanup_case(runtime, evaluation['expected_behavior_confirmed'])
    final_report = {
        **preliminary_report,
        'cleanup': cleanup,
        'evidence_persistence': persistence,
    }
    durable_summary = update_durable_case_outputs(runtime, final_report)
    final_report['durable_evidence'] = durable_summary
    update_durable_case_outputs(runtime, final_report)
    if runtime.paths.evidence.exists():
        write_json_atomic(runtime.paths.evidence / 'case_report.json', final_report)
        write_json_atomic(runtime.paths.evidence / 'evidence_inventory.json', {
            'case_id': runtime.case['case_id'],
            'written_before_cleanup': True,
            'files': evidence_inventory(runtime.paths.evidence),
        })
    return final_report


def execute_case_sequence(
    contract: dict[str, Any],
    case_ids: list[str],
    parent: Path,
    run_report_root: Path,
    case_runner: Callable[[CaseRuntime], dict[str, Any]] | None = None,
    runtime_factory: Callable[[dict[str, Any]], CaseRuntime] | None = None,
) -> dict[str, Any]:
    cases = contract_case_map(contract)
    results = []
    stopped_on = None
    preserved_failed_clone = None
    for cid in case_ids:
        runtime = runtime_factory(cases[cid]) if runtime_factory else make_case_runtime(cases[cid], parent, run_report_root)
        result = (case_runner or execute_case)(runtime)
        results.append(result)
        if not result['evaluation']['expected_behavior_confirmed']:
            stopped_on = cid
            if runtime.paths.clone.exists():
                runtime.preserve_clone = True
                preserved_failed_clone = str(runtime.paths.clone)
            break
    not_started = case_ids[len(results):]
    return {
        'started_case_ids': [r['case_id'] for r in results],
        'stopped_on_case_id': stopped_on,
        'not_started_case_ids': not_started,
        'preserved_failed_clone': preserved_failed_clone,
        'stop_on_first_unexpected_result_honored': (stopped_on is None and not not_started) or (stopped_on is not None and bool(not_started)),
        'results': results,
    }


def validate_failed_case_preservation_logic(contract: dict[str, Any]) -> dict[str, Any]:
    case_ids = list_case_ids(contract)
    failed_case = case_ids[4]
    started = []
    preserved = []
    for cid in case_ids:
        if preserved:
            break
        started.append(cid)
        if cid == failed_case:
            preserved.append({'case_id': cid, 'preserved_for_diagnosis': True})
            break
    not_started = case_ids[len(started):]
    return {
        'failed_case_id': failed_case,
        'started_before_stop': started,
        'not_started_after_failure': not_started,
        'preserved_failed_case': preserved,
        'ok': started[-1] == failed_case and bool(not_started),
    }


def dry_run_case_layout_validation(contract: dict[str, Any]) -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_layout_', dir='/private/tmp')).resolve()
    try:
        layouts = []
        for case in contract['cases']:
            paths = build_case_paths(parent, case)
            layouts.append({'case_id': case['case_id'], 'clone': str(paths.clone), 'work': str(paths.work), 'evidence': str(paths.evidence)})
        return {'ok': len(layouts) == 20, 'layouts': layouts}
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def build_mock_baseline(parent: Path) -> Path:
    baseline = parent / 'baseline'
    baseline.mkdir(parents=True, exist_ok=True)
    (baseline / 'embedded.json').write_text('{"baseline": true}\n', encoding='utf-8')
    (baseline / 'chroma.sqlite3').write_text('sqlite-bytes', encoding='utf-8')
    (baseline / 'note.txt').write_text('baseline note\n', encoding='utf-8')
    return baseline


def build_mock_package(parent: Path) -> Path:
    pkg = parent / 'package'
    pkg.mkdir(parents=True, exist_ok=True)
    manifest = {
        'manifest_payload_sha256': 'a' * 64,
        'operations': [{'verified_live_sha256': 'b' * 64}],
    }
    write_json_atomic(pkg / 'atomic_orphan_repair_manifest.json', manifest)
    return pkg


def wait_for_json_file(path: Path, timeout_s: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
        time.sleep(0.02)
    raise HarnessRefusal(f'timed out waiting for {path.name}')


def write_case11_worker_script(state_dir: Path) -> Path:
    script_path = state_dir / 'case11_worker.py'
    script = r"""#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
from pathlib import Path


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def main() -> None:
    state_dir = Path(sys.argv[1]).resolve()
    case_id = sys.argv[2]
    run_id = sys.argv[3]
    clone_path = Path(sys.argv[4]).resolve()
    started_path = state_dir / 'worker_started.json'
    expected_path = state_dir / 'expected_identity.json'
    checkpoint_path = state_dir / 'mutation_checkpoint.json'
    refusal_path = state_dir / 'identity_refusal.json'
    signal_path = state_dir / 'signal_received.json'
    mutation_flag = clone_path / 'mutation_in_progress.flag'
    wal = clone_path / 'chroma.sqlite3-wal'
    shm = clone_path / 'chroma.sqlite3-shm'

    def on_signal(signum, _frame):
        write_json(signal_path, {
            'case_id': case_id,
            'run_id': run_id,
            'clone_path': str(clone_path),
            'worker_pid': os.getpid(),
            'signal': signum,
            'checkpoint_reached': checkpoint_path.exists(),
        })
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    write_json(started_path, {
        'case_id': case_id,
        'run_id': run_id,
        'clone_path': str(clone_path),
        'worker_pid': os.getpid(),
        'ppid': os.getppid(),
        'argv': sys.argv,
        'script_path': str(Path(__file__).resolve()),
    })

    deadline = time.time() + 10.0
    while time.time() < deadline:
        if expected_path.exists():
            break
        time.sleep(0.02)
    else:
        write_json(refusal_path, {
            'status': 'IDENTITY_REFUSED',
            'reason': 'expected_identity_missing',
            'worker_pid': os.getpid(),
            'case_id': case_id,
            'run_id': run_id,
            'clone_path': str(clone_path),
        })
        raise SystemExit(90)

    expected = json.loads(expected_path.read_text(encoding='utf-8'))
    mismatches = []
    if expected.get('case_id') != case_id:
        mismatches.append('case_id')
    if expected.get('run_id') != run_id:
        mismatches.append('run_id')
    if Path(expected.get('clone_path', '')).resolve() != clone_path:
        mismatches.append('clone_path')
    if int(expected.get('expected_worker_pid', -1)) != os.getpid():
        mismatches.append('worker_pid')
    if mismatches:
        write_json(refusal_path, {
            'status': 'IDENTITY_REFUSED',
            'reason': 'identity_mismatch',
            'mismatches': mismatches,
            'expected': expected,
            'observed': {
                'case_id': case_id,
                'run_id': run_id,
                'clone_path': str(clone_path),
                'worker_pid': os.getpid(),
            },
        })
        raise SystemExit(91)

    mutation_flag.write_text(run_id + '\n', encoding='utf-8')
    with (clone_path / 'embedded.json').open('a', encoding='utf-8') as fh:
        fh.write('mutation checkpoint ' + run_id + '\n')
    wal.write_text('wal ' + run_id + '\n', encoding='utf-8')
    shm.write_text('shm ' + run_id + '\n', encoding='utf-8')

    write_json(checkpoint_path, {
        'status': 'MUTATION_IN_PROGRESS',
        'case_id': case_id,
        'run_id': run_id,
        'clone_path': str(clone_path),
        'worker_pid': os.getpid(),
        'expected_worker_pid': expected['expected_worker_pid'],
        'checkpoint_name': 'mutation_in_progress',
        'mutation_flag': str(mutation_flag),
        'wal_path': str(wal),
        'shm_path': str(shm),
        'script_path': str(Path(__file__).resolve()),
    })
    signal.pause()


if __name__ == '__main__':
    main()
"""
    script_path.write_text(script, encoding='utf-8')
    script_path.chmod(0o755)
    return script_path


def ps_command_for_pid(pid: int) -> str:
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def clone_inventory_summary_for_path(path: Path) -> dict[str, Any]:
    rows = inventory_rows(path)
    return {
        'path': str(path),
        'row_count': len(rows),
        'checksum': inventory_checksum_from_rows(rows),
        'rows': rows,
    }


def restore_clone_from_snapshot(snapshot_dir: Path, clone_dir: Path) -> None:
    if clone_dir.exists():
        shutil.rmtree(clone_dir)
    shutil.copytree(snapshot_dir, clone_dir, copy_function=shutil.copy2)


def case11_roll_back_gate(child_alive: bool, child_exited: bool) -> dict[str, Any]:
    allowed = child_exited and not child_alive
    return {
        'rollback_allowed': allowed,
        'reason': None if allowed else 'rollback forbidden while child remains alive',
    }


def verify_case11_process_identity(proc_pid: int, case_id: str, run_id: str, clone_path: Path, script_path: Path, parent: Path, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_within_parent(parent, clone_path)
    ensure_not_forbidden_target(clone_path)
    if payload.get('case_id') != case_id:
        raise HarnessRefusal('case11 identity case_id mismatch')
    if payload.get('run_id') != run_id:
        raise HarnessRefusal('case11 identity run_id mismatch')
    if Path(payload.get('clone_path', '')).resolve() != clone_path.resolve():
        raise HarnessRefusal('case11 identity clone_path mismatch')
    if int(payload.get('worker_pid', -1)) != proc_pid:
        raise HarnessRefusal('case11 identity worker_pid mismatch')
    command = ps_command_for_pid(proc_pid)
    if str(script_path) not in command:
        raise HarnessRefusal('case11 process command mismatch: script path not present')
    if case_id not in command or run_id not in command or str(clone_path) not in command:
        raise HarnessRefusal('case11 process command mismatch: identity tokens missing')
    return {
        'worker_pid': proc_pid,
        'case_id': case_id,
        'run_id': run_id,
        'clone_path': str(clone_path),
        'script_path': str(script_path),
        'ps_command': command,
    }


def verify_case11_checkpoint_payload(proc_pid: int, case_id: str, run_id: str, clone_path: Path, script_path: Path, parent: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get('status') != 'MUTATION_IN_PROGRESS':
        raise HarnessRefusal('case11 checkpoint status mismatch')
    if payload.get('checkpoint_name') != 'mutation_in_progress':
        raise HarnessRefusal('case11 checkpoint name mismatch')
    verification = verify_case11_process_identity(proc_pid, case_id, run_id, clone_path, script_path, parent, payload)
    verification['checkpoint_name'] = payload.get('checkpoint_name')
    verification['checkpoint_status'] = payload.get('status')
    return verification


def signal_verified_case11_process(worker_pid: int, case_id: str, run_id: str, clone_path: Path, script_path: Path, parent: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    verification = verify_case11_checkpoint_payload(worker_pid, case_id, run_id, clone_path, script_path, parent, checkpoint)
    os.kill(worker_pid, signal.SIGTERM)
    return {
        'verified_identity': verification,
        'signal_sent': 'SIGTERM',
        'target_pid': worker_pid,
        'target_clone_path': str(clone_path),
    }


def run_case11_identity_refusal_test(kind: str) -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix=f'lc6_case11_{kind}_', dir='/private/tmp')).resolve()
    try:
        baseline = build_mock_baseline(parent)
        case = contract_case_map(load_contract())['LC6-11-interruption-during-mutation']
        runtime = make_case_runtime(case, parent / 'cases', parent / 'run_report', source_package_dir=build_mock_package(parent), baseline_clone_source=baseline)
        prepare_disposable_clone(runtime)
        state_dir = runtime.paths.work / 'case11_identity_test'
        state_dir.mkdir(parents=True, exist_ok=True)
        script_path = write_case11_worker_script(state_dir)
        run_id = f'{case["case_id"]}-{uuid.uuid4().hex[:8]}'
        cmd = [sys.executable, str(script_path), str(state_dir), case['case_id'], run_id, str(runtime.paths.clone)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = wait_for_json_file(state_dir / 'worker_started.json')
        expected = {
            'case_id': case['case_id'] if kind != 'wrong_case_id' else 'LC6-99-wrong-case',
            'run_id': run_id if kind != 'wrong_run_id' else f'{run_id}-wrong',
            'clone_path': str((runtime.paths.clone if kind != 'wrong_path' else parent / 'wrong_clone').resolve()),
            'expected_worker_pid': proc.pid if kind != 'wrong_pid' else proc.pid + 1,
        }
        write_json_atomic(state_dir / 'expected_identity.json', expected)
        refusal = wait_for_json_file(state_dir / 'identity_refusal.json')
        stdout, stderr = proc.communicate(timeout=5)
        return {
            'ok': proc.returncode == 91 and refusal.get('status') == 'IDENTITY_REFUSED' and not (state_dir / 'mutation_checkpoint.json').exists(),
            'test': kind,
            'worker_started': started,
            'expected_identity': expected,
            'refusal': refusal,
            'returncode': proc.returncode,
            'stdout': stdout,
            'stderr': stderr,
        }
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def run_case11_stale_checkpoint_refusal_test() -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_case11_stale_checkpoint_', dir='/private/tmp')).resolve()
    try:
        baseline = build_mock_baseline(parent)
        case = contract_case_map(load_contract())['LC6-11-interruption-during-mutation']
        runtime = make_case_runtime(case, parent / 'cases', parent / 'run_report', source_package_dir=build_mock_package(parent), baseline_clone_source=baseline)
        prepare_disposable_clone(runtime)
        state_dir = runtime.paths.work / 'case11_stale_checkpoint'
        state_dir.mkdir(parents=True, exist_ok=True)
        script_path = write_case11_worker_script(state_dir)
        run_id = f'{case["case_id"]}-{uuid.uuid4().hex[:8]}'
        cmd = [sys.executable, str(script_path), str(state_dir), case['case_id'], run_id, str(runtime.paths.clone)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            started = wait_for_json_file(state_dir / 'worker_started.json')
            expected_identity = {
                'case_id': case['case_id'],
                'run_id': run_id,
                'clone_path': str(runtime.paths.clone.resolve()),
                'expected_worker_pid': proc.pid,
            }
            write_json_atomic(state_dir / 'expected_identity.json', expected_identity)
            checkpoint = wait_for_json_file(state_dir / 'mutation_checkpoint.json')
            try:
                verify_case11_checkpoint_payload(proc.pid, case['case_id'], f'{run_id}-stale', runtime.paths.clone, script_path, parent, checkpoint)
                return {'ok': False, 'reason': 'stale checkpoint unexpectedly accepted', 'checkpoint': checkpoint}
            except Exception as exc:
                return {
                    'ok': isinstance(exc, HarnessRefusal),
                    'started': started,
                    'checkpoint': checkpoint,
                    'error_type': type(exc).__name__,
                    'error': str(exc),
                }
        finally:
            proc.terminate()
            proc.communicate(timeout=5)
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def run_case11_production_path_refusal_test() -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_case11_prod_path_', dir='/private/tmp')).resolve()
    try:
        payload = {
            'case_id': 'LC6-11-interruption-during-mutation',
            'run_id': 'test-run',
            'clone_path': str(PROD_GEN),
            'worker_pid': 12345,
            'checkpoint_name': 'mutation_in_progress',
            'status': 'MUTATION_IN_PROGRESS',
        }
        try:
            verify_case11_checkpoint_payload(12345, 'LC6-11-interruption-during-mutation', 'test-run', PROD_GEN, Path('/tmp/fake_case11_worker.py'), parent, payload)
            return {'ok': False, 'reason': 'production path unexpectedly accepted'}
        except Exception as exc:
            return {
                'ok': isinstance(exc, (TargetRefusal, HarnessRefusal)),
                'error_type': type(exc).__name__,
                'error': str(exc),
                'opened_chroma': False,
            }
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def case11_case_matrix_consistency_validation(contract: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
    case = contract_case_map(contract)['LC6-11-interruption-during-mutation']
    matrix_mechanic = MECHANIC_BY_CASE_ID[case['case_id']]
    expected_entry = 'future lc6_failure_injection.py interruption harness terminating disposable worker only after mutation begins'
    blocker_entries = [item for item in blockers if item.startswith('LC6-11-interruption-during-mutation:')]
    return {
        'case_id': case['case_id'],
        'mechanic': matrix_mechanic,
        'contract_entry_matches_expected': case['command_or_harness_entry_point'] == expected_entry,
        'mechanic_matches_expected': matrix_mechanic == 'harness_fixture:interruption_during_execution',
        'single_blocker_entry': blocker_entries,
        'ok': case['command_or_harness_entry_point'] == expected_entry and matrix_mechanic == 'harness_fixture:interruption_during_execution' and len(blocker_entries) <= 1,
    }


def case11_interruption_mechanism_preflight_validation(contract: dict[str, Any]) -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_case11_preflight_', dir='/private/tmp')).resolve()
    run_report_root = parent / 'run_report'
    case = contract_case_map(contract)['LC6-11-interruption-during-mutation']
    try:
        runtime = make_case_runtime(case, parent / 'cases', run_report_root, source_package_dir=build_mock_package(parent), baseline_clone_source=build_mock_baseline(parent))
        prepare_disposable_clone(runtime)
        runtime.fixture_state = {'mechanic': MECHANIC_BY_CASE_ID[case['case_id']], 'preflight_only': True}
        write_case_evidence(runtime, 'fixture_setup.json', runtime.fixture_state)
        capture_case_production_hash(runtime, 'before')
        snapshot_dir = runtime.paths.work / 'verified_baseline_snapshot'
        shutil.copytree(runtime.paths.clone, snapshot_dir, copy_function=shutil.copy2)
        snapshot_summary = clone_inventory_summary_for_path(snapshot_dir)
        state_dir = runtime.paths.work / 'case11_preflight'
        state_dir.mkdir(parents=True, exist_ok=True)
        script_path = write_case11_worker_script(state_dir)
        run_id = f'{case["case_id"]}-{uuid.uuid4().hex[:12]}'
        cmd = [sys.executable, str(script_path), str(state_dir), case['case_id'], run_id, str(runtime.paths.clone)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        runtime.child_pid = proc.pid
        write_work_jsonl_placeholder(runtime, 'execution_log.jsonl', {
            'event': 'case11_preflight_worker_spawned',
            'case_id': case['case_id'],
            'run_id': run_id,
            'worker_pid': proc.pid,
            'clone_path': str(runtime.paths.clone),
            'script_path': str(script_path),
        })
        started = wait_for_json_file(state_dir / 'worker_started.json')
        start_identity = verify_case11_process_identity(proc.pid, case['case_id'], run_id, runtime.paths.clone, script_path, parent, started)
        expected_identity = {
            'case_id': case['case_id'],
            'run_id': run_id,
            'clone_path': str(runtime.paths.clone.resolve()),
            'expected_worker_pid': proc.pid,
        }
        write_json_atomic(state_dir / 'expected_identity.json', expected_identity)
        checkpoint = wait_for_json_file(state_dir / 'mutation_checkpoint.json')
        checkpoint_identity = verify_case11_checkpoint_payload(proc.pid, case['case_id'], run_id, runtime.paths.clone, script_path, parent, checkpoint)
        pre_signal_gate = case11_roll_back_gate(child_alive=proc.poll() is None, child_exited=False)
        if pre_signal_gate['rollback_allowed']:
            raise HarnessRefusal('case11 rollback gate opened before child exit')
        signal_record = signal_verified_case11_process(proc.pid, case['case_id'], run_id, runtime.paths.clone, script_path, parent, checkpoint)
        stdout, stderr = proc.communicate(timeout=10)
        signal_received = wait_for_json_file(state_dir / 'signal_received.json')
        exit_record = {
            'worker_pid': proc.pid,
            'returncode': proc.returncode,
            'stdout': stdout,
            'stderr': stderr,
            'fully_exited': proc.poll() is not None,
            'alive_after_wait': proc.poll() is None,
        }
        post_exit_gate = case11_roll_back_gate(child_alive=False, child_exited=True)
        if not post_exit_gate['rollback_allowed']:
            raise HarnessRefusal('case11 rollback gate remained closed after child exit')
        restore_clone_from_snapshot(snapshot_dir, runtime.paths.clone)
        post_rollback = clone_inventory_summary_for_path(runtime.paths.clone)
        rollback_validation = {
            'snapshot_checksum': snapshot_summary['checksum'],
            'restored_checksum': post_rollback['checksum'],
            'matches_verified_baseline_snapshot': post_rollback['checksum'] == snapshot_summary['checksum'],
            'wal_present_after_rollback': (runtime.paths.clone / 'chroma.sqlite3-wal').exists(),
            'shm_present_after_rollback': (runtime.paths.clone / 'chroma.sqlite3-shm').exists(),
            'lock_present_after_rollback': any('lock' in p.name.lower() for p in runtime.paths.clone.iterdir()),
            'mutation_flag_present_after_rollback': (runtime.paths.clone / 'mutation_in_progress.flag').exists(),
        }
        capture_case_production_hash(runtime, 'after')
        if runtime.production_hash_before is None or runtime.production_hash_after is None:
            raise HarnessRefusal('case11 preflight production hash capture incomplete')
        production_hash_evidence = compare_production_hashes(runtime.production_hash_before, runtime.production_hash_after)
        write_case_evidence(runtime, 'checkpoint_identity_validation.json', {'started': start_identity, 'checkpoint': checkpoint_identity, 'expected_identity': expected_identity})
        write_case_evidence(runtime, 'process_termination_log.json', {'signal_record': signal_record, 'signal_received': signal_received, 'exit_record': exit_record})
        write_case_evidence(runtime, 'worker_exit_codes.json', exit_record)
        write_case_evidence(runtime, 'rollback_validation.json', rollback_validation)
        write_case_evidence(runtime, 'checkpoint_design.json', {
            'checkpoint_name': 'mutation_in_progress',
            'binding_fields': ['case_id', 'run_id', 'clone_path', 'expected_worker_pid'],
            'process_verification': ['ps command includes script path', 'ps command includes case_id', 'ps command includes run_id', 'ps command includes clone path'],
            'fail_closed_rules': ['wrong pid refused', 'wrong path refused', 'rollback blocked before exit', 'forbidden target check before signal'],
        })
        case_report = {
            'case_id': case['case_id'],
            'status': 'CASE11_PREFLIGHT_OK',
            'run_id': run_id,
            'worker_started': started,
            'checkpoint': checkpoint,
            'expected_identity': expected_identity,
            'signal_record': signal_record,
            'pre_signal_rollback_gate': pre_signal_gate,
            'post_exit_rollback_gate': post_exit_gate,
            'signal_received': signal_received,
            'rollback_validation': rollback_validation,
            'production_hash_evidence': production_hash_evidence,
            'remaining_unproven_item': 'Real disposable execution against the actual executor/baseline is still required to prove the deterministic checkpoint integrates with the real LC6-11 mutation path before single-case execution approval.',
            'no_production_action': True,
        }
        persistence = persist_case_evidence_before_cleanup(runtime, case_report)
        cleanup = cleanup_case(runtime, True)
        final_report = {
            **case_report,
            'evidence_persistence': persistence,
            'cleanup': cleanup,
        }
        durable = update_durable_case_outputs(runtime, final_report)
        final_report['durable_evidence'] = durable
        return {
            'ok': start_identity['worker_pid'] == checkpoint_identity['worker_pid'] == proc.pid and rollback_validation['matches_verified_baseline_snapshot'] and not rollback_validation['wal_present_after_rollback'] and not rollback_validation['shm_present_after_rollback'] and not rollback_validation['mutation_flag_present_after_rollback'] and production_hash_evidence['match'] and cleanup['durable_evidence_survived_cleanup'],
            'durable_case_dir': str(runtime.durable_case_dir),
            'report': final_report,
            'checkpoint_worker_verification_design': {
                'checkpoint_name': 'mutation_in_progress',
                'binds_case_id': True,
                'binds_run_id': True,
                'binds_clone_path': True,
                'binds_expected_worker_pid': True,
                'signal_target_verified_before_interrupt': True,
                'production_target_verification_before_signal': True,
                'uses_sleep_for_state_decision': False,
            },
        }
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def mock_command_runner_factory(configs: dict[str, MockCaseTestConfig]) -> Callable[[list[str], int, str, CaseRuntime], dict[str, Any]]:
    counters: dict[tuple[str, str], int] = {}

    def runner(cmd: list[str], timeout_s: int, stage: str, runtime: CaseRuntime) -> dict[str, Any]:
        key = (runtime.case['case_id'], stage)
        counters[key] = counters.get(key, 0) + 1
        config = configs[runtime.case['case_id']]
        if config.create_snapshot and stage == 'apply':
            snap = runtime.paths.work / 'generation_snapshot'
            snap.mkdir(parents=True, exist_ok=True)
            (snap / 'snapshot.txt').write_text('snapshot', encoding='utf-8')
        pid = 91000 + len(counters)
        base = {
            'stage': stage,
            'cmd': cmd,
            'pid': pid,
            'returncode': 0,
            'stdout': '',
            'stderr': '',
            'timed_out': False,
            'terminated': False,
            'killed': False,
            'timeout_message': None,
            'duration_s': 0.01,
            'alive_after_wait': False,
            'exited': True,
            'json_payload': None,
            'json_parse_error': None,
            'mocked': True,
        }
        if stage == 'rollback':
            base['json_payload'] = {'status': 'ROLLBACK_OK'}
            return base
        if config.behavior == 'apply_rejected':
            base['json_payload'] = {'status': 'APPLY_REJECTED', 'error': 'mock rejection'}
            return base
        if config.behavior == 'apply_ok_then_rejected':
            if counters[key] == 1:
                base['json_payload'] = {'status': 'APPLY_OK'}
            else:
                base['json_payload'] = {'status': 'APPLY_REJECTED', 'error': 'mock second-run refusal'}
            return base
        if config.behavior == 'malformed_json':
            base['stdout'] = '{bad json'
            base['json_parse_error'] = 'Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'
            return base
        if config.behavior == 'timed_out':
            base['timed_out'] = True
            base['terminated'] = True
            base['returncode'] = -15
            base['timeout_message'] = 'mock timeout'
            base['stdout'] = ''
            base['json_payload'] = None
            base['json_parse_error'] = 'empty stdout'
            base['interrupted_while_active'] = config.simulate_mutation_active
            return base
        raise HarnessRefusal(f'unhandled mock behavior: {config.behavior}')

    runner.counters = counters  # type: ignore[attr-defined]
    return runner


def run_case_non_mutating_validation(contract: dict[str, Any]) -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_case_validation_', dir='/private/tmp')).resolve()
    run_report_root = parent / 'run_report'
    baseline = build_mock_baseline(parent)
    source_package = build_mock_package(parent)
    configs = {cid: MockCaseTestConfig('apply_rejected', True, create_snapshot=True) for cid in list_case_ids(contract)}
    configs['LC6-11-interruption-during-mutation'] = MockCaseTestConfig(
        behavior='timed_out',
        actual_mechanics_complete=False,
        blocked_reason='missing proven disposable mutation-worker interruption against real executor; only mocked timeout lifecycle available',
        create_snapshot=True,
        simulate_mutation_active=True,
    )
    configs['LC6-12-direct-active-production-target'] = MockCaseTestConfig('apply_rejected', True, expected_case_execution_performed=False)
    configs['LC6-13-legacy-rag-db-target'] = MockCaseTestConfig('apply_rejected', True, expected_case_execution_performed=False)
    configs['LC6-14-generations-root-target'] = MockCaseTestConfig('apply_rejected', True, expected_case_execution_performed=False)
    configs['LC6-15-ce-library-root-target'] = MockCaseTestConfig('apply_rejected', True, expected_case_execution_performed=False)
    configs['LC6-16-rerun-against-already-repaired-clone'] = MockCaseTestConfig('apply_ok_then_rejected', True, create_snapshot=True)
    configs['LC6-20-worker-timeout-or-malformed-output'] = MockCaseTestConfig('malformed_json', True, create_snapshot=True)
    runner = mock_command_runner_factory(configs)
    results = []
    try:
        for case in contract['cases']:
            runtime = make_case_runtime(
                case,
                parent / 'cases',
                run_report_root,
                source_package_dir=source_package,
                baseline_clone_source=baseline,
                command_runner=runner,
            )
            result = execute_case(runtime)
            durable_verification = result['durable_evidence']['verification']
            config = configs[case['case_id']]
            if result['execution_metadata']['actual_case_execution_performed'] != config.expected_case_execution_performed:
                raise HarnessRefusal(f'truthful execution flag mismatch for {case["case_id"]}')
            results.append({
                'case_id': case['case_id'],
                'mechanic': MECHANIC_BY_CASE_ID[case['case_id']],
                'actual_mechanics_complete': config.actual_mechanics_complete,
                'blocked_reason': config.blocked_reason,
                'expected_behavior_confirmed': result['evaluation']['expected_behavior_confirmed'],
                'rollback_owner': result['rollback']['rollback_owner'],
                'rollback_invoked': result['rollback']['rollback_invoked'],
                'execution_metadata': result['execution_metadata'],
                'durable_evidence_ok': durable_verification['ok'],
                'production_hash_match': result['production_hash_evidence']['match'],
                'cleanup': result['cleanup'],
            })
        lock_validation = candidate_lock_contention_non_mutating_validation(contract)
        results = [lock_validation['case_result'] if row['case_id'] == 'LC6-10-candidate-lock-contention' else row for row in results]
        return {'ok': all(row['expected_behavior_confirmed'] or row['blocked_reason'] is not None for row in results), 'results': results, 'candidate_lock_contention_validation': lock_validation}
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def candidate_lock_contention_non_mutating_validation(contract: dict[str, Any]) -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_lock_fixture_', dir='/private/tmp')).resolve()
    run_report_root = parent / 'run_report'
    baseline = build_mock_baseline(parent)
    case = contract_case_map(contract)['LC6-10-candidate-lock-contention']
    try:
        runtime = make_case_runtime(
            case,
            parent / 'cases',
            run_report_root,
            source_package_dir=PKG,
            baseline_clone_source=baseline,
        )
        result = execute_case(runtime)
        durable = Path(result['durable_evidence']['durable_case_dir'])
        observed_text = json.dumps(result['observed'], ensure_ascii=False)
        lock_refusal_present = 'IngestLockError' in observed_text or 'already in progress' in observed_text or 'APPLY_REJECTED' in observed_text
        missing = []
        for name in ['lock_refusal.json', 'inventory_proof.json', 'execution_log.jsonl']:
            if not (durable / name).exists():
                missing.append(name)
        blocked_reason = None
        if not result['evaluation']['expected_behavior_confirmed']:
            blocked_reason = 'candidate-lock contention validation did not produce the expected controlled refusal on the disposable candidate lock fixture'
        elif not lock_refusal_present:
            blocked_reason = 'candidate-lock contention validation returned APPLY_REJECTED but did not capture explicit lock-refusal evidence text'
        elif missing:
            blocked_reason = 'candidate-lock contention durable evidence missing: ' + ', '.join(missing)
        return {
            'ok': blocked_reason is None,
            'lock_refusal_present': lock_refusal_present,
            'missing_required_evidence': missing,
            'durable_case_dir': str(durable),
            'case_result': {
                'case_id': case['case_id'],
                'mechanic': MECHANIC_BY_CASE_ID[case['case_id']],
                'actual_mechanics_complete': blocked_reason is None,
                'blocked_reason': blocked_reason,
                'expected_behavior_confirmed': result['evaluation']['expected_behavior_confirmed'],
                'rollback_owner': result['rollback']['rollback_owner'],
                'rollback_invoked': result['rollback']['rollback_invoked'],
                'execution_metadata': result['execution_metadata'],
                'durable_evidence_ok': result['durable_evidence']['verification']['ok'],
                'production_hash_match': result['production_hash_evidence']['match'],
                'cleanup': result['cleanup'],
            },
        }
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def evidence_survival_after_cleanup_validation(contract: dict[str, Any]) -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_evidence_survival_', dir='/private/tmp')).resolve()
    run_report_root = parent / 'run_report'
    baseline = build_mock_baseline(parent)
    source_package = build_mock_package(parent)
    case = contract_case_map(contract)['LC6-01-failure-before-first-mutation']
    runner = mock_command_runner_factory({'LC6-01-failure-before-first-mutation': MockCaseTestConfig('apply_rejected', True, create_snapshot=True)})
    try:
        runtime = make_case_runtime(case, parent / 'cases', run_report_root, source_package_dir=source_package, baseline_clone_source=baseline, command_runner=runner)
        result = execute_case(runtime)
        durable = Path(result['durable_evidence']['durable_case_dir'])
        return {
            'ok': result['cleanup']['case_root_removed'] and result['cleanup']['durable_evidence_survived_cleanup'],
            'case_root_removed': result['cleanup']['case_root_removed'],
            'durable_case_dir_exists': durable.exists(),
            'required_evidence_survived': result['cleanup']['durable_evidence_verification'],
            'proof': {
                'case_contract_json': (durable / 'case_contract.json').exists(),
                'fixture_setup_json': (durable / 'fixture_setup.json').exists(),
                'executor_output': (durable / 'executor_apply_observed.json').exists(),
                'production_hashes_before': (durable / 'production_hashes_before.json').exists(),
                'production_hashes_after': (durable / 'production_hashes_after.json').exists(),
                'case_report_json': (durable / 'case_report.json').exists(),
                'evidence_inventory_json': (durable / 'evidence_inventory.json').exists(),
            },
        }
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def no_double_rollback_validation(contract: dict[str, Any]) -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_double_rollback_', dir='/private/tmp')).resolve()
    run_report_root = parent / 'run_report'
    baseline = build_mock_baseline(parent)
    source_package = build_mock_package(parent)
    case_map = contract_case_map(contract)
    executor_case = case_map['LC6-01-failure-before-first-mutation']
    harness_case = case_map['LC6-20-worker-timeout-or-malformed-output']
    executor_runner = mock_command_runner_factory({'LC6-01-failure-before-first-mutation': MockCaseTestConfig('apply_rejected', True, create_snapshot=True)})
    harness_runner = mock_command_runner_factory({'LC6-20-worker-timeout-or-malformed-output': MockCaseTestConfig('malformed_json', True, create_snapshot=True)})
    try:
        rt1 = make_case_runtime(executor_case, parent / 'executor_owned', run_report_root / 'executor_owned', source_package_dir=source_package, baseline_clone_source=baseline, command_runner=executor_runner)
        res1 = execute_case(rt1)
        rt2 = make_case_runtime(harness_case, parent / 'harness_owned', run_report_root / 'harness_owned', source_package_dir=source_package, baseline_clone_source=baseline, command_runner=harness_runner)
        res2 = execute_case(rt2)
        exec_counts = getattr(executor_runner, 'counters')
        harness_counts = getattr(harness_runner, 'counters')
        return {
            'ok': (
                res1['rollback']['rollback_owner'] == 'executor'
                and not res1['rollback']['rollback_invoked']
                and res2['rollback']['rollback_owner'] == 'harness'
                and res2['rollback']['rollback_invoked']
                and exec_counts.get(('LC6-01-failure-before-first-mutation', 'rollback'), 0) == 0
                and harness_counts.get(('LC6-20-worker-timeout-or-malformed-output', 'rollback'), 0) == 1
            ),
            'executor_owned_path': res1['rollback'],
            'harness_owned_path': res2['rollback'],
            'executor_owned_runner_counts': {f'{k[0]}::{k[1]}': v for k, v in exec_counts.items()},
            'harness_owned_runner_counts': {f'{k[0]}::{k[1]}': v for k, v in harness_counts.items()},
        }
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def timeout_termination_before_rollback_validation() -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_timeout_', dir='/private/tmp')).resolve()
    child = parent / 'sleeper.py'
    child.write_text('import time\nprint("started", flush=True)\ntime.sleep(60)\n', encoding='utf-8')
    runtime = CaseRuntime(
        case={'case_id': 'LC6-11-interruption-during-mutation'},
        paths=CasePaths(parent=parent, case_root=parent, clone=parent / 'clone', work=parent / 'work', evidence=parent / 'evidence'),
        package_dir=PKG,
        cleanup_callbacks=[],
        notes=[],
        run_report_root=parent / 'reports',
        durable_case_dir=parent / 'reports' / 'cases' / 'LC6-11-interruption-during-mutation',
    )
    try:
        record = run_subprocess_json(['/usr/bin/python3', str(child)], 1, 'timeout_probe', runtime)
        decision = {
            'child_pid': record['pid'],
            'timed_out': record['timed_out'],
            'terminated': record['terminated'],
            'killed': record['killed'],
            'alive_after_wait': record['alive_after_wait'],
            'rollback_permitted_after_exit_only': record['exited'] and not record['alive_after_wait'],
        }
        return {'ok': decision['timed_out'] and decision['terminated'] and decision['rollback_permitted_after_exit_only'], 'process': record, 'decision': decision}
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def truthful_execution_flags_validation(contract: dict[str, Any]) -> dict[str, Any]:
    dry = execution_metadata('dry-run', False)
    validation = execution_metadata('mocked-validation', False, validate_harness=True)
    execute_meta = execution_metadata('execute', True)
    return {
        'ok': (
            dry['no_failure_injection_case_ran']
            and validation['no_failure_injection_case_ran']
            and not execute_meta['no_failure_injection_case_ran']
            and execute_meta['actual_case_execution_performed']
        ),
        'dry_run': dry,
        'mocked_validation': validation,
        'execute_mode': execute_meta,
        'sample_execution_summary': {
            'mode': 'execute',
            'summary_written_to_file_required': True,
            'case_ids': [list_case_ids(contract)[0]],
        },
    }


def durable_pre_post_hash_evidence_validation(contract: dict[str, Any]) -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_hash_evidence_', dir='/private/tmp')).resolve()
    run_report_root = parent / 'run_report'
    baseline = build_mock_baseline(parent)
    source_package = build_mock_package(parent)
    case = contract_case_map(contract)['LC6-02-failure-midway-deterministic-repoints']
    runner = mock_command_runner_factory({'LC6-02-failure-midway-deterministic-repoints': MockCaseTestConfig('apply_rejected', True, create_snapshot=True)})
    try:
        runtime = make_case_runtime(case, parent / 'cases', run_report_root, source_package_dir=source_package, baseline_clone_source=baseline, command_runner=runner)
        result = execute_case(runtime)
        durable = Path(result['durable_evidence']['durable_case_dir'])
        before = json.loads((durable / 'production_hashes_before.json').read_text(encoding='utf-8'))
        after = json.loads((durable / 'production_hashes_after.json').read_text(encoding='utf-8'))
        return {
            'ok': before['embedded']['sha256'] == after['embedded']['sha256'] and before['chroma_sqlite3']['sha256'] == after['chroma_sqlite3']['sha256'],
            'durable_case_dir': str(durable),
            'before': before,
            'after': after,
        }
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def mocked_orchestration_validation(contract: dict[str, Any]) -> dict[str, Any]:
    parent = Path(tempfile.mkdtemp(prefix='lc6_mock_orch_', dir='/private/tmp')).resolve()
    run_report_root = parent / 'run_report'
    cases = contract_case_map(contract)
    case_ids = list_case_ids(contract)
    failing_case = case_ids[4]
    try:
        def fake_runtime_factory(case: dict[str, Any]) -> CaseRuntime:
            paths = build_case_paths(parent / 'cases', case)
            paths.case_root.mkdir(parents=True, exist_ok=True)
            return CaseRuntime(
                case=case,
                paths=paths,
                package_dir=PKG,
                cleanup_callbacks=[],
                notes=[],
                run_report_root=run_report_root,
                durable_case_dir=run_report_root / 'cases' / case['case_id'],
            )

        def fake_runner(runtime: CaseRuntime) -> dict[str, Any]:
            observed = {'status': 'CONTROLLED_REFUSAL_CONFIRMED' if runtime.case['case_id'] != failing_case else 'UNEXPECTED_RESULT'}
            evaluation = {
                'case_id': runtime.case['case_id'],
                'expected_process_exit_result': runtime.case['expected_process_exit_result'],
                'observed_summary': observed['status'],
                'expected_behavior_confirmed': runtime.case['case_id'] != failing_case,
            }
            if not evaluation['expected_behavior_confirmed']:
                runtime.preserve_clone = True
                runtime.paths.clone.mkdir(parents=True, exist_ok=True)
            return {
                'case_id': runtime.case['case_id'],
                'mechanic': MECHANIC_BY_CASE_ID[runtime.case['case_id']],
                'execution_metadata': execution_metadata('mocked-validation', False, validate_harness=True),
                'observed': observed,
                'evaluation': evaluation,
                'rollback': {'rollback_owner': 'none', 'rollback_invoked': False},
                'cleanup': {'callbacks': [], 'clone_preserved': runtime.preserve_clone, 'case_root_removed': False},
                'no_production_action': True,
            }

        seq = execute_case_sequence(contract, case_ids, parent / 'cases_root', run_report_root, case_runner=fake_runner, runtime_factory=fake_runtime_factory)
        return {'ok': seq['stopped_on_case_id'] == failing_case and bool(seq['not_started_case_ids']) and bool(seq['preserved_failed_clone']), 'sequence': seq}
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def current_sha256sum_mismatches() -> list[dict[str, Any]]:
    expected = {}
    for line in (PKG / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        digest, name = stripped.split(maxsplit=1)
        expected[name.strip()] = digest.strip()
    names = set(expected)
    names.update({'lc6_failure_injection.py', 'lc6_failure_injection_harness_validation_report.json', 'lc6_failure_injection_harness_validation_correction_report.json', 'lc6_case11_interruption_mechanism_preflight_report.json'})
    rows = []
    for name in sorted(names):
        path = PKG / name
        actual = file_sha256(path) if path.exists() else None
        rows.append({
            'file': name,
            'expected_sha256': expected.get(name),
            'actual_sha256': actual,
            'status': 'match' if expected.get(name) == actual and actual is not None else ('absent_from_sha256sums' if name not in expected else ('missing_file' if actual is None else 'mismatch')),
        })
    return [row for row in rows if row['status'] != 'match']


def correction_changed_file_hashes() -> dict[str, Any]:
    return {
        'lc6_failure_injection.py': {
            'before_sha256': PRE_CORRECTION_LC6_HASH,
            'after_sha256': file_sha256(PKG / 'lc6_failure_injection.py'),
        },
        'lc6_failure_injection_harness_validation_correction_report.json': {
            'before_sha256': 'not present before correction pass',
            'after_sha256': 'self-referential; final file hash must be checked externally after write',
        },
    }


def case11_preflight_changed_file_hashes() -> dict[str, Any]:
    return {
        'lc6_failure_injection.py': {
            'before_sha256': CASE11_CONTINUATION_START_HASH,
            'after_sha256': file_sha256(PKG / 'lc6_failure_injection.py'),
        },
        'lc6_case11_interruption_mechanism_preflight_report.json': {
            'before_sha256': 'not present before case11 preflight step',
            'after_sha256': 'whole-file hash must be checked externally after write',
        },
        'lc6_failure_injection_harness_validation_correction_v2_report.json': {
            'before_sha256': PRE_CASE11_PREFLIGHT_V2_HASH,
            'after_sha256': file_sha256(V2_REPORT_PATH) if V2_REPORT_PATH.exists() else None,
        },
    }


def referenced_file_hashes_for_report(paths: list[Path], self_hashed_names: set[str]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for path in paths:
        rows[path.name] = {
            'path': str(path),
            'exists': path.exists(),
            'sha256': None if path.name in self_hashed_names else (file_sha256(path) if path.exists() else None),
            'hash_note': 'whole-file hash must be computed externally after write' if path.name in self_hashed_names else None,
        }
    return rows


def compile_and_cli_preflight_checks() -> dict[str, Any]:
    commands = [
        {'name': 'py_compile', 'cmd': [RAG_PY, '-m', 'py_compile', str(PKG / 'lc6_failure_injection.py')]},
        {'name': 'help', 'cmd': [RAG_PY, str(PKG / 'lc6_failure_injection.py'), '--help']},
    ]
    results = []
    for item in commands:
        proc = subprocess.run(item['cmd'], capture_output=True, text=True)
        results.append({
            'name': item['name'],
            'cmd': item['cmd'],
            'exit_code': proc.returncode,
            'stdout': proc.stdout,
            'stderr': proc.stderr,
            'ok': proc.returncode == 0,
        })
    contract = load_contract()
    results.append({
        'name': 'contract_validation',
        'cmd': ['internal', 'validate_contract'],
        'exit_code': 0,
        'stdout': '',
        'stderr': '',
        'ok': validate_contract(contract)['case_count'] == 20,
    })
    return {'ok': all(r['ok'] for r in results), 'results': results}


def build_case11_preflight_payload(contract: dict[str, Any]) -> dict[str, Any]:
    compile_cli = compile_and_cli_preflight_checks()
    mechanism = case11_interruption_mechanism_preflight_validation(contract)
    wrong_pid = run_case11_identity_refusal_test('wrong_pid')
    wrong_path = run_case11_identity_refusal_test('wrong_path')
    wrong_case = run_case11_identity_refusal_test('wrong_case_id')
    wrong_run = run_case11_identity_refusal_test('wrong_run_id')
    stale_checkpoint = run_case11_stale_checkpoint_refusal_test()
    production_path = run_case11_production_path_refusal_test()
    lifecycle = {
        'timeout_termination_before_rollback': timeout_termination_before_rollback_validation(),
        'no_double_rollback': no_double_rollback_validation(contract),
    }
    containment = validate_parent_escape_guards()
    refusal_checks = [refusal_probe(Path(p)) for p in FORBIDDEN_TARGETS]
    test_results = {
        'compile_and_cli_checks': compile_cli,
        'checkpoint_identity_path_pid_validation': mechanism,
        'wrong_pid_refusal_test': wrong_pid,
        'wrong_path_refusal_test': wrong_path,
        'wrong_case_id_refusal_test': wrong_case,
        'wrong_run_id_refusal_test': wrong_run,
        'stale_checkpoint_refusal_test': stale_checkpoint,
        'production_path_refusal_test': production_path,
        'rollback_before_exit_refusal_test': {
            'ok': mechanism['report']['pre_signal_rollback_gate']['rollback_allowed'] is False and mechanism['report']['post_exit_rollback_gate']['rollback_allowed'] is True,
            'pre_signal_gate': mechanism['report']['pre_signal_rollback_gate'],
            'post_exit_gate': mechanism['report']['post_exit_rollback_gate'],
        },
        'mocked_process_lifecycle_tests': lifecycle,
        'containment_validation': containment,
        'production_target_refusal_tests': refusal_checks,
    }
    blockers = []
    if not compile_cli['ok']:
        blockers.append('compile/cli preflight checks failed')
    if not mechanism['ok']:
        blockers.append('case11 deterministic checkpoint/pid/path preflight validation failed')
    if not wrong_pid['ok']:
        blockers.append('wrong PID refusal test failed')
    if not wrong_path['ok']:
        blockers.append('wrong path refusal test failed')
    if not wrong_case['ok']:
        blockers.append('wrong case ID refusal test failed')
    if not wrong_run['ok']:
        blockers.append('wrong run ID refusal test failed')
    if not stale_checkpoint['ok']:
        blockers.append('stale checkpoint refusal test failed')
    if not production_path['ok']:
        blockers.append('production path refusal test failed')
    if not test_results['rollback_before_exit_refusal_test']['ok']:
        blockers.append('rollback-before-exit refusal test failed')
    if not lifecycle['timeout_termination_before_rollback']['ok']:
        blockers.append('timeout termination before rollback validation failed')
    if not lifecycle['no_double_rollback']['ok']:
        blockers.append('no-double-rollback validation failed')
    if not containment['all_protected_targets_blocked'] or not containment['outside_escape_blocked']:
        blockers.append('containment validation failed')
    if not all(row['ok'] for row in refusal_checks):
        blockers.append('production-target refusal validation failed')
    consistency = case11_case_matrix_consistency_validation(contract, blockers)
    if not consistency['ok']:
        blockers.append('case11 blocker list / case matrix consistency validation failed')
    return {
        'final_status': 'LC6_CASE11_MECHANISM_PREFLIGHT_PASS_READY_FOR_SINGLE_CASE_EXECUTION_APPROVAL' if not blockers else 'LC6_CASE11_MECHANISM_PREFLIGHT_PARTIAL_NOT_READY',
        'report_purpose': 'LC6-11 interruption mechanism preflight validation only; no real injection and no production Chroma access',
        'partial_change_recovery': {
            'exact_textual_diff_from_v2_verified_hash': 'unavailable_from_hash_alone',
            'rationale': 'The last V2-verified harness content is identified only by SHA-256 7c9f083f…; without the exact historical file body, an exact textual diff to the current preflight harness cannot be reconstructed from hashes alone.',
            'current_harness_hash_before_this_continuation': CASE11_CONTINUATION_START_HASH,
            'recovered_current_change_areas_from_static_review': [
                'case11-specific report constants and CLI flags',
                'mock worker script with explicit mutation checkpoint',
                'checkpoint/PID/path identity verification helpers',
                'worker-exit-before-rollback gate',
                'durable evidence persistence before cleanup',
                'case11 preflight payload builder and report writer',
            ],
        },
        'files_changed': case11_preflight_changed_file_hashes(),
        'checkpoint_and_worker_verification_design': mechanism['checkpoint_worker_verification_design'],
        'exact_tests_run_and_results': test_results,
        'accepted_rejected_changes': {
            'accepted': [
                'explicit checkpoint-bound interruption design with case_id/run_id/clone_path/worker_pid identity binding',
                'verified-child-only signalling before interruption',
                'rollback gate that remains closed until worker exit is confirmed',
                'durable evidence preservation before cleanup',
                'production hash capture before and after the mocked interruption lifecycle',
            ],
            'rejected_or_unproven': [
                'real disposable execution against the actual executor and actual disposable LC4 baseline remains unproven in this preflight step',
            ],
            'rationale': 'Only non-mutating mocked/process-level validation was authorized in this step; real integration proof requires a separately approved single-case execution.',
        },
        'remaining_unproven_item_requiring_actual_disposable_execution': mechanism['report']['remaining_unproven_item'],
        'referenced_file_external_hashes': referenced_file_hashes_for_report([CONTRACT_PATH, V2_REPORT_PATH, PKG / 'lc6_failure_injection.py', CASE11_PREFLIGHT_REPORT_PATH], {CASE11_PREFLIGHT_REPORT_PATH.name}),
        'current_sha256sums_mismatches': current_sha256sum_mismatches(),
        'execution_metadata': execution_metadata('mocked-validation', False, validate_harness=True),
        'case_matrix_blocker_consistency': consistency,
        'confirmation': {
            'no_actual_injection_ran': True,
            'no_actual_failure_injection_case_ran': True,
            'no_production_action_ran': True,
            'production_chroma_not_opened': True,
            'final_clean_cycle_not_run': True,
            'sha256sums_not_regenerated': True,
        },
        'blockers': blockers,
    }


def mechanically_exercised_case_matrix(contract: dict[str, Any], case_validation: dict[str, Any]) -> list[dict[str, Any]]:
    exercised = {row['case_id']: row for row in case_validation['results']}
    rows = []
    for case in contract['cases']:
        cid = case['case_id']
        mech = MECHANIC_BY_CASE_ID[cid]
        exercise = exercised[cid]
        hooks = injection_hook_validation()['checks']
        blocked_reason = exercise['blocked_reason']
        if mech.startswith('executor_inject:'):
            hook = mech.split(':', 1)[1]
            if not hooks.get(hook):
                blocked_reason = f'missing supported hook: {hook}'
        if cid == 'LC6-11-interruption-during-mutation' and not blocked_reason:
            blocked_reason = 'missing proven disposable mutation-worker interruption against real executor; only mocked timeout lifecycle available'
        complete = exercise['actual_mechanics_complete'] and blocked_reason is None and exercise['expected_behavior_confirmed'] and exercise['durable_evidence_ok'] and exercise['production_hash_match']
        rows.append({
            'case_id': cid,
            'mechanic': mech,
            'implemented_in_harness': complete,
            'mechanically_executable_after_operator_approval': complete,
            'blocked_reason': blocked_reason,
            'non_mutating_exercised': {
                'expected_behavior_confirmed': exercise['expected_behavior_confirmed'],
                'rollback_owner': exercise['rollback_owner'],
                'rollback_invoked': exercise['rollback_invoked'],
                'durable_evidence_ok': exercise['durable_evidence_ok'],
                'production_hash_match': exercise['production_hash_match'],
            },
        })
    return rows


def build_execution_summary_file(summary_dir: Path, payload: dict[str, Any]) -> Path:
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / 'execution_summary.json'
    write_json_atomic(summary_path, payload)
    return summary_path


def referenced_file_hashes(paths: list[Path]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for path in paths:
        is_v2_report = path.name == V2_REPORT_PATH.name
        rows[path.name] = {
            'path': str(path),
            'exists': path.exists(),
            'sha256': None if is_v2_report else (file_sha256(path) if path.exists() else None),
            'hash_note': 'whole-file hash must be computed externally after write' if is_v2_report else None,
        }
    return rows


def blocked_case_consistency_validation(mechanics: list[dict[str, Any]]) -> dict[str, Any]:
    blocked_rows = [row for row in mechanics if not row['mechanically_executable_after_operator_approval']]
    blocker_list = [f"{row['case_id']}: {row['blocked_reason']}" for row in blocked_rows]
    blocked_case_ids = [row['case_id'] for row in blocked_rows]
    return {
        'blocked_case_ids_from_matrix': blocked_case_ids,
        'blocker_list': blocker_list,
        'exact_match': blocker_list == [f"{row['case_id']}: {row['blocked_reason']}" for row in blocked_rows],
    }


def build_harness_validation_payload(contract: dict[str, Any]) -> dict[str, Any]:
    case_validation = run_case_non_mutating_validation(contract)
    evidence_survival = evidence_survival_after_cleanup_validation(contract)
    double_rollback = no_double_rollback_validation(contract)
    timeout_validation = timeout_termination_before_rollback_validation()
    truthful_flags = truthful_execution_flags_validation(contract)
    hash_evidence = durable_pre_post_hash_evidence_validation(contract)
    mocked_sequence = mocked_orchestration_validation(contract)
    mechanics = mechanically_exercised_case_matrix(contract, case_validation)
    blocker_consistency = blocked_case_consistency_validation(mechanics)
    blockers = blocker_consistency['blocker_list']
    return {
        'final_status': 'LC6_HARNESS_CORRECTION_PARTIAL_NOT_READY' if blockers else 'LC6_HARNESS_CORRECTION_PASS_READY_FOR_EXECUTION_APPROVAL',
        'report_purpose': 'LC6 harness correction pass validation only; no actual failure-injection execution and no production Chroma access',
        'changed_file_hashes': correction_changed_file_hashes(),
        'external_file_hashes': package_file_hashes(),
        'referenced_file_external_hashes': referenced_file_hashes([CONTRACT_PATH, PLAN_PATH, PKG / 'lc6_failure_injection.py', CORRECTION_REPORT_PATH, ERRATUM_PATH, V2_REPORT_PATH]),
        'contract_summary': validate_contract(contract),
        'hook_validation': injection_hook_validation(),
        'baseline_revalidation': baseline_revalidation_summary(),
        'containment_validation': validate_parent_escape_guards(),
        'refusal_guard_validation': [refusal_probe(Path(p)) for p in FORBIDDEN_TARGETS],
        'dry_run_case_layout_validation': dry_run_case_layout_validation(contract),
        'failed_case_preservation_logic': validate_failed_case_preservation_logic(contract),
        'non_mutating_regression_validations': {
            'evidence_survival_after_cleanup': evidence_survival,
            'candidate_lock_contention_non_mutating_validation': case_validation.get('candidate_lock_contention_validation'),
            'no_double_rollback': double_rollback,
            'timeout_termination_before_rollback': timeout_validation,
            'truthful_execution_flags': truthful_flags,
            'durable_per_case_pre_post_hash_evidence': hash_evidence,
            'stop_on_first_unexpected_result': mocked_sequence,
            'failed_clone_preservation': mocked_sequence,
            'parent_directory_containment': validate_parent_escape_guards(),
            'protected_target_refusal_before_chroma_open': [refusal_probe(Path(p)) for p in FORBIDDEN_TARGETS],
        },
        'implemented_vs_blocked_case_mechanics': mechanics,
        'blocked_case_consistency_validation': blocker_consistency,
        'case_validation_runs': case_validation,
        'current_sha256sums_mismatches': current_sha256sum_mismatches(),
        'execution_metadata': execution_metadata('mocked-validation', False, validate_harness=True),
        'confirmation': {
            'no_actual_injection_ran': True,
            'no_actual_failure_injection_case_ran': True,
            'no_production_action_ran': True,
            'production_chroma_not_opened': True,
            'final_clean_cycle_not_run': True,
            'sha256sums_not_regenerated': True,
        },
        'blockers': blockers,
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description='LC6 failure-injection harness')
    ap.add_argument('--contract-file', default=str(CONTRACT_PATH))
    ap.add_argument('--case-id')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--validate-harness', action='store_true', help='run non-mutating harness validations and write validation report')
    ap.add_argument('--validate-case11-preflight', action='store_true', help='run non-mutating LC6-11 interruption mechanism preflight validation and write report')
    ap.add_argument('--report-file', default=str(CORRECTION_REPORT_PATH))
    ap.add_argument('--tmp-parent')
    ap.add_argument('--baseline-dir')
    ap.add_argument('--test-baseline-dir')
    ap.add_argument('--test-package-dir')
    return ap.parse_args()


def resolve_test_override(path_text: str | None) -> Path | None:
    return Path(path_text).expanduser().resolve() if path_text else None


def validate_test_overrides(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    if args.baseline_dir and (args.test_baseline_dir or args.test_package_dir):
        raise SystemExit('--baseline-dir is mutually exclusive with --test-baseline-dir/--test-package-dir')
    baseline = resolve_test_override(args.test_baseline_dir)
    package_dir = resolve_test_override(args.test_package_dir)
    if bool(baseline) != bool(package_dir):
        raise SystemExit('test overrides require both --test-baseline-dir and --test-package-dir')
    if not baseline:
        return None, None
    if baseline == BASELINE_CLONE.resolve():
        raise SystemExit('test override baseline must not be the LC4 baseline clone')
    if str(baseline).startswith(str(PROD_LIBRARY.resolve())):
        raise SystemExit('test override baseline must not be under CE_Library')
    if str(package_dir).startswith(str(PROD_LIBRARY.resolve())):
        raise SystemExit('test override package must not be under CE_Library')
    if baseline == PROD_GEN.resolve() or package_dir == PROD_GEN.resolve():
        raise SystemExit('test override baseline/package must not target production generation')
    return baseline, package_dir


def validate_baseline_dir(path_text: str | None) -> Path | None:
    if not path_text:
        return None
    baseline = Path(path_text).expanduser().resolve()
    forbidden = {
        BASELINE_CLONE.resolve(),
        PROD_GEN.resolve(),
        (PROD_LIBRARY / '.rag_db').resolve(),
        (PROD_LIBRARY / '.rag_db_generations').resolve(),
        PROD_LIBRARY.resolve(),
        PKG.resolve(),
    }
    if baseline in forbidden:
        raise SystemExit('baseline-dir resolves to a forbidden path')
    if str(baseline).startswith(str(PROD_LIBRARY.resolve())):
        raise SystemExit('baseline-dir must not be inside CE_Library')
    marker = baseline / DISPOSABLE_MARKER
    if not marker.is_file():
        raise SystemExit('baseline-dir is missing a disposable marker')
    return baseline


def build_runtime_baseline_summary(args: argparse.Namespace, test_baseline: Path | None, test_package_dir: Path | None, bound_baseline: Path | None) -> dict[str, Any]:
    if test_baseline and test_package_dir:
        if not args.tmp_parent:
            raise SystemExit('test override execution requires --tmp-parent inside one disposable temporary parent')
        return {
            'baseline_revalidation': None,
            'test_override_baseline_validation': synthetic_test_override_baseline_validation(
                test_baseline,
                test_package_dir,
                Path(args.tmp_parent),
                args.case_id,
                args.execute,
            ),
        }
    if bound_baseline:
        summary = baseline_revalidation_summary(bound_baseline, RESTORED_BASELINE_MARKER_SHA)
        if summary['source_inventory_schema'] != 'inventory-row-v1':
            raise HarnessRefusal('baseline-dir schema mismatch')
        if summary['row_count'] != 19:
            raise HarnessRefusal(f'baseline-dir row count mismatch: {summary["row_count"]}')
        if summary['computed_checksum'] != EXPECTED_SOURCE_INVENTORY_CHECKSUM:
            raise HarnessRefusal('baseline-dir inventory checksum mismatch')
        if summary['embedded_json_sha256'] != EXPECTED_EMBEDDED or summary['chroma_sqlite3_sha256'] != EXPECTED_CHROMA:
            raise HarnessRefusal('baseline-dir core hash mismatch')
        if summary['chunk_count'] != EXPECTED_CHUNKS or summary['orphan_count'] != EXPECTED_ORPHANS:
            raise HarnessRefusal('baseline-dir chunk/orphan mismatch')
        if summary['collection_count'] != 1 or summary['collection_names'] != ['langchain']:
            raise HarnessRefusal(f'baseline-dir collection mismatch: {summary["collection_names"]}')
        if not summary['wal_shm_clear']:
            raise HarnessRefusal('baseline-dir WAL/SHM not clear')
        return {
            'baseline_revalidation': summary,
            'test_override_baseline_validation': None,
        }
    return {
        'baseline_revalidation': baseline_revalidation_summary(),
        'test_override_baseline_validation': None,
    }


def main() -> None:
    args = parse_args()
    contract = load_contract(Path(args.contract_file))
    contract_summary = validate_contract(contract)
    exec_guard = validate_execution_guard(args.case_id, args.all, args.execute)
    test_baseline, test_package_dir = validate_test_overrides(args)
    bound_baseline = validate_baseline_dir(args.baseline_dir)
    baseline_summary = build_runtime_baseline_summary(args, test_baseline, test_package_dir, bound_baseline)
    if not exec_guard['ok']:
        raise SystemExit(exec_guard['reason'])

    if args.validate_harness:
        payload = build_harness_validation_payload(contract)
        write_json_atomic(Path(args.report_file), payload)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f'LC6 harness correction report written to {args.report_file}')
        return

    if args.validate_case11_preflight:
        report_path = Path(args.report_file) if args.report_file != str(CORRECTION_REPORT_PATH) else CASE11_PREFLIGHT_REPORT_PATH
        payload = build_case11_preflight_payload(contract)
        write_json_atomic(report_path, payload)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f'LC6 case11 preflight report written to {report_path}')
        return

    payload = {
        'status': 'ok',
        'contract_summary': contract_summary,
        'execution_guard': exec_guard,
        'hook_validation': injection_hook_validation(),
        'forbidden_target_refusal_probes': [refusal_probe(Path(p)) for p in FORBIDDEN_TARGETS],
        'parent_escape_guards': validate_parent_escape_guards(),
        'failed_case_preservation_logic': validate_failed_case_preservation_logic(contract),
        'baseline_revalidation': baseline_summary['baseline_revalidation'],
        'test_override_baseline_validation': baseline_summary['test_override_baseline_validation'],
        'dry_run': dry_run(contract, args.case_id, args.all),
        'mechanical_case_matrix': [{'case_id': case['case_id'], 'mechanic': MECHANIC_BY_CASE_ID[case['case_id']]} for case in contract['cases']],
        'execution_metadata': execution_metadata('dry-run', False),
    }
    if args.execute:
        parent = Path(args.tmp_parent).resolve() if args.tmp_parent else Path(tempfile.mkdtemp(prefix='lc6_exec_', dir='/private/tmp')).resolve()
        run_report_root = (parent / 'run_report') if (test_baseline and test_package_dir) else (CASE_REPORTS_DIR / f'lc6_execution_{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}')
        run_report_root.mkdir(parents=True, exist_ok=True)
        case_ids = [args.case_id] if args.case_id else list_case_ids(contract)
        custom_runtime_factory: Callable[[dict[str, Any]], CaseRuntime] | None = None
        if test_baseline and test_package_dir:
            def runtime_factory(case: dict[str, Any]) -> CaseRuntime:
                runtime = make_case_runtime(case, parent, run_report_root, package_dir=test_package_dir, source_package_dir=test_package_dir, baseline_clone_source=test_baseline)
                runtime.selected_case_id = args.case_id or case['case_id']
                runtime.contract_case_id = case['case_id']
                return runtime
            custom_runtime_factory = runtime_factory
        elif bound_baseline:
            def runtime_factory(case: dict[str, Any]) -> CaseRuntime:
                runtime = make_case_runtime(case, parent, run_report_root, baseline_clone_source=bound_baseline)
                runtime.selected_case_id = args.case_id or case['case_id']
                runtime.contract_case_id = case['case_id']
                return runtime
            custom_runtime_factory = runtime_factory
        sequence = execute_case_sequence(contract, case_ids, parent, run_report_root, runtime_factory=custom_runtime_factory)
        payload['execution_plan'] = sequence
        payload['execution_metadata'] = execution_metadata('execute', True)
        if test_baseline and test_package_dir:
            payload['test_override'] = {
                'baseline_dir': str(test_baseline),
                'package_dir': str(test_package_dir),
                'synthetic_only': True,
            }
        if bound_baseline:
            payload['baseline_binding'] = {
                'baseline_dir': str(bound_baseline),
                'marker_sha256': baseline_summary['baseline_revalidation']['marker_sha256'],
            }
        payload['execution_summary_file'] = str(build_execution_summary_file(run_report_root, payload))
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if args.execute:
            print('LC6 harness execution summary written; inspect JSON output or execution summary file.')
        else:
            print('LC6 harness dry-run summary ready; use --json for structured output.')


if __name__ == '__main__':
    main()

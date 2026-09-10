#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from orphan_repair_common import (
    PROD_GEN,
    PROD_LIBRARY,
    RETIRE_DIGEST,
    artifact_inventory,
    chunk_counts_by_id,
    chunk_rows_by_source_hash,
    collection_assignment_reason,
    count_embeddings,
    doctor_json,
    fetch_chunk_payload,
    file_sha256,
    open_client_collection,
    parse_orphan_count,
    scope_stats_json,
    tracker_load,
    tree_hashes,
    validate_no_placeholders,
    write_json_atomic,
)

PKG = Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z')
MANIFEST_PATH = PKG / 'atomic_orphan_repair_manifest.json'
BASELINE_PATH = PKG / 'production_baseline.json'
TRANSITIONS_PATH = PKG / 'collection_transition_report.json'
ACCEPTANCE_PATH = PKG / 'retrieval_acceptance_tests.json'

FAILED_CASES = [
    {
        'name': 'lgip_forum_service_experience',
        'canonical_live_path': '00_Career/03_Engine_Knowledge/Training/MAN_Academy/ME_LGIP/3_LGIP service experience_LGIP Owners Forum_Dec 2022.pdf',
        'intended_scope': 'maker-manuals',
        'original_query': 'LGIP service experience owners forum December 2022',
    },
    {
        'name': 'me_gi_intro_march_2026',
        'canonical_live_path': '00_Career/03_Engine_Knowledge/Training/MAN_Academy/ME_GI/2026_03/01_Introduction/01 Introduction to ME-GI(Mar 2026).pdf',
        'intended_scope': 'maker-manuals',
        'original_query': '01 Introduction to ME-GI March 2026',
    },
    {
        'name': 'sl2022_725',
        'canonical_live_path': '00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2022-725.pdf',
        'intended_scope': 'maker-manuals',
        'original_query': 'SL2022-725',
    },
]


def manifest_payload(manifest: dict[str, Any]) -> bytes:
    payload = {k: v for k, v in manifest.items() if k != 'manifest_payload_sha256'}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def build() -> None:
    tracker = tracker_load(PROD_GEN)
    client, coll, collection_name = open_client_collection(PROD_GEN)
    sqlite_path = PROD_GEN / 'chroma.sqlite3'

    path_to_digests: dict[str, list[str]] = defaultdict(list)
    chunk_to_digests: dict[str, list[str]] = defaultdict(list)
    orphan_occurrences: list[tuple[str, str]] = []
    orphan_digests: set[str] = set()
    for digest, rec in tracker.items():
        for p in rec.get('paths', []):
            path_to_digests[p].append(digest)
            if not (PROD_LIBRARY / p).is_file():
                orphan_occurrences.append((digest, p))
                orphan_digests.add(digest)
        for cid in rec.get('chunk_ids', []):
            chunk_to_digests[cid].append(digest)

    exact_matches: dict[str, list[str]] = defaultdict(list)
    for full in sorted(p for p in PROD_LIBRARY.rglob('*') if p.is_file()):
        parts = set(full.parts)
        if '.rag_db_generations' in parts or '.rag_state' in parts or '.git' in parts:
            continue
        rel = full.relative_to(PROD_LIBRARY).as_posix()
        digest = file_sha256(full)
        if digest in orphan_digests:
            exact_matches[digest].append(rel)

    operations: list[dict[str, Any]] = []
    deterministic_stale_path_occurrences = 0
    deterministic_digests = 0
    retirement_digests = 0
    transition_rows: list[dict[str, Any]] = []
    collection_move_chunk_total = 0

    for digest in sorted(orphan_digests):
        rec = tracker[digest]
        old_paths = list(rec.get('paths') or [])
        stale_old_paths = [p for p in old_paths if not (PROD_LIBRARY / p).is_file()]
        live_old_paths = [p for p in old_paths if (PROD_LIBRARY / p).is_file()]
        chunk_ids = list(rec.get('chunk_ids') or [])
        chunk_payload = fetch_chunk_payload(coll, chunk_ids)
        chunk_counts = chunk_counts_by_id(sqlite_path, chunk_ids)
        by_hash = sorted(chunk_rows_by_source_hash(sqlite_path, digest))
        if sorted(chunk_ids) != by_hash:
            raise RuntimeError(f'source-hash rows mismatch for {digest}')
        current_state = {}
        for cid in chunk_ids:
            meta = dict(chunk_payload[cid]['metadata'])
            current_state[cid] = {
                'source': meta.get('source'),
                'source_hash': meta.get('source_hash'),
                'collection': meta.get('collection'),
                'document_type': meta.get('document_type'),
                'authority_rank': meta.get('authority_rank'),
                'page': meta.get('page'),
                'sqlite_row_count': chunk_counts[cid],
            }
            if meta.get('source_hash') != digest:
                raise RuntimeError(f'source_hash mismatch {digest} {cid}')
            if chunk_counts[cid] != 1:
                raise RuntimeError(f'chunk not exactly once {digest} {cid} {chunk_counts[cid]}')

        if digest == RETIRE_DIGEST:
            retirement_digests += 1
            for cid in chunk_ids:
                if chunk_to_digests[cid] != [digest]:
                    raise RuntimeError(f'retirement chunk shared {cid} {chunk_to_digests[cid]}')
            operations.append({
                'op_type': 'RETIRE_UNRECOVERABLE',
                'digest': digest,
                'old_paths': old_paths,
                'stale_old_paths': stale_old_paths,
                'live_old_paths': live_old_paths,
                'canonical_live_path': None,
                'verified_live_sha256': None,
                'tracker_collection': rec.get('collection'),
                'derived_collection': None,
                'chunk_ids': chunk_ids,
                'expected_chunk_count': len(chunk_ids),
                'current_chunk_state': current_state,
                'target_path_conflict_digests': [],
                'exact_live_matches': exact_matches.get(digest, []),
            })
            continue

        matches = exact_matches.get(digest, [])
        if len(matches) != 1:
            raise RuntimeError(f'deterministic digest {digest} live matches {matches}')
        canonical = matches[0]
        target_conflicts = sorted(set(path_to_digests.get(canonical, [])) - {digest})
        if target_conflicts:
            raise RuntimeError(f'target conflict {digest} {canonical} {target_conflicts}')
        actual_sha = file_sha256(PROD_LIBRARY / canonical)
        if actual_sha != digest:
            raise RuntimeError(f'sha mismatch {digest} {canonical} {actual_sha}')
        derived = collection_assignment_reason(canonical)
        op_type = 'REPOINT_SINGLE' if len(old_paths) == 1 else 'COLLAPSE_ALIAS_SET'
        deterministic_digests += 1
        deterministic_stale_path_occurrences += len(stale_old_paths)
        if rec.get('collection') != derived['scope']:
            collection_move_chunk_total += len(chunk_ids)
        transition_rows.append({
            'digest': digest,
            'old_paths': old_paths,
            'stale_old_paths': stale_old_paths,
            'live_old_paths': live_old_paths,
            'canonical_live_path': canonical,
            'previous_collection': rec.get('collection'),
            'derived_collection': derived['scope'],
            'chunk_count': len(chunk_ids),
            'change': rec.get('collection') != derived['scope'],
            'reason_rule': derived['rule'],
            'reason_matched': derived['matched'],
            'legitimacy_note': 'ME-C path-hint reclassification' if derived['scope']=='me-c' else 'remains maker-manuals under 00_Career/03_Engine_Knowledge prefix',
        })
        operations.append({
            'op_type': op_type,
            'digest': digest,
            'old_paths': old_paths,
            'stale_old_paths': stale_old_paths,
            'live_old_paths': live_old_paths,
            'canonical_live_path': canonical,
            'verified_live_sha256': actual_sha,
            'tracker_collection': rec.get('collection'),
            'derived_collection': derived['scope'],
            'canonical_assignment': derived,
            'chunk_ids': chunk_ids,
            'expected_chunk_count': len(chunk_ids),
            'current_chunk_state': current_state,
            'target_path_conflict_digests': target_conflicts,
            'exact_live_matches': matches,
        })

    manifest = {
        'schema_version': 'orphan-repair-manifest-v2',
        'production': {
            'library_root': str(PROD_LIBRARY),
            'generation_dir': str(PROD_GEN),
            'collection_name': collection_name,
        },
        'counts': {
            'deterministic_stale_path_occurrences': deterministic_stale_path_occurrences,
            'total_orphan_path_occurrences': len(orphan_occurrences),
            'deterministic_digests': deterministic_digests,
            'retirement_digests': retirement_digests,
            'affected_digests_total': len(operations),
        },
        'failed_case_controls': FAILED_CASES,
        'orphan_occurrences': [
            {'digest': d, 'path': p} for d, p in sorted(orphan_occurrences, key=lambda x: (x[0], x[1]))
        ],
        'operations': operations,
    }
    manifest['manifest_payload_sha256'] = hashlib.sha256(manifest_payload(manifest)).hexdigest()
    bad = validate_no_placeholders(manifest)
    if bad:
        raise RuntimeError(f'placeholder content found: {bad[:10]}')
    if deterministic_stale_path_occurrences != 54 or len(orphan_occurrences) != 55 or deterministic_digests != 40 or retirement_digests != 1 or len(operations) != 41:
        raise RuntimeError('unexpected manifest counts')

    baseline = {
        'doctor': doctor_json(PROD_GEN),
        'scope_stats': scope_stats_json(PROD_GEN),
        'actual_total_chunk_count_sqlite': count_embeddings(PROD_GEN / 'chroma.sqlite3'),
        'orphan_count': parse_orphan_count(doctor_json(PROD_GEN)),
        'file_hashes': tree_hashes(PROD_GEN),
    }

    transitions = {
        'generated_from_manifest_sha256': manifest['manifest_payload_sha256'],
        'rows': transition_rows,
        'summary': {
            'deterministic_digest_count': len(transition_rows),
            'collection_change_digest_count': sum(1 for r in transition_rows if r['change']),
            'collection_change_chunk_total': collection_move_chunk_total,
            'maker_manuals_to_me_c_chunk_total': sum(r['chunk_count'] for r in transition_rows if r['previous_collection']=='maker-manuals' and r['derived_collection']=='me-c'),
            'maker_manuals_stays_chunk_total': sum(r['chunk_count'] for r in transition_rows if r['previous_collection']=='maker-manuals' and r['derived_collection']=='maker-manuals'),
            'retirement_chunk_total': len(next(op for op in operations if op['op_type']=='RETIRE_UNRECOVERABLE')['chunk_ids']),
        },
    }
    if transitions['summary']['maker_manuals_to_me_c_chunk_total'] != 444:
        raise RuntimeError('expected 444 moved chunks')
    if transitions['summary']['collection_change_chunk_total'] != 444:
        raise RuntimeError('expected 444 collection-change chunks')

    acceptance = {
        'positive_controls': [
            {
                'name': 'me_lgip_renamed_paper',
                'scope': 'maker-manuals',
                'query': 'Show the source document for service experience for Everllence B&W ME-LGIP engines',
                'expected_top_path': '00_Career/03_Engine_Knowledge/MAN_Technical_Papers/Service_Experience/Service_Experience_Everllence_BW_ME-LGIP_Engines_5510-0283-00.pdf',
            }
        ],
        'negative_controls': [
            {
                'name': 'retired_c1210_absent',
                'scope': 'maker-manuals',
                'query': 'C1210 ME Pre-Dual Fuel standard operation online timetable',
                'expected_status': 'no_coverage',
            }
        ],
        'no_regression_controls': FAILED_CASES,
    }

    write_json_atomic(MANIFEST_PATH, manifest)
    write_json_atomic(BASELINE_PATH, baseline)
    write_json_atomic(TRANSITIONS_PATH, transitions)
    write_json_atomic(ACCEPTANCE_PATH, acceptance)
    write_json_atomic(PKG / 'artifact_inventory.partial.json', artifact_inventory(PKG))


if __name__ == '__main__':
    build()

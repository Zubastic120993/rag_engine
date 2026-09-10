#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path('/Users/vladymyrzub/CE_Library/Tools/rag_engine')
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import chromadb  # type: ignore
from langchain_community.embeddings import OllamaEmbeddings  # type: ignore
from langchain_chroma import Chroma  # type: ignore
from rag_engine.config import chroma_client_settings, embed_model  # type: ignore
from rag_engine.query import answer, retrieve_with_scores_and_diagnostics  # type: ignore

from orphan_repair_common import (
    PROD_LIBRARY,
    TargetRefusal,
    canonical_json_bytes,
    chunk_rows_by_source_hash,
    embedding_digest_from_payload,
    env_for,
    tracker_load,
    validate_disposable_clone_marker,
)


def load_request(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def respond(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def jsonable_embedding(value: Any) -> Any:
    if hasattr(value, 'tolist'):
        return value.tolist()
    return value


def fetch_chunk_payload(coll: Any, chunk_ids: list[str], include_embeddings: bool = False) -> dict[str, Any]:
    include = ['metadatas', 'documents']
    if include_embeddings:
        include.append('embeddings')
    raw = coll.get(ids=chunk_ids, include=include)
    ids_raw = raw.get('ids')
    metas_raw = raw.get('metadatas')
    docs_raw = raw.get('documents')
    embs_raw = raw.get('embeddings') if include_embeddings else None
    ids = list(ids_raw) if ids_raw is not None else []
    metas = list(metas_raw) if metas_raw is not None else []
    docs = list(docs_raw) if docs_raw is not None else []
    embs = list(embs_raw) if embs_raw is not None else []
    out: dict[str, Any] = {}
    for i, cid in enumerate(ids):
        entry: dict[str, Any] = {
            'metadata': dict(metas[i] or {}),
            'document': docs[i] if i < len(docs) else None,
        }
        if include_embeddings:
            entry['embedding'] = jsonable_embedding(embs[i]) if i < len(embs) else None
        out[str(cid)] = entry
    return out


def payload_digest(payload: dict[str, Any], chunk_ids: list[str], field: str) -> str:
    rows = [{'chunk_id': cid, field: payload[cid].get(field)} for cid in chunk_ids]
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


def payload_metadata_digest(payload: dict[str, Any], chunk_ids: list[str], exclude_keys: set[str] | None = None) -> str:
    exclude_keys = exclude_keys or set()
    rows = []
    for cid in chunk_ids:
        meta = dict(payload[cid].get('metadata') or {})
        for key in exclude_keys:
            meta.pop(key, None)
        rows.append({'chunk_id': cid, 'metadata': meta})
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def maybe_emit_lc6_case20_malformed_output(args: argparse.Namespace, request: dict[str, Any], gen_dir: Path, marker: dict[str, Any]) -> None:
    ctx = request.get('lc6_case20_malformed_output') if isinstance(request, dict) else None
    if not isinstance(ctx, dict):
        return
    expected_case = 'LC6-20-worker-timeout-or-malformed-output'
    if args.action != 'apply_manifest_ops':
        raise RuntimeError('LC6-20 malformed-output injection refused: worker action mismatch')
    if args.case_id != expected_case or ctx.get('case_id') != expected_case:
        raise RuntimeError('LC6-20 malformed-output injection refused: case_id mismatch')
    if not args.run_id or ctx.get('run_id') != args.run_id:
        raise RuntimeError('LC6-20 malformed-output injection refused: run_id mismatch')
    if Path(str(ctx.get('clone_path', ''))).resolve() != gen_dir.resolve():
        raise RuntimeError('LC6-20 malformed-output injection refused: clone_path mismatch')
    if ctx.get('expected_worker_action') != args.action:
        raise RuntimeError('LC6-20 malformed-output injection refused: expected worker action mismatch')
    if not marker.get('disposable') or not marker.get('unpromoted'):
        raise RuntimeError('LC6-20 malformed-output injection refused: target is not disposable/unpromoted')
    state_dir = Path(str(ctx.get('state_dir', ''))).resolve()
    if not state_dir or gen_dir.parent.parent.resolve() not in state_dir.parents:
        raise RuntimeError('LC6-20 malformed-output injection refused: state_dir outside disposable case root')
    state_dir.mkdir(parents=True, exist_ok=True)
    write_json(state_dir / 'worker_malformed_output_emitted.json', {
        'case_id': expected_case,
        'run_id': args.run_id,
        'clone_path': str(gen_dir.resolve()),
        'worker_pid': os.getpid(),
        'worker_script': str(Path(__file__).resolve()),
        'worker_action': args.action,
        'marker_clone_id': marker.get('clone_id'),
        'opened_chroma': False,
        'malformed_output_emitted': True,
    })
    print('{lc6_case20_malformed_worker_output:')
    raise SystemExit(0)


def load_interrupt_context(request: dict[str, Any], gen_dir: Path) -> dict[str, Any]:
    ctx = dict(request.get('interrupt_context') or {})
    required = ['state_dir', 'case_id', 'run_id', 'clone_path']
    missing = [key for key in required if not ctx.get(key)]
    if missing:
        raise RuntimeError(f'interrupt_context missing fields: {missing}')
    clone_path = Path(str(ctx['clone_path'])).resolve()
    if clone_path != gen_dir.resolve():
        raise RuntimeError('interrupt_context clone_path mismatch')
    state_dir = Path(str(ctx['state_dir'])).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    ctx['clone_path'] = str(clone_path)
    ctx['state_dir'] = str(state_dir)
    return ctx


def emit_interrupt_checkpoint_and_wait(ctx: dict[str, Any], gen_dir: Path) -> None:
    state_dir = Path(ctx['state_dir'])
    checkpoint_path = state_dir / 'mutation_checkpoint.json'
    signal_path = state_dir / 'signal_received.json'
    command = ' '.join(sys.argv)

    def on_signal(signum, _frame):
        write_json(signal_path, {
            'status': 'SIGNAL_RECEIVED',
            'signal': signum,
            'case_id': ctx['case_id'],
            'run_id': ctx['run_id'],
            'clone_path': ctx['clone_path'],
            'worker_pid': os.getpid(),
            'worker_script': str(Path(__file__).resolve()),
            'worker_command': command,
            'wait_completed': True,
            'timestamp_monotonic': time.monotonic(),
        })
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    write_json(checkpoint_path, {
        'status': 'MUTATION_IN_PROGRESS',
        'checkpoint_name': 'mutation_in_progress',
        'case_id': ctx['case_id'],
        'run_id': ctx['run_id'],
        'clone_path': ctx['clone_path'],
        'worker_pid': os.getpid(),
        'collection_name': ctx.get('collection_name'),
        'selected_chunk_id': ctx.get('selected_chunk_id'),
        'original_metadata': ctx.get('original_metadata'),
        'worker_script': str(Path(__file__).resolve()),
        'worker_command': command,
        'timestamp_monotonic': time.monotonic(),
        'opened_chroma': True,
    })
    signal.pause()


def open_client_collection(gen_dir: Path):
    client = chromadb.PersistentClient(path=str(gen_dir), settings=chroma_client_settings())
    cols = client.list_collections()
    if len(cols) != 1:
        raise RuntimeError(f'expected one collection, found {[c.name for c in cols]}')
    coll = client.get_collection(cols[0].name)
    return client, coll, cols[0].name


def langchain_db(gen_dir: Path):
    embeddings = OllamaEmbeddings(model=embed_model())
    return Chroma(persist_directory=str(gen_dir), embedding_function=embeddings, client_settings=chroma_client_settings())


def action_seed_test_collection(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    client = chromadb.PersistentClient(path=str(gen_dir), settings=chroma_client_settings())
    coll = client.get_or_create_collection(request.get('collection_name', 'test'))
    coll.add(
        ids=[request.get('id', 'a')],
        documents=[request.get('document', 'alpha')],
        metadatas=[request.get('metadata', {'source': 'x', 'collection': 't'})],
        embeddings=[request.get('embedding', [0.1, 0.2, 0.3])],
    )
    return {'status': 'ok', 'action': 'seed_test_collection', 'opened_chroma': True, 'collection_name': request.get('collection_name', 'test')}


def action_inspect(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    _client, coll, collection_name = open_client_collection(gen_dir)
    ids = request.get('ids') or []
    payload = fetch_chunk_payload(coll, list(ids), include_embeddings=bool(request.get('include_embeddings', False))) if ids else {}
    return {
        'status': 'ok',
        'action': 'inspect',
        'opened_chroma': True,
        'collection_name': collection_name,
        'payload': payload,
    }


def action_mutate_metadata(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    _client, coll, collection_name = open_client_collection(gen_dir)
    ids = list(request['ids'])
    metadatas = list(request['metadatas'])
    coll.update(ids=ids, metadatas=metadatas)
    return {'status': 'ok', 'action': 'mutate_metadata', 'opened_chroma': True, 'collection_name': collection_name, 'updated_ids': ids}


def action_apply_manifest_ops(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    tracker = tracker_load(gen_dir)
    ops = list(request['operations'])
    inject = request.get('inject')
    _client, coll, collection_name = open_client_collection(gen_dir)
    deterministic_seen = 0
    alias_seen = 0
    retired = False
    for op in ops:
        digest = op['digest']
        chunk_ids = list(op['chunk_ids'])
        if op['op_type'] != 'RETIRE_UNRECOVERABLE':
            deterministic_seen += 1
            if inject == 'failure_halfway_through_deterministic' and deterministic_seen == 20:
                raise RuntimeError('injected failure halfway through deterministic repoints')
            if op['op_type'] == 'COLLAPSE_ALIAS_SET':
                alias_seen += 1
                if inject == 'failure_during_alias_collapse' and alias_seen == 1:
                    raise RuntimeError('injected failure during alias collapse')
            state = fetch_chunk_payload(coll, chunk_ids, include_embeddings=False)
            new_metas = []
            for cid in chunk_ids:
                meta = dict(state[cid]['metadata'])
                meta['source'] = op['canonical_live_path']
                meta['collection'] = op['derived_collection']
                new_metas.append(meta)
            coll.update(ids=chunk_ids, metadatas=new_metas)
            tracker[digest]['paths'] = [op['canonical_live_path']]
            tracker[digest]['collection'] = op['derived_collection']
        else:
            if inject == 'failure_before_retirement':
                raise RuntimeError('injected failure before retirement')
            coll.delete(ids=chunk_ids)
            tracker.pop(digest, None)
            retired = True
            if inject == 'failure_after_retirement':
                raise RuntimeError('injected failure after retirement')
    return {
        'status': 'ok',
        'action': 'apply_manifest_ops',
        'opened_chroma': True,
        'collection_name': collection_name,
        'tracker_after': tracker,
        'retired': retired,
    }


def action_case11_interrupt_probe(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    ctx = load_interrupt_context(request, gen_dir)
    _client, coll, collection_name = open_client_collection(gen_dir)
    expected_collection = request.get('expected_collection_name')
    if expected_collection and str(expected_collection) != collection_name:
        raise RuntimeError(f'collection name mismatch: {collection_name} != {expected_collection}')
    chunk_id = str(request['selected_chunk_id'])
    payload = fetch_chunk_payload(coll, [chunk_id], include_embeddings=False)
    if chunk_id not in payload:
        raise RuntimeError(f'interrupt probe chunk missing: {chunk_id}')
    original_meta = dict(payload[chunk_id]['metadata'] or {})
    meta = dict(original_meta)
    meta['_lc6_interrupt_probe_case_id'] = ctx['case_id']
    meta['_lc6_interrupt_probe_run_id'] = ctx['run_id']
    coll.update(ids=[chunk_id], metadatas=[meta])
    ctx['selected_chunk_id'] = chunk_id
    ctx['collection_name'] = collection_name
    ctx['original_metadata'] = original_meta
    emit_interrupt_checkpoint_and_wait(ctx, gen_dir)
    return {
        'status': 'ok',
        'action': 'case11_interrupt_probe',
        'opened_chroma': True,
        'collection_name': collection_name,
        'updated_id': chunk_id,
    }


def action_collection_summary(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    client = chromadb.PersistentClient(path=str(gen_dir), settings=chroma_client_settings())
    cols = client.list_collections()
    names = sorted(c.name for c in cols)
    selected_chunk_id = request.get('selected_chunk_id')
    selected_chunk_exists = None
    selected_chunk_metadata = None
    if len(cols) == 1 and selected_chunk_id:
        coll = client.get_collection(cols[0].name)
        payload = fetch_chunk_payload(coll, [str(selected_chunk_id)], include_embeddings=False)
        if str(selected_chunk_id) in payload:
            selected_chunk_exists = True
            selected_chunk_metadata = payload[str(selected_chunk_id)].get('metadata')
        else:
            selected_chunk_exists = False
    return {
        'status': 'ok',
        'action': 'collection_summary',
        'opened_chroma': True,
        'collection_names': names,
        'collection_count': len(names),
        'selected_chunk_id': selected_chunk_id,
        'selected_chunk_exists': selected_chunk_exists,
        'selected_chunk_metadata': selected_chunk_metadata,
    }


def action_source_hash_rows(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    rows = chunk_rows_by_source_hash(gen_dir / 'chroma.sqlite3', str(request['digest']))
    return {'status': 'ok', 'action': 'source_hash_rows', 'opened_chroma': False, 'chunk_ids': rows}


def action_validate_manifest_chunks(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    _client, coll, collection_name = open_client_collection(gen_dir)
    operations = list(request['operations'])
    rows: list[dict[str, Any]] = []
    for op in operations:
        if op['op_type'] == 'RETIRE_UNRECOVERABLE':
            remaining = chunk_rows_by_source_hash(gen_dir / 'chroma.sqlite3', op['digest'])
            rows.append({'digest': op['digest'], 'retired': True, 'remaining_chunk_ids': remaining})
            continue
        payload = fetch_chunk_payload(coll, list(op['chunk_ids']), include_embeddings=bool(request.get('include_embeddings', False)))
        first = payload[op['chunk_ids'][0]] if op['chunk_ids'] else None
        rows.append({
            'digest': op['digest'],
            'chunk_count': len(op['chunk_ids']),
            'chunk_ids': list(op['chunk_ids']),
            'canonical_live_path': op['canonical_live_path'],
            'derived_collection': op['derived_collection'],
            'first_chunk': first,
            'document_digest': payload_digest(payload, list(op['chunk_ids']), 'document'),
            'metadata_digest': payload_metadata_digest(payload, list(op['chunk_ids'])),
            'metadata_digest_excluding_source_collection': payload_metadata_digest(payload, list(op['chunk_ids']), exclude_keys={'source', 'collection'}),
            'sources': sorted({str((payload[cid].get('metadata') or {}).get('source')) for cid in op['chunk_ids']}),
            'collections': sorted({str((payload[cid].get('metadata') or {}).get('collection')) for cid in op['chunk_ids']}),
            'source_hashes': sorted({str((payload[cid].get('metadata') or {}).get('source_hash')) for cid in op['chunk_ids']}),
            'embedding_digest': embedding_digest_from_payload(payload, list(op['chunk_ids'])) if request.get('include_embeddings', False) else None,
        })
    return {'status': 'ok', 'action': 'validate_manifest_chunks', 'opened_chroma': True, 'collection_name': collection_name, 'rows': rows}


def action_similarity_pairs(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    db = langchain_db(gen_dir)
    kwargs: dict[str, Any] = {'k': int(request.get('k', 8))}
    if request.get('scope'):
        kwargs['filter'] = {'collection': request['scope']}
    raw = db.similarity_search_with_score(str(request['query']), **kwargs)
    pairs = []
    for doc, dist in raw:
        meta = dict(doc.metadata or {})
        pairs.append({
            'source': meta.get('source'),
            'source_hash': meta.get('source_hash'),
            'collection': meta.get('collection'),
            'document_type': meta.get('document_type'),
            'authority_rank': meta.get('authority_rank'),
            'page': meta.get('page'),
            'chunk_id': meta.get('chunk_id'),
            'distance': float(dist),
            'text_head': (doc.page_content or '')[:240],
        })
    return {'status': 'ok', 'action': 'similarity_pairs', 'opened_chroma': True, 'pairs': pairs}


def action_answer_diagnostics(gen_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(env_for(gen_dir, PROD_LIBRARY))
    try:
        pairs, diag = retrieve_with_scores_and_diagnostics(str(request['query']), scope=str(request['scope']), k=int(request.get('k', 5)))
        result = answer(str(request['query']), scope=str(request['scope']), k=int(request.get('k', 5)))
    finally:
        os.environ.clear()
        os.environ.update(old)
    top = []
    for doc, dist in pairs:
        meta = dict(doc.metadata or {})
        top.append({
            'source': meta.get('source'),
            'source_hash': meta.get('source_hash'),
            'collection': meta.get('collection'),
            'document_type': meta.get('document_type'),
            'authority_rank': meta.get('authority_rank'),
            'page': meta.get('page'),
            'chunk_id': meta.get('chunk_id'),
            'distance': float(dist),
            'text_head': (doc.page_content or '')[:240],
        })
    return {
        'status': 'ok',
        'action': 'answer_diagnostics',
        'opened_chroma': True,
        'answer': {
            'status': result.status,
            'gate': result.gate,
            'best_distance': result.best_distance,
            'score_floor': result.score_floor,
            'resolved_scope': result.resolved_scope,
            'pairs': top,
            'diagnostics': diag,
            'sources': result.sources,
        },
    }


ACTIONS = {
    'seed_test_collection': action_seed_test_collection,
    'inspect': action_inspect,
    'mutate_metadata': action_mutate_metadata,
    'apply_manifest_ops': action_apply_manifest_ops,
    'case11_interrupt_probe': action_case11_interrupt_probe,
    'collection_summary': action_collection_summary,
    'source_hash_rows': action_source_hash_rows,
    'validate_manifest_chunks': action_validate_manifest_chunks,
    'similarity_pairs': action_similarity_pairs,
    'answer_diagnostics': action_answer_diagnostics,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=sorted(ACTIONS))
    ap.add_argument('--gen-dir', required=True)
    ap.add_argument('--request-file', required=True)
    ap.add_argument('--case-id')
    ap.add_argument('--run-id')
    ap.add_argument('--state-dir')
    args = ap.parse_args()

    gen_dir = Path(args.gen_dir).expanduser().resolve()
    request = load_request(Path(args.request_file))

    try:
        marker = validate_disposable_clone_marker(gen_dir)
        maybe_emit_lc6_case20_malformed_output(args, request, gen_dir, marker)
        handler = ACTIONS[args.action]
        payload = handler(gen_dir, request)
        payload['worker_pid'] = os.getpid()
        payload['generation'] = str(gen_dir)
        payload['marker_clone_id'] = marker['clone_id']
        respond(payload)
    except TargetRefusal as exc:
        respond({'status': 'refused', 'opened_chroma': False, 'reason': str(exc), 'generation': str(gen_dir), 'worker_pid': os.getpid()})
        raise SystemExit(3)
    except Exception as exc:
        respond({'status': 'error', 'opened_chroma': False, 'error_type': type(exc).__name__, 'error': str(exc), 'generation': str(gen_dir), 'worker_pid': os.getpid()})
        raise SystemExit(1)


if __name__ == '__main__':
    main()

"""Deterministic source-path reconciliation for tracker + Chroma metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import chromadb

from rag_engine.config import (
    chroma_client_settings,
    collection_from_relpath,
    library_root,
    persist_dir,
    track_file,
)
from rag_engine.diagnostics import scope_stats
from rag_engine.doctor import run_doctor
from rag_engine.lock import IngestLockError, ingest_lock

STATUS_DRY_RUN_OK = "DRY_RUN_OK"
STATUS_COMMITTED_AND_VERIFIED = "COMMITTED_AND_VERIFIED"
STATUS_REFUSED = "REFUSED"
STATUS_FAILED_ROLLED_BACK = "FAILED_ROLLED_BACK"
STATUS_FAILED_ROLLBACK_INCOMPLETE = "FAILED_ROLLBACK_INCOMPLETE"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 3
EXIT_FAILED_ROLLED_BACK = 4
EXIT_FAILED_ROLLBACK_INCOMPLETE = 5

ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "maintenance_artifacts"


class ReconcileRefusal(RuntimeError):
    """Validation refusal for an unsafe or ambiguous reconciliation request."""


class ReconcileFailure(RuntimeError):
    """Failure after validation, possibly requiring rollback."""


class RollbackFailure(RuntimeError):
    """Rollback did not fully restore pre-commit state."""


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _json_digest(obj: Any) -> str:
    payload = json.dumps(_jsonable(obj), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _validate_rel_path(raw: str, *, field: str) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        raise ReconcileRefusal(f"{field} is required")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("/"):
        raise ReconcileRefusal(f"{field} must be a relative CE_Library path")
    if value.startswith("./"):
        raise ReconcileRefusal(f"{field} must not start with ./")
    if ".." in pure.parts:
        raise ReconcileRefusal(f"{field} must not contain ..")
    normalized = "/".join(pure.parts)
    if normalized != value:
        raise ReconcileRefusal(f"{field} must be normalized and exact")
    return value


def _load_tracker_strict() -> dict[str, Any]:
    tf = track_file()
    if not tf.exists():
        raise ReconcileRefusal(f"tracker missing: {tf}")
    try:
        data = json.loads(tf.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReconcileRefusal(f"malformed tracker JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReconcileRefusal("tracker root is not a JSON object")
    return data


def _write_tracker_atomic(tracker: dict[str, Any]) -> None:
    tf = track_file()
    tf.parent.mkdir(parents=True, exist_ok=True)
    tmp = tf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tracker, indent=2), encoding="utf-8")
    tmp.replace(tf)


def _tracker_entries_for_path(tracker: dict[str, Any], rel_path: str) -> list[tuple[str, dict[str, Any]]]:
    matches: list[tuple[str, dict[str, Any]]] = []
    for digest, meta in tracker.items():
        if not isinstance(meta, dict):
            continue
        paths = meta.get("paths") or []
        count = sum(1 for p in paths if str(p).replace("\\", "/") == rel_path)
        if count > 1:
            raise ReconcileRefusal(f"tracker path appears more than once inside digest {digest}: {rel_path}")
        if count == 1:
            matches.append((digest, meta))
    return matches


def _open_collection():
    client = chromadb.PersistentClient(path=str(persist_dir()), settings=chroma_client_settings())
    cols = client.list_collections()
    if not cols:
        raise ReconcileRefusal("Chroma has no collections")
    if len(cols) != 1:
        raise ReconcileRefusal(f"expected exactly one Chroma collection, found {[c.name for c in cols]}")
    return client, client.get_collection(cols[0].name), cols[0].name


def _source_records(collection, source_rel: str) -> dict[str, Any]:
    return collection.get(where={"source": source_rel}, include=["metadatas"])


def _ids_for_source(collection, source_rel: str) -> list[str]:
    raw = _source_records(collection, source_rel)
    return list(raw.get("ids") or [])


def _embedding_digest_for_ids(collection, ids: list[str]) -> str | None:
    if not ids:
        return None
    raw = collection.get(ids=ids, include=["embeddings"])
    embeddings = raw.get("embeddings")
    if embeddings is None:
        return None
    return _json_digest(embeddings)


def _metadata_update_preview(old_metas: list[dict[str, Any] | None], *, new_source: str, new_collection: str) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    for meta in old_metas:
        item = dict(meta or {})
        item["source"] = new_source
        item["collection"] = new_collection
        updated.append(item)
    return updated


def _hashes_for_live_state() -> dict[str, str]:
    db = persist_dir()
    paths = {
        "embedded_json": track_file(),
        "chroma_sqlite3": db / "chroma.sqlite3",
    }
    out: dict[str, str] = {}
    for key, path in paths.items():
        if not path.exists():
            raise ReconcileFailure(f"required live state file missing: {path}")
        out[key] = _file_sha256(path)
    return out


def _rollback_tracker_from_backup(backup_path: Path) -> None:
    if not backup_path.exists():
        raise RollbackFailure(f"tracker backup missing: {backup_path}")
    restored = json.loads(backup_path.read_text(encoding="utf-8"))
    _write_tracker_atomic(restored)


def inspect_reconcile_request(old_path: str, new_path: str, expected_sha256: str) -> dict[str, Any]:
    old_rel = _validate_rel_path(old_path, field="old_path")
    new_rel = _validate_rel_path(new_path, field="new_path")
    expected = str(expected_sha256 or "").strip().lower()
    if not expected:
        raise ReconcileRefusal("expected_sha256 is required")
    if old_rel == new_rel:
        raise ReconcileRefusal("old_path and new_path are identical")

    root = library_root()
    old_abs = root / old_rel
    new_abs = root / new_rel

    old_exists = old_abs.is_file()
    new_exists = new_abs.is_file()
    if old_exists:
        raise ReconcileRefusal("old_path still exists on filesystem")
    if not new_exists:
        raise ReconcileRefusal("new_path does not exist on filesystem")

    actual_sha = _file_sha256(new_abs)
    if actual_sha != expected:
        raise ReconcileRefusal(
            f"expected_sha256 mismatch: expected {expected} got {actual_sha}"
        )

    tracker = _load_tracker_strict()
    old_matches = _tracker_entries_for_path(tracker, old_rel)
    new_matches = _tracker_entries_for_path(tracker, new_rel)
    if len(old_matches) != 1:
        raise ReconcileRefusal(f"tracker old entry is ambiguous: matches={len(old_matches)}")
    if new_matches:
        raise ReconcileRefusal("tracker new path already exists and would conflict")

    digest, meta = old_matches[0]
    chunk_ids = list(meta.get("chunk_ids") or [])
    if not chunk_ids:
        raise ReconcileRefusal("tracker old entry has no chunk_ids; rollback cannot be guaranteed")

    new_collection = collection_from_relpath(new_rel)
    old_collection = str(meta.get("collection") or "")

    _, collection, collection_name = _open_collection()
    old_raw = _source_records(collection, old_rel)
    new_raw = _source_records(collection, new_rel)

    old_ids = list(old_raw.get("ids") or [])
    new_ids = list(new_raw.get("ids") or [])
    old_metas = list(old_raw.get("metadatas") or [])
    new_metas = list(new_raw.get("metadatas") or [])

    if len(old_ids) == 0:
        raise ReconcileRefusal("Chroma old source count is zero")
    if len(new_ids) > 0:
        raise ReconcileRefusal("Chroma new source already has chunks and would conflict")
    if set(chunk_ids) != set(old_ids):
        raise ReconcileRefusal("tracker chunk_ids do not exactly match Chroma old-source ids")

    for meta_item in old_metas:
        item = dict(meta_item or {})
        if item.get("source") != old_rel:
            raise ReconcileRefusal("Chroma old-source metadata contains mismatched source values")
        coll = item.get("collection")
        if coll is not None and old_collection and str(coll) != old_collection:
            raise ReconcileRefusal("Chroma old-source metadata collection conflicts with tracker collection")

    return {
        "old_path": old_rel,
        "new_path": new_rel,
        "expected_sha256": expected,
        "actual_sha256": actual_sha,
        "library_root": str(root),
        "db_path": str(persist_dir()),
        "tracker_path": str(track_file()),
        "tracker_digest": digest,
        "tracker_meta": {
            "paths": list(meta.get("paths") or []),
            "chunk_ids_count": len(chunk_ids),
            "chunk_ids_sha256": _json_digest(sorted(chunk_ids)),
            "collection": old_collection,
            "ingested_at": meta.get("ingested_at"),
        },
        "collection_transition": {
            "old_collection": old_collection,
            "new_collection": new_collection,
        },
        "chroma": {
            "collection_name": collection_name,
            "old_source_count": len(old_ids),
            "new_source_count": len(new_ids),
            "ids_sha256": _json_digest(sorted(old_ids)),
            "metadata_sha256_before": _json_digest(old_metas),
            "embedding_sha256_before": _embedding_digest_for_ids(collection, old_ids),
            "sample_metadata": old_metas[0] if old_metas else None,
        },
        "prepared_metadata_sha256_after": _json_digest(
            _metadata_update_preview(old_metas, new_source=new_rel, new_collection=new_collection)
        ),
        "validation": {
            "old_path_absent": not old_exists,
            "new_path_exists": new_exists,
            "sha256_matches": actual_sha == expected,
            "tracker_old_entry_exists": True,
            "tracker_new_entry_conflict": False,
            "chroma_old_source_count": len(old_ids),
            "chroma_new_source_count": len(new_ids),
            "tracker_ids_match_chroma_ids": True,
        },
    }


def _human_report(payload: dict[str, Any]) -> str:
    lines = [
        f"status: {payload['status']}",
        f"old_path: {payload['old_path']}",
        f"new_path: {payload['new_path']}",
    ]
    if payload.get("message"):
        lines.append(f"message: {payload['message']}")
    validation = payload.get("validation") or {}
    for key in sorted(validation):
        lines.append(f"{key}: {validation[key]}")
    return "\n".join(lines)


def _result(status: str, *, exit_code: int, old_path: str, new_path: str, expected_sha256: str, message: str | None = None, **extra: Any) -> tuple[dict[str, Any], int]:
    payload = {
        "status": status,
        "old_path": old_path,
        "new_path": new_path,
        "expected_sha256": expected_sha256,
        "message": message,
        **extra,
    }
    return payload, exit_code


def _commit_reconcile(plan: dict[str, Any]) -> tuple[dict[str, Any], int]:
    old_rel = plan["old_path"]
    new_rel = plan["new_path"]
    digest = plan["tracker_digest"]
    new_collection = plan["collection_transition"]["new_collection"]
    artifact_dir = ARTIFACTS_DIR / f"reconcile_path_{_now_stamp()}"
    artifact_dir.mkdir(parents=True, exist_ok=False)

    tracker_backup = artifact_dir / "embedded.json.backup"
    rollback_json = artifact_dir / "rollback_payload.json"
    pre_hashes_json = artifact_dir / "pre_hashes.json"
    post_hashes_json = artifact_dir / "post_hashes.json"

    chroma_updated = False
    tracker_updated = False
    rollback_complete = False
    rollback_error: str | None = None
    rollback_payload: dict[str, Any] | None = None

    with ingest_lock(timeout_s=0):
        locked_plan = inspect_reconcile_request(old_rel, new_rel, plan["expected_sha256"])
        tracker = _load_tracker_strict()
        _, collection, collection_name = _open_collection()
        old_raw = _source_records(collection, old_rel)
        old_ids = list(old_raw.get("ids") or [])
        old_metas = list(old_raw.get("metadatas") or [])
        old_embeddings_sha = _embedding_digest_for_ids(collection, old_ids)

        pre_hashes = _hashes_for_live_state()
        pre_hashes_json.write_text(json.dumps(pre_hashes, indent=2), encoding="utf-8")
        shutil.copy2(track_file(), tracker_backup)

        # Stays inside ingest_lock with the mutation and the post-read: a pre-count taken
        # outside the lock could race a concurrent ingest, making an unrelated chunk-count
        # change look like this reconcile broke something and triggering a spurious rollback.
        pre_scope_stats = scope_stats()
        if pre_scope_stats.get("error") or "total_chunks" not in pre_scope_stats:
            raise ReconcileFailure(
                "scope-stats failed before reconcile: "
                f"{pre_scope_stats.get('error', 'total_chunks missing')}"
            )
        pre_total_chunks = pre_scope_stats["total_chunks"]

        rollback_payload = {
            "created_at": _now_stamp(),
            "old_path": old_rel,
            "new_path": new_rel,
            "expected_sha256": plan["expected_sha256"],
            "tracker_digest": digest,
            "collection_name": collection_name,
            "affected_ids": old_ids,
            "affected_ids_sha256": _json_digest(sorted(old_ids)),
            "original_metadatas": old_metas,
            "original_metadata_sha256": _json_digest(old_metas),
            "original_embedding_sha256": old_embeddings_sha,
            "tracker_backup_path": str(tracker_backup),
            "pre_hashes": pre_hashes,
        }
        rollback_json.write_text(json.dumps(rollback_payload, indent=2, ensure_ascii=False), encoding="utf-8")

        try:
            new_metas = _metadata_update_preview(old_metas, new_source=new_rel, new_collection=new_collection)
            collection.update(ids=old_ids, metadatas=new_metas)
            chroma_updated = True

            verify_new = _source_records(collection, new_rel)
            verify_old = _source_records(collection, old_rel)
            verify_new_ids = list(verify_new.get("ids") or [])
            if len(verify_old.get("ids") or []) != 0:
                raise ReconcileFailure("post-update old-source Chroma count is not zero")
            if len(verify_new_ids) != len(old_ids):
                raise ReconcileFailure("post-update new-source Chroma count mismatch")
            if set(verify_new_ids) != set(old_ids):
                raise ReconcileFailure("post-update Chroma ids changed unexpectedly")
            verify_new_metas = list(verify_new.get("metadatas") or [])
            if _json_digest(verify_new_metas) != _json_digest(new_metas):
                raise ReconcileFailure("post-update Chroma metadata does not match prepared metadata")
            if _embedding_digest_for_ids(collection, old_ids) != old_embeddings_sha:
                raise ReconcileFailure("embeddings changed during metadata-only update")

            tracker[digest]["paths"] = [new_rel if p == old_rel else p for p in (tracker[digest].get("paths") or [])]
            tracker[digest]["collection"] = new_collection
            _write_tracker_atomic(tracker)
            tracker_updated = True

            tracker_verify = _load_tracker_strict()
            if _tracker_entries_for_path(tracker_verify, old_rel):
                raise ReconcileFailure("tracker still contains old_path after write")
            new_matches = _tracker_entries_for_path(tracker_verify, new_rel)
            if len(new_matches) != 1 or new_matches[0][0] != digest:
                raise ReconcileFailure("tracker new_path verification failed")
            if list(new_matches[0][1].get("chunk_ids") or []) != list(tracker[digest].get("chunk_ids") or []):
                raise ReconcileFailure("tracker chunk_ids changed unexpectedly")

            doctor = run_doctor()
            if doctor.get("status") != "PASS":
                raise ReconcileFailure("doctor verification failed after reconcile")
            stats = scope_stats()
            if stats.get("error"):
                raise ReconcileFailure(f"scope-stats failed after reconcile: {stats['error']}")
            if stats.get("total_chunks") != pre_total_chunks:
                raise ReconcileFailure(
                    "scope-stats total_chunks changed unexpectedly: "
                    f"pre={pre_total_chunks} post={stats.get('total_chunks')}"
                )

            post_hashes = _hashes_for_live_state()
            post_hashes_json.write_text(json.dumps(post_hashes, indent=2), encoding="utf-8")

            payload, code = _result(
                STATUS_COMMITTED_AND_VERIFIED,
                exit_code=EXIT_OK,
                old_path=old_rel,
                new_path=new_rel,
                expected_sha256=plan["expected_sha256"],
                validation=locked_plan["validation"],
                tracker_before=plan["tracker_meta"],
                tracker_after={
                    "digest": digest,
                    "paths": [new_rel],
                    "collection": new_collection,
                    "chunk_ids_count": len(old_ids),
                    "chunk_ids_sha256": _json_digest(sorted(old_ids)),
                },
                chroma_before=plan["chroma"],
                chroma_after={
                    "collection_name": collection_name,
                    "old_source_count": len(_ids_for_source(collection, old_rel)),
                    "new_source_count": len(_ids_for_source(collection, new_rel)),
                    "ids_sha256": _json_digest(sorted(old_ids)),
                    "metadata_sha256_after": _json_digest(verify_new_metas),
                    "embedding_sha256_after": _embedding_digest_for_ids(collection, old_ids),
                },
                doctor=doctor,
                scope_stats=stats,
                artifacts={
                    "artifact_dir": str(artifact_dir),
                    "tracker_backup": str(tracker_backup),
                    "rollback_json": str(rollback_json),
                    "pre_hashes_json": str(pre_hashes_json),
                    "post_hashes_json": str(post_hashes_json),
                },
                pre_hashes=pre_hashes,
                post_hashes=post_hashes,
            )
            return payload, code
        except Exception as exc:
            rollback_messages: list[str] = []
            try:
                if chroma_updated and rollback_payload is not None:
                    collection.update(
                        ids=list(rollback_payload["affected_ids"]),
                        metadatas=list(rollback_payload["original_metadatas"]),
                    )
                    rollback_messages.append("chroma metadata restored")
                if tracker_updated or track_file().read_text(encoding="utf-8") != tracker_backup.read_text(encoding="utf-8"):
                    _rollback_tracker_from_backup(tracker_backup)
                    rollback_messages.append("tracker restored from backup")
                rollback_complete = True
            except Exception as rb_exc:  # noqa: BLE001
                rollback_error = str(rb_exc)

            status = STATUS_FAILED_ROLLBACK_INCOMPLETE if not rollback_complete else STATUS_FAILED_ROLLED_BACK
            exit_code = EXIT_FAILED_ROLLBACK_INCOMPLETE if not rollback_complete else EXIT_FAILED_ROLLED_BACK
            payload, code = _result(
                status,
                exit_code=exit_code,
                old_path=old_rel,
                new_path=new_rel,
                expected_sha256=plan["expected_sha256"],
                message=str(exc),
                rollback={
                    "complete": rollback_complete,
                    "steps": rollback_messages,
                    "error": rollback_error,
                },
                artifacts={
                    "artifact_dir": str(artifact_dir),
                    "tracker_backup": str(tracker_backup),
                    "rollback_json": str(rollback_json),
                    "pre_hashes_json": str(pre_hashes_json),
                },
            )
            return payload, code


def run_reconcile_path(old_path: str, new_path: str, expected_sha256: str, *, commit: bool = False) -> tuple[dict[str, Any], int]:
    try:
        plan = inspect_reconcile_request(old_path, new_path, expected_sha256)
    except ReconcileRefusal as exc:
        return _result(
            STATUS_REFUSED,
            exit_code=EXIT_REFUSED,
            old_path=str(old_path),
            new_path=str(new_path),
            expected_sha256=str(expected_sha256),
            message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return _result(
            STATUS_REFUSED,
            exit_code=EXIT_ERROR,
            old_path=str(old_path),
            new_path=str(new_path),
            expected_sha256=str(expected_sha256),
            message=str(exc),
        )

    if not commit:
        payload, code = _result(
            STATUS_DRY_RUN_OK,
            exit_code=EXIT_OK,
            old_path=plan["old_path"],
            new_path=plan["new_path"],
            expected_sha256=plan["expected_sha256"],
            validation=plan["validation"],
            tracker=plan["tracker_meta"],
            chroma=plan["chroma"],
            collection_transition=plan["collection_transition"],
            prepared_metadata_sha256_after=plan["prepared_metadata_sha256_after"],
            artifact_dir=str(ARTIFACTS_DIR / "reconcile_path_<timestamp>"),
        )
        return payload, code

    try:
        return _commit_reconcile(plan)
    except IngestLockError as exc:
        return _result(
            STATUS_REFUSED,
            exit_code=EXIT_REFUSED,
            old_path=plan["old_path"],
            new_path=plan["new_path"],
            expected_sha256=plan["expected_sha256"],
            message=str(exc),
        )


def cmd_reconcile_path(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rag-engine reconcile-path")
    parser.add_argument("--old-path", required=True)
    parser.add_argument("--new-path", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    payload, code = run_reconcile_path(
        args.old_path,
        args.new_path,
        args.expected_sha256,
        commit=args.commit,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(_human_report(payload))
    return code

"""Read-only CE Library state resolver (Phase 1).

Proposes an operation plan. Never mutates filesystem, registry, tracker,
Chroma, locks, or journals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from rag_engine.config import should_skip_dir
from rag_engine.library_state.contract import (
    ABSENCE_CLASS_BY_INTENT,
    ABSENCE_OPERATION_BY_INTENT,
    APPROVAL_BLOCKED,
    APPROVAL_NOT_REQUIRED,
    APPROVAL_REQUIRED,
    APPROVAL_SEPARATE_INDEX_APPROVAL,
    CLASS_ALIAS_ONLY,
    CLASS_AMBIGUOUS,
    CLASS_DIFFERENT_REVISION,
    CLASS_EXACT_DUPLICATE,
    CLASS_INDEXED_OK,
    CLASS_NEW_DOCUMENT,
    CLASS_NOT_INDEXED,
    CLASS_SAME_BYTES_MOVED,
    CLASS_STALE_METADATA,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    EMBEDDING_NONE,
    EMBEDDING_PENDING_SEPARATE_APPROVAL,
    GAP_REGISTRY_VECTOR_MAP_ABSENT,
    INTENT_CHECK,
    INTENT_MATRIX,
    INTENT_PLAN_ADD,
    INTENT_PLAN_DELETE,
    INTENT_VERIFY,
    OP_ALIAS_REGISTER,
    OP_CERTIFIED_APPEND_PROPOSAL,
    OP_MANUAL_REVIEW,
    OP_METADATA_ONLY_RECONCILE,
    OP_NO_OP,
    OP_RETIREMENT_PROPOSAL,
    RESULT_AMBIGUOUS,
    RESULT_BLOCKED,
    RESULT_FAILED,
    RESULT_PARTIAL,
    RESULT_VERIFIED,
    SUPPORTED_INTENTS,
)
from rag_engine.library_state.evidence import (
    EvidenceBundle,
    FilesystemObservation,
    RegistryIdentity,
    chroma_union_query_ids,
    gather_store_handles,
    id_pairs_disagree,
    load_optional_chroma,
    load_optional_tracker,
    load_registry_identity,
    maybe_evaluate_generation,
    note_vector_map_gap,
    observe_file,
    open_optional_registry,
    pairwise_id_comparison,
    read_journal_if_exactly_located,
)
from rag_engine.library_state.plan import (
    OperationPlan,
    TargetClassification,
    make_request_id,
)
from rag_engine.scope_rules import explain_path_assignment
from rag_engine.stable_identity import PathNormalizationError, normalize_relative_path
from rag_engine.reconciliation.models import TrackerRecord


def _normalize_target(raw: str, library_root: Path) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        raise ValueError("target path is required")
    try:
        return normalize_relative_path(value, library_root=library_root)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid target path {raw!r}: {exc}") from exc


def _is_under(child: str, parent: str) -> bool:
    if child == parent:
        return True
    prefix = parent.rstrip("/") + "/"
    return child.startswith(prefix)


def _list_folder_files(folder_rel: str, library_root: Path) -> list[str]:
    root = library_root / folder_rel
    if not root.is_dir():
        return []
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(library_root).as_posix()
        if should_skip_dir(str(path.parent)):
            continue
        out.append(rel)
    return out


def _scope_for(rel: str) -> str | None:
    try:
        return str(explain_path_assignment(rel).get("scope") or "") or None
    except Exception:  # noqa: BLE001 " scope is informational
        return None


def _tracker_paths(rec: TrackerRecord | None) -> tuple[str, ...]:
    if rec is None:
        return ()
    return tuple(str(p).replace("\\", "/") for p in rec.source_paths)


def _index_tracker_by_path(records: dict[str, TrackerRecord]) -> dict[str, list[str]]:
    by_path: dict[str, list[str]] = {}
    for digest, rec in records.items():
        for rel in _tracker_paths(rec):
            by_path.setdefault(rel, []).append(digest)
    for rel in by_path:
        by_path[rel] = sorted(set(by_path[rel]))
    return by_path


def _observe(
    bundle: EvidenceBundle,
    rel: str,
    *,
    library_root: Path,
    hash_if_exists: bool,
) -> FilesystemObservation:
    if rel in bundle.filesystem:
        existing = bundle.filesystem[rel]
        if hash_if_exists and existing.exists and not existing.hashed:
            obs = observe_file(rel, library_root=library_root, hash_if_exists=True)
            bundle.filesystem[rel] = obs
            if obs.hashed and rel not in bundle.hashed_paths:
                bundle.hashed_paths.append(rel)
            return obs
        return existing
    obs = observe_file(rel, library_root=library_root, hash_if_exists=hash_if_exists)
    bundle.filesystem[rel] = obs
    if rel not in bundle.inspected_paths:
        bundle.inspected_paths.append(rel)
    if obs.hashed and rel not in bundle.hashed_paths:
        bundle.hashed_paths.append(rel)
    return obs


def _approval_for(classification: str, intent: str, operation: str) -> str:
    if classification == CLASS_AMBIGUOUS or operation == OP_MANUAL_REVIEW:
        return APPROVAL_BLOCKED
    if operation == OP_CERTIFIED_APPEND_PROPOSAL:
        return APPROVAL_SEPARATE_INDEX_APPROVAL
    if operation in {OP_METADATA_ONLY_RECONCILE, OP_ALIAS_REGISTER, OP_RETIREMENT_PROPOSAL}:
        return APPROVAL_REQUIRED
    if intent in {INTENT_CHECK, INTENT_VERIFY} or operation == OP_NO_OP:
        return APPROVAL_NOT_REQUIRED
    return str(INTENT_MATRIX[intent]["default_approval"])


def _embedding_for(classification: str, operation: str) -> str:
    if operation == OP_CERTIFIED_APPEND_PROPOSAL or classification in {
        CLASS_NEW_DOCUMENT,
        CLASS_DIFFERENT_REVISION,
    }:
        return EMBEDDING_PENDING_SEPARATE_APPROVAL
    return EMBEDDING_NONE


def _verification_contract(classification: str, target: str) -> dict[str, Any]:
    if classification == CLASS_INDEXED_OK:
        return {
            "require": [
                "filesystem_hash_unchanged",
                "registry_alias_includes_target",
                "tracker_path_includes_target",
                "chroma_ids_equal_tracker_chunk_ids",
                "chunk_ids_unchanged",
                "embeddings_unchanged",
            ],
            "target": target,
        }
    if classification in {CLASS_SAME_BYTES_MOVED, CLASS_STALE_METADATA}:
        return {
            "require": [
                "registry_current_path_equals_target",
                "tracker_path_equals_target",
                "chroma_source_equals_target",
                "chunk_ids_unchanged",
                "embeddings_unchanged",
                "no_new_vectors",
            ],
            "target": target,
        }
    if classification == CLASS_ALIAS_ONLY:
        return {
            "require": ["alias_registered", "no_new_vectors", "chunk_ids_unchanged"],
            "target": target,
        }
    if classification == CLASS_EXACT_DUPLICATE:
        return {
            "require": ["duplicate_path_removed_or_retired", "retained_alias_still_live"],
            "target": target,
        }
    if classification in {CLASS_NEW_DOCUMENT, CLASS_DIFFERENT_REVISION}:
        return {
            "require": ["certified_append_only_if_separately_approved"],
            "target": target,
        }
    return {"require": ["manual_review"], "target": target}


def _summary(item: TargetClassification) -> str:
    bits = [
        f"{item.target}: {item.classification}",
        f"operation={item.proposed_operation}",
        f"embedding={item.embedding_action}",
        f"result={item.result}",
    ]
    if item.document_id:
        bits.append(f"document_id={item.document_id}")
    return "; ".join(bits)


def _classify_target(
    *,
    intent: str,
    rel: str,
    obs: FilesystemObservation,
    registry_for_hash: list[RegistryIdentity],
    registry_path_rows: list[dict[str, Any]],
    tracker_for_hash: TrackerRecord | None,
    tracker_digests_for_path: list[str],
    chroma_records_by_id: dict[str, Any],
    query_ids: tuple[str, ...],
    comparisons: dict[str, Any],
    vector_map_present: bool,
    vector_map_gap: bool,
    chroma_configured: bool,
    tracker_configured: bool,
    registry_configured: bool,
    registry_read: bool,
    filesystem_by_rel: dict[str, FilesystemObservation],
    compat_conflict: bool = False,
    compat_state: str | None = None,
    compat_reason: str | None = None,
) -> TargetClassification:
    evidence: dict[str, Any] = {
        "exists": obs.exists,
        "source_hash": obs.source_hash,
        "document_id": obs.document_id,
        "query_ids": list(query_ids),
        "id_comparisons": comparisons,
        "scope": _scope_for(rel),
    }
    subject_id = next((i.subject_id for i in registry_for_hash if i.subject_id), None)

    if len(registry_for_hash) > 1:
        return _ambiguous(
            rel,
            obs,
            evidence,
            "multiple_registry_identities_for_hash",
            subject_id=subject_id,
        )
    if len(tracker_digests_for_path) > 1:
        return _ambiguous(
            rel,
            obs,
            evidence,
            "multiple_tracker_records_for_path",
            subject_id=subject_id,
        )

    path_bound_hashes = {
        str(row["source_hash"])
        for row in registry_path_rows
        if row.get("source_hash")
    }
    for digest in tracker_digests_for_path:
        path_bound_hashes.add(digest)

    if obs.exists and obs.source_hash and path_bound_hashes:
        other_hashes = {h for h in path_bound_hashes if h != obs.source_hash}
        if other_hashes:
            return TargetClassification(
                target=rel,
                classification=CLASS_DIFFERENT_REVISION,
                confidence=CONFIDENCE_HIGH,
                proposed_operation=OP_CERTIFIED_APPEND_PROPOSAL,
                approval=APPROVAL_SEPARATE_INDEX_APPROVAL,
                embedding_action=EMBEDDING_PENDING_SEPARATE_APPROVAL,
                result=RESULT_PARTIAL,
                expected_new_vectors=None,
                document_id=obs.document_id,
                source_hash=obs.source_hash,
                subject_id=subject_id,
                evidence={
                    **evidence,
                    "path_bound_hashes": sorted(path_bound_hashes),
                    "reason": "filesystem_hash_differs_from_path_bound_hash",
                },
            )

    governed = bool(registry_for_hash or tracker_for_hash)
    if not governed:
        klass = ABSENCE_CLASS_BY_INTENT[intent]
        operation = ABSENCE_OPERATION_BY_INTENT[intent]
        result = {
            CLASS_NEW_DOCUMENT: RESULT_PARTIAL,
            CLASS_NOT_INDEXED: RESULT_PARTIAL,
            CLASS_AMBIGUOUS: RESULT_AMBIGUOUS,
        }[klass]
        approval = _approval_for(klass, intent, operation)
        return TargetClassification(
            target=rel,
            classification=klass,
            confidence=CONFIDENCE_MEDIUM if klass != CLASS_AMBIGUOUS else CONFIDENCE_LOW,
            proposed_operation=operation,
            approval=approval,
            embedding_action=_embedding_for(klass, operation),
            result=result,
            expected_new_vectors=None if klass == CLASS_NEW_DOCUMENT else 0,
            document_id=obs.document_id if obs.exists else None,
            source_hash=obs.source_hash,
            subject_id=subject_id,
            evidence={**evidence, "reason": "no_governed_same_byte_identity"},
        )

    if any(id_pairs_disagree(c) for c in comparisons.values()):
        return _ambiguous(
            rel,
            obs,
            {**evidence, "reason": "id_set_mismatch"},
            "id_set_mismatch",
            subject_id=subject_id,
        )

    registry_aliases = tuple(registry_for_hash[0].aliases) if registry_for_hash else ()
    tracker_paths = _tracker_paths(tracker_for_hash)
    governed_paths = tuple(sorted(set(registry_aliases) | set(tracker_paths)))
    evidence["governed_paths"] = list(governed_paths)
    evidence["registry_aliases"] = list(registry_aliases)
    evidence["tracker_paths"] = list(tracker_paths)

    chroma_sources = sorted(
        {
            rec.source_path
            for rec in chroma_records_by_id.values()
            if rec.source_path
        }
    )
    evidence["chroma_sources"] = chroma_sources
    if compat_conflict:
        evidence["compat_state"] = compat_state
        evidence["compat_reason"] = compat_reason
    return _classify_with_paths(
        intent=intent,
        rel=rel,
        obs=obs,
        governed_paths=governed_paths,
        registry_aliases=registry_aliases,
        tracker_paths=tracker_paths,
        chroma_sources=chroma_sources,
        vector_map_present=vector_map_present,
        vector_map_gap=vector_map_gap,
        chroma_configured=chroma_configured,
        tracker_configured=tracker_configured,
        registry_configured=registry_configured,
        registry_read=registry_read,
        evidence=evidence,
        subject_id=subject_id,
        filesystem_by_rel=filesystem_by_rel,
        compat_conflict=compat_conflict,
        compat_state=compat_state,
        compat_reason=compat_reason,
    )


def _ambiguous(
    rel: str,
    obs: FilesystemObservation,
    evidence: dict[str, Any],
    flag: str,
    *,
    subject_id: str | None,
) -> TargetClassification:
    return TargetClassification(
        target=rel,
        classification=CLASS_AMBIGUOUS,
        confidence=CONFIDENCE_LOW,
        proposed_operation=OP_MANUAL_REVIEW,
        approval=APPROVAL_BLOCKED,
        embedding_action=EMBEDDING_NONE,
        result=RESULT_AMBIGUOUS,
        expected_new_vectors=0,
        document_id=obs.document_id,
        source_hash=obs.source_hash,
        subject_id=subject_id,
        evidence={**evidence, "ambiguity": flag},
    )


def _exists_same_hash(
    rel: str,
    expected_hash: str | None,
    filesystem: dict[str, FilesystemObservation],
) -> bool | None:
    obs = filesystem.get(rel)
    if obs is None:
        return None
    if not obs.exists:
        return False
    if expected_hash is None:
        return True
    if not obs.hashed:
        return None
    return obs.source_hash == expected_hash


def _id_comparisons_disagree(comparisons: dict[str, Any]) -> bool:
    for comp in comparisons.values():
        if id_pairs_disagree(comp):
            return True
    return False


def _exact_duplicate_retirement_classification(
    rel: str,
    obs: FilesystemObservation,
    evidence: dict[str, Any],
    live: list[str],
    *,
    subject_id: str | None,
) -> TargetClassification:
    return TargetClassification(
        target=rel,
        classification=CLASS_EXACT_DUPLICATE,
        confidence=CONFIDENCE_HIGH,
        proposed_operation=OP_RETIREMENT_PROPOSAL,
        approval=APPROVAL_REQUIRED,
        embedding_action=EMBEDDING_NONE,
        result=RESULT_PARTIAL,
        expected_new_vectors=0,
        document_id=obs.document_id,
        source_hash=obs.source_hash,
        subject_id=subject_id,
        evidence={**evidence, "retained_aliases": live},
    )


def _plan_delete_governed_exact_duplicate_eligible(
    *,
    intent: str,
    obs: FilesystemObservation,
    live: list[str],
    compat_conflict: bool,
    evidence: dict[str, Any],
) -> bool:
    if intent != INTENT_PLAN_DELETE:
        return False
    if not obs.exists or not obs.source_hash:
        return False
    if not live:
        return False
    if compat_conflict:
        return False
    if _id_comparisons_disagree(dict(evidence.get("id_comparisons") or {})):
        return False
    return True


def _classify_with_paths(
    *,
    intent: str,
    rel: str,
    obs: FilesystemObservation,
    governed_paths: tuple[str, ...],
    registry_aliases: tuple[str, ...],
    tracker_paths: tuple[str, ...],
    chroma_sources: list[str],
    vector_map_present: bool,
    vector_map_gap: bool,
    chroma_configured: bool,
    tracker_configured: bool,
    registry_configured: bool,
    registry_read: bool,
    evidence: dict[str, Any],
    subject_id: str | None,
    filesystem_by_rel: dict[str, FilesystemObservation],
    compat_conflict: bool = False,
    compat_state: str | None = None,
    compat_reason: str | None = None,
) -> TargetClassification:
    fs = filesystem_by_rel
    live: list[str] = []
    absent: list[str] = []
    others = [p for p in governed_paths if p != rel]
    for other in others:
        status = _exists_same_hash(other, obs.source_hash, fs)
        if status is True:
            live.append(other)
        elif status is False:
            absent.append(other)
        elif other in fs and not fs[other].exists:
            absent.append(other)
    evidence["live_other_paths"] = live
    evidence["absent_other_paths"] = absent

    requested_in_registry = rel in registry_aliases
    requested_in_tracker = rel in tracker_paths
    chroma_matches_target = bool(chroma_sources) and set(chroma_sources) == {rel}
    chroma_stale = bool(chroma_sources) and rel not in chroma_sources

    if not obs.exists:
        return _ambiguous(
            rel,
            obs,
            {**evidence, "reason": "target_missing_on_filesystem"},
            "target_missing",
            subject_id=subject_id,
        )

    # Same-byte extra path (not yet a governed alias).
    if obs.exists and not requested_in_registry and not requested_in_tracker and live:
        if intent == INTENT_PLAN_DELETE:
            return _exact_duplicate_retirement_classification(
                rel, obs, evidence, live, subject_id=subject_id
            )
        return TargetClassification(
            target=rel,
            classification=CLASS_ALIAS_ONLY,
            confidence=CONFIDENCE_HIGH,
            proposed_operation=OP_ALIAS_REGISTER,
            approval=APPROVAL_REQUIRED,
            embedding_action=EMBEDDING_NONE,
            result=RESULT_PARTIAL,
            expected_new_vectors=0,
            document_id=obs.document_id,
            source_hash=obs.source_hash,
            subject_id=subject_id,
            evidence={**evidence, "retained_aliases": live},
        )

    # Same-byte move: one governed locator, that locator is absent, request is the live file.
    if (
        obs.exists
        and not requested_in_registry
        and not requested_in_tracker
        and len(others) == 1
        and not live
        and others[0] in absent
    ):
        return TargetClassification(
            target=rel,
            classification=CLASS_SAME_BYTES_MOVED,
            confidence=CONFIDENCE_HIGH,
            proposed_operation=OP_METADATA_ONLY_RECONCILE,
            approval=APPROVAL_REQUIRED,
            embedding_action=EMBEDDING_NONE,
            result=RESULT_PARTIAL,
            expected_new_vectors=0,
            document_id=obs.document_id,
            source_hash=obs.source_hash,
            subject_id=subject_id,
            evidence={**evidence, "old_path": others[0]},
        )

    if obs.exists and not requested_in_registry and not requested_in_tracker:
        if others and not live and not absent:
            return _ambiguous(
                rel,
                obs,
                {**evidence, "reason": "old_path_candidates_not_inspected"},
                "old_path_uninspected",
                subject_id=subject_id,
            )
        if len(others) > 1:
            return _ambiguous(
                rel,
                obs,
                {**evidence, "reason": "multiple_current_path_candidates"},
                "multiple_current_candidates",
                subject_id=subject_id,
            )

    # Path is governed. Check lower-store drift vs INDEXED_OK.
    if requested_in_registry or requested_in_tracker:
        if _plan_delete_governed_exact_duplicate_eligible(
            intent=intent,
            obs=obs,
            live=live,
            compat_conflict=compat_conflict,
            evidence=evidence,
        ):
            return _exact_duplicate_retirement_classification(
                rel, obs, evidence, live, subject_id=subject_id
            )
        if chroma_stale or (requested_in_registry and not requested_in_tracker and tracker_paths):
            return TargetClassification(
                target=rel,
                classification=CLASS_STALE_METADATA,
                confidence=CONFIDENCE_MEDIUM,
                proposed_operation=OP_METADATA_ONLY_RECONCILE,
                approval=APPROVAL_REQUIRED,
                embedding_action=EMBEDDING_NONE,
                result=RESULT_PARTIAL,
                expected_new_vectors=0,
                document_id=obs.document_id,
                source_hash=obs.source_hash,
                subject_id=subject_id,
                evidence={**evidence, "reason": "lower_store_path_stale"},
            )
        stores_ok = True
        if registry_read and not requested_in_registry:
            stores_ok = False
        if tracker_configured and tracker_paths and not requested_in_tracker:
            stores_ok = False
        if chroma_configured and chroma_sources and not chroma_matches_target:
            stores_ok = False
        if stores_ok and requested_in_registry and (not tracker_configured or requested_in_tracker):
            if chroma_configured and chroma_sources and not chroma_matches_target:
                stores_ok = False
        if stores_ok and (requested_in_registry or (not registry_read and requested_in_tracker)):
            # Compatibility gate: a detected fingerprint conflict or non-compatible
            # generation state invalidates authority coherence.  Do not classify as
            # INDEXED_OK when the configured generation evidence disagrees.
            if compat_conflict:
                return _ambiguous(
                    rel,
                    obs,
                    {
                        **evidence,
                        "reason": "generation_compatibility_conflict",
                        "compat_state": compat_state,
                        "compat_reason": compat_reason,
                    },
                    "generation_compatibility_conflict",
                    subject_id=subject_id,
                )
            verified = (
                (not registry_configured or (registry_read and requested_in_registry and vector_map_present))
                and (not tracker_configured or requested_in_tracker)
                and (not chroma_configured or chroma_matches_target or not chroma_sources)
                and not vector_map_gap
            )
            result = RESULT_VERIFIED if verified else RESULT_PARTIAL
            if vector_map_gap:
                result = RESULT_PARTIAL
            return TargetClassification(
                target=rel,
                classification=CLASS_INDEXED_OK,
                confidence=CONFIDENCE_HIGH if result == RESULT_VERIFIED else CONFIDENCE_MEDIUM,
                proposed_operation=OP_NO_OP,
                approval=APPROVAL_NOT_REQUIRED,
                embedding_action=EMBEDDING_NONE,
                result=result,
                expected_new_vectors=0,
                document_id=obs.document_id,
                source_hash=obs.source_hash,
                subject_id=subject_id,
                evidence={
                    **evidence,
                    "reason": "coherent_indexed_document",
                    "vector_map_present": vector_map_present,
                },
            )

    if intent == INTENT_PLAN_DELETE and live:
        return _exact_duplicate_retirement_classification(
            rel, obs, evidence, live, subject_id=subject_id
        )

    if intent == INTENT_PLAN_DELETE and requested_in_registry and not live:
        return TargetClassification(
            target=rel,
            classification=CLASS_AMBIGUOUS,
            confidence=CONFIDENCE_LOW,
            proposed_operation=OP_MANUAL_REVIEW,
            approval=APPROVAL_BLOCKED,
            embedding_action=EMBEDDING_NONE,
            result=RESULT_BLOCKED,
            expected_new_vectors=0,
            document_id=obs.document_id,
            source_hash=obs.source_hash,
            subject_id=subject_id,
            evidence={**evidence, "reason": "last_alias_not_proven"},
        )

    return _ambiguous(
        rel,
        obs,
        {**evidence, "reason": "unable_to_classify_closed_set"},
        "unclassified",
        subject_id=subject_id,
    )


def _rollup(items: Sequence[TargetClassification]) -> tuple[str, str, str, str, str]:
    if not items:
        return (
            CLASS_AMBIGUOUS,
            OP_MANUAL_REVIEW,
            APPROVAL_BLOCKED,
            EMBEDDING_NONE,
            RESULT_AMBIGUOUS,
        )
    if len(items) == 1:
        item = items[0]
        return (
            item.classification,
            item.proposed_operation,
            item.approval,
            item.embedding_action,
            item.result,
        )
    classes = {i.classification for i in items}
    ops = {i.proposed_operation for i in items}
    if len(classes) == 1 and len(ops) == 1:
        item = items[0]
        results = {i.result for i in items}
        result = item.result
        if RESULT_AMBIGUOUS in results:
            result = RESULT_AMBIGUOUS
        elif RESULT_BLOCKED in results:
            result = RESULT_BLOCKED
        elif RESULT_FAILED in results:
            result = RESULT_FAILED
        elif RESULT_PARTIAL in results:
            result = RESULT_PARTIAL
        elif results == {RESULT_VERIFIED}:
            result = RESULT_VERIFIED
        return (
            item.classification,
            item.proposed_operation,
            item.approval,
            item.embedding_action,
            result,
        )
    return (
        CLASS_AMBIGUOUS,
        OP_MANUAL_REVIEW,
        APPROVAL_BLOCKED,
        EMBEDDING_NONE,
        RESULT_AMBIGUOUS,
    )


def resolve_library_state(
    intent: str,
    targets: Sequence[str],
    *,
    library_root: str | Path,
    persist_dir: str | Path | None = None,
    registry_db: str | Path | None = None,
    tracker_path: str | Path | None = None,
    journal_path: str | Path | None = None,
    operation_id: str | None = None,
    request_id: str | None = None,
) -> OperationPlan:
    """Classify explicit targets and return a machine-readable plan. Read-only."""
    if intent not in SUPPORTED_INTENTS:
        raise ValueError(f"unsupported intent: {intent!r}")
    root = Path(library_root).resolve()
    if not root.is_dir():
        raise ValueError(f"library_root is not a directory: {root}")

    requested = tuple(_normalize_target(t, root) for t in targets)
    rid = request_id or make_request_id(intent, requested)

    handles = gather_store_handles(
        persist_dir=persist_dir,
        registry_db=registry_db,
        tracker_path=tracker_path,
    )
    persist = handles["persist_dir"]
    bundle = EvidenceBundle()
    bundle.stores_configured.append("filesystem")
    bundle.stores_read.append("filesystem")

    file_targets: list[str] = []
    for rel in requested:
        abs_path = root / rel
        if abs_path.is_dir():
            bundle.inspected_paths.append(rel)
            file_targets.extend(_list_folder_files(rel, root))
        else:
            file_targets.append(rel)
    file_targets = sorted(set(file_targets))

    for rel in file_targets:
        _observe(bundle, rel, library_root=root, hash_if_exists=True)

    registry_conn = open_optional_registry(handles["registry_db"], bundle)
    tracker_records = load_optional_tracker(handles["tracker_path"], bundle)
    tracker_by_path = _index_tracker_by_path(tracker_records)

    try:
        identities_for_files: dict[str, list[RegistryIdentity]] = {}
        path_rows_for_files: dict[str, list[dict[str, Any]]] = {}
        candidate_paths: set[str] = set()

        for rel in file_targets:
            obs = bundle.filesystem[rel]
            ids: list[RegistryIdentity] = []
            path_rows: list[dict[str, Any]] = []
            if registry_conn is not None:
                by_hash, _ = load_registry_identity(
                    registry_conn, source_hash=obs.source_hash
                ) if obs.source_hash else ([], [])
                by_path_ids, path_rows = load_registry_identity(
                    registry_conn, relative_path=rel
                )
                merged = {i.document_id: i for i in by_hash}
                for i in by_path_ids:
                    merged.setdefault(i.document_id, i)
                ids = [merged[k] for k in sorted(merged)]
            identities_for_files[rel] = ids
            path_rows_for_files[rel] = path_rows
            for ident in ids:
                candidate_paths.update(ident.aliases)
            digest = obs.source_hash
            if digest and digest in tracker_records:
                candidate_paths.update(_tracker_paths(tracker_records[digest]))
            for digest in tracker_by_path.get(rel, []):
                candidate_paths.update(_tracker_paths(tracker_records[digest]))

        # Containment gate: every registry/tracker-derived candidate path must
        # resolve strictly inside library_root before it may be observed.
        # Absolute paths, traversal segments, and symlink escapes are all
        # rejected here.  A deterministic gap is recorded per excluded category
        # so the authority snapshot remains auditable.
        contained_candidates: list[str] = []
        containment_gaps: set[str] = set()
        for cand in sorted(candidate_paths):
            if cand in bundle.filesystem:
                contained_candidates.append(cand)
                continue
            # Reject candidates that look absolute or contain traversal.
            raw = str(cand)
            if raw.startswith("/") or raw.startswith("\\"):
                containment_gaps.add("candidate_path_containment:absolute_rejected")
                continue
            if ".." in raw.split("/") or ".." in raw.split("\\"):
                containment_gaps.add("candidate_path_containment:traversal_rejected")
                continue
            # Symlink-resolved containment check.
            try:
                resolved = (root / cand).resolve()
                resolved.relative_to(root.resolve())
            except (ValueError, OSError):
                containment_gaps.add("candidate_path_containment:symlink_escape_rejected")
                continue
            contained_candidates.append(cand)
        for gap in sorted(containment_gaps):
            bundle.add_gap(gap)
        extra_candidates = [p for p in contained_candidates if p not in bundle.filesystem]
        for rel in extra_candidates:
            _observe(bundle, rel, library_root=root, hash_if_exists=True)

        # Union query IDs across resolved identities for these targets only.
        query_ids_parts: list[str] = []
        per_target_tracker: dict[str, TrackerRecord | None] = {}
        per_target_query: dict[str, tuple[str, ...]] = {}
        per_target_reg_vectors: dict[str, tuple[str, ...]] = {}
        per_target_vector_map: dict[str, bool] = {}

        for rel in file_targets:
            obs = bundle.filesystem[rel]
            rec = tracker_records.get(obs.source_hash) if obs.source_hash else None
            per_target_tracker[rel] = rec
            tracker_ids = tuple(rec.chunk_ids) if rec is not None else ()
            idents = identities_for_files.get(rel) or []
            matching = [
                i
                for i in idents
                if obs.source_hash and i.source_hash == obs.source_hash
            ]
            vector_present = any(i.vector_map_present for i in matching)
            vector_ids: list[str] = []
            if vector_present:
                for i in matching:
                    vector_ids.extend(i.vector_ids)
            per_target_vector_map[rel] = vector_present
            per_target_reg_vectors[rel] = tuple(sorted(set(vector_ids)))
            q = chroma_union_query_ids(
                tracker_chunk_ids=tracker_ids,
                registry_vector_ids=per_target_reg_vectors[rel],
                registry_vector_map_present=vector_present,
            )
            per_target_query[rel] = q
            query_ids_parts.extend(q)
            note_vector_map_gap(matching, bundle)

        query_ids = tuple(sorted(set(query_ids_parts)))
        bundle.query_ids = query_ids
        chroma = load_optional_chroma(handles["chroma_sqlite"], query_ids, bundle)
        bundle.chroma = chroma
        chroma_by_id = {r.chroma_embedding_id: r for r in (chroma.records if chroma else ())}

        journal, journal_gap = read_journal_if_exactly_located(
            persist_dir=persist,
            journal_path=journal_path,
            operation_id=operation_id,
        )
        bundle.journal = journal
        if journal_gap:
            bundle.add_gap(journal_gap)
        generation, gen_gap = maybe_evaluate_generation(
            persist, registry_db=handles["registry_db"]
        )
        bundle.generation = generation
        if gen_gap:
            bundle.add_gap(gen_gap)

        # Derive compatibility conflict flag for classification gating.
        # Only states that mean "authority disagrees" block INDEXED_OK.
        # Missing sidecar (gen is None or gap-only) is non-blocking by contract.
        _NON_BLOCKING_COMPAT_STATES = frozenset({
            "KNOWN_COMPATIBLE",
            "EMPTY_UNINITIALIZED",
        })
        _compat_state: str | None = (generation or {}).get("state")
        _compat_reason: str | None = (generation or {}).get("reason")
        _compat_conflict = bool(
            _compat_state and _compat_state not in _NON_BLOCKING_COMPAT_STATES
        )

        classifications: list[TargetClassification] = []
        for rel in file_targets:
            obs = bundle.filesystem[rel]
            rec = per_target_tracker.get(rel)
            tracker_ids = tuple(rec.chunk_ids) if rec is not None else ()
            vector_ids = per_target_reg_vectors.get(rel) or ()
            vector_present = per_target_vector_map.get(rel, False)
            qids = per_target_query.get(rel) or ()
            found_for_target = tuple(
                eid for eid in (chroma.found_ids if chroma else ()) if eid in set(qids)
            )
            comparisons: dict[str, Any] = {}
            if rec is not None and vector_present:
                comparisons["tracker_vs_registry"] = pairwise_id_comparison(
                    "tracker", tracker_ids, "registry", vector_ids
                )
            if rec is not None and chroma is not None:
                comparisons["tracker_vs_chroma"] = pairwise_id_comparison(
                    "tracker", tracker_ids, "chroma", found_for_target
                )
            if vector_present and chroma is not None:
                comparisons["registry_vs_chroma"] = pairwise_id_comparison(
                    "registry", vector_ids, "chroma", found_for_target
                )
            target_chroma = {eid: chroma_by_id[eid] for eid in found_for_target if eid in chroma_by_id}
            matching = [
                i
                for i in identities_for_files.get(rel, [])
                if obs.source_hash and i.source_hash == obs.source_hash
            ]
            item = _classify_target(
                intent=intent,
                rel=rel,
                obs=obs,
                registry_for_hash=matching,
                registry_path_rows=path_rows_for_files.get(rel, []),
                tracker_for_hash=rec,
                tracker_digests_for_path=tracker_by_path.get(rel, []),
                chroma_records_by_id=target_chroma,
                query_ids=qids,
                comparisons=comparisons,
                vector_map_present=vector_present,
                vector_map_gap=GAP_REGISTRY_VECTOR_MAP_ABSENT in bundle.gaps
                and bool(matching),
                chroma_configured="chroma" in bundle.stores_configured,
                tracker_configured="tracker" in bundle.stores_configured,
                registry_configured="registry" in bundle.stores_configured,
                registry_read="registry" in bundle.stores_read,
                filesystem_by_rel=bundle.filesystem,
                compat_conflict=_compat_conflict,
                compat_state=_compat_state,
                compat_reason=_compat_reason,
            )
            classifications.append(item)

        classifications_t = tuple(classifications)
        classification, operation, approval, embedding, result = _rollup(classifications_t)
        plan_targets = tuple(sorted(set(requested) | set(extra_candidates)))
        snapshot = {
            "filesystem": {k: v.to_dict() for k, v in sorted(bundle.filesystem.items())},
            "hashed_paths": sorted(bundle.hashed_paths),
            "inspected_paths": sorted(bundle.inspected_paths),
            "registry": [i.to_dict() for ids in identities_for_files.values() for i in ids],
            "tracker_digests": sorted(tracker_records),
            "query_ids": list(query_ids),
            "chroma": chroma.to_dict() if chroma is not None else None,
            "id_comparisons": {
                item.target: dict(item.evidence.get("id_comparisons") or {})
                for item in classifications_t
            },
            "journal": bundle.journal,
            "generation": bundle.generation,
            "gaps": list(bundle.gaps),
            "stores_configured": list(bundle.stores_configured),
            "stores_read": list(bundle.stores_read),
        }
        # Attach per-target comparisons into snapshot from latest evidence.
        snapshot["id_comparisons"] = {
            c.target: dict(c.evidence.get("id_comparisons") or {})
            for c in classifications_t
        }
        risk: list[str] = []
        amb: list[str] = []
        for c in classifications_t:
            if c.classification == CLASS_SAME_BYTES_MOVED:
                risk.append("SAME_BYTE_PATH_DRIFT")
            if c.embedding_action != EMBEDDING_NONE:
                risk.append("INDEX_APPROVAL_REQUIRED")
            if c.classification == CLASS_AMBIGUOUS:
                amb.append(str((c.evidence or {}).get("ambiguity") or "ambiguous"))
            if c.classification == CLASS_EXACT_DUPLICATE:
                risk.append("DUPLICATE_PATH_RETIREMENT")
        affected = tuple(sorted(set(bundle.stores_read)))
        summary = " | ".join(_summary(c) for c in classifications_t) or "no file targets"
        vcontract = (
            _verification_contract(classifications_t[0].classification, classifications_t[0].target)
            if len(classifications_t) == 1
            else {"require": ["per_target_verification"], "targets": [c.target for c in classifications_t]}
        )
        return OperationPlan(
            request_id=rid,
            intent=intent,
            targets=plan_targets,
            classification=classification,
            authority_snapshot=snapshot,
            proposed_operation=operation,
            approval=approval,
            embedding_action=embedding,
            affected_stores=affected,
            risk_flags=tuple(sorted(set(risk))),
            ambiguity_flags=tuple(sorted(set(amb))),
            verification_contract=vcontract,
            evidence_summary=summary,
            result=result,
            classifications=classifications_t,
            evidence_gaps=tuple(bundle.gaps),
        )
    finally:
        if registry_conn is not None:
            registry_conn.close()

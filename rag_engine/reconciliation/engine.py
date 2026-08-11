"""Read-only reconciliation engine — classify only; never repair."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from rag_engine.reconciliation.chroma_reader import (
    audit_chroma,
    load_chroma_snapshot_readonly,
)
from rag_engine.reconciliation.fingerprint_evidence import (
    classify_extractor_version,
    classify_fingerprint_evidence,
    read_index_fingerprint_readonly,
)
from rag_engine.reconciliation.models import (
    BackfillReadiness,
    ChromaRecord,
    FingerprintEvidenceStatus,
    ReasonCode,
    ReconciliationResult,
    ReconciliationState,
    ReconciliationSummary,
    RegistrySnapshotRecord,
    TrackerRecord,
    select_primary_state,
)
from rag_engine.reconciliation.registry_snapshot import (
    load_registry_snapshot_readonly,
    registry_audit,
)
from rag_engine.reconciliation.source_observer import (
    SourceHashCache,
    normalize_locator,
    observe_source_path,
)
from rag_engine.reconciliation.tracker_reader import (
    audit_tracker,
    is_uuid_like,
    load_tracker_readonly,
)
from rag_engine.stable_identity import (
    IDENTITY_SCHEME_VERSION,
    chunk_id,
    document_id_from_source_hash,
    subject_id_pending,
    validate_source_hash,
)


def _sorted_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _advisory(state: ReconciliationState, reasons: set[ReasonCode]) -> BackfillReadiness:
    if state == ReconciliationState.MATCH:
        if ReasonCode.FINGERPRINT_UNKNOWN in reasons:
            return BackfillReadiness.REVIEW_REQUIRED
        return BackfillReadiness.SAFE_MAPPING_CANDIDATE
    if state in (
        ReconciliationState.HASH_MISMATCH,
        ReconciliationState.DUPLICATE_ACTIVE,
        ReconciliationState.CHUNK_COUNT_MISMATCH,
    ):
        return BackfillReadiness.REINDEX_REQUIRED
    if state == ReconciliationState.METADATA_MISMATCH:
        return BackfillReadiness.REVIEW_REQUIRED
    if state in (ReconciliationState.CHROMA_ONLY, ReconciliationState.REGISTRY_ONLY):
        return BackfillReadiness.REVIEW_REQUIRED
    return BackfillReadiness.UNRESOLVED


def _stable_chunk_mapping(
    *,
    document_id: str | None,
    n_chunks: int,
    historical_fingerprint: str | None,
    ordinals_known: bool,
) -> tuple[list[str | None], list[ReasonCode], int, int]:
    """Return per-ordinal stable chunk ids only when fully proven.

    Never substitutes current/default fingerprint for unknown historical values.
    Never treats page as ordinal.
    """
    proven = 0
    unresolved = 0
    reasons: list[ReasonCode] = []
    ids: list[str | None] = []

    if not historical_fingerprint:
        reasons.append(ReasonCode.FINGERPRINT_UNKNOWN)
        unresolved = n_chunks
        return [None] * n_chunks, reasons, proven, unresolved

    if not ordinals_known:
        reasons.append(ReasonCode.ORDINAL_UNRESOLVED)
        # page must never be used as ordinal
        reasons.append(ReasonCode.PAGE_NOT_ORDINAL)
        unresolved = n_chunks
        return [None] * n_chunks, reasons, proven, unresolved

    if not document_id:
        unresolved = n_chunks
        return [None] * n_chunks, reasons, proven, unresolved

    for ordinal in range(n_chunks):
        sid = chunk_id(
            document_id,
            historical_fingerprint,
            ordinal,
            identity_scheme_version=IDENTITY_SCHEME_VERSION,
        )
        ids.append(sid)
        proven += 1
    if proven:
        reasons.append(ReasonCode.STABLE_CHUNK_PROVEN)
    return ids, reasons, proven, unresolved


def reconcile(
    *,
    tracker: Mapping[str, TrackerRecord],
    chroma: Mapping[str, ChromaRecord],
    library_root: str | Path | None = None,
    registry: Sequence[RegistrySnapshotRecord] | None = None,
    historical_chunking_fingerprint: str | None = None,
    index_fingerprint: Mapping[str, Any] | None = None,
    hash_existing_sources: bool = True,
    include_chroma_only: bool = True,
    include_registry_only: bool = True,
) -> tuple[list[ReconciliationResult], ReconciliationSummary]:
    """Reconcile tracker ↔ Chroma ↔ optional registry snapshot (read-only).

    Complexity: O(T + C + R + E) with hash maps — no O(N²) joins.
    Does not mutate inputs or production state.
    """
    fp_info = classify_fingerprint_evidence(
        explicit_historical_fingerprint=historical_chunking_fingerprint,
        index_fingerprint=index_fingerprint,
        allow_cohort_inference=False,
    )
    ext_info = classify_extractor_version(
        index_fingerprint=index_fingerprint,
    )
    historical_fp = fp_info.get("fingerprint")
    assert fp_info.get("used_current_defaults") is False

    chroma_ids = set(chroma.keys())
    # Build tracker ownership index O(E)
    owners: dict[str, list[str]] = defaultdict(list)
    for digest, rec in tracker.items():
        for cid in rec.chunk_ids:
            owners[cid].append(digest)

    duplicate_chunk_ids = {cid for cid, ds in owners.items() if len(ds) > 1}

    # Same source_hash with conflicting tracker identities shouldn't happen
    # (digest IS the source_hash), but detect same Chroma ID multi-owner above.

    cache = SourceHashCache()
    results: list[ReconciliationResult] = []

    join_found = 0
    join_missing = 0
    hash_same = 0
    hash_diff = 0
    hash_unavailable = 0
    path_exact = 0
    path_norm = 0
    path_drift = 0
    collection_mismatch = 0
    missing_source_meta = 0
    count_match = 0
    count_mismatch = 0
    count_unresolved = 0
    missing_source_files = 0
    stable_proven_total = 0
    stable_unresolved_total = 0

    tracked_chroma_ids: set[str] = set()

    for digest in sorted(tracker.keys()):
        rec = tracker[digest]
        candidates: set[ReconciliationState] = set()
        reasons: set[ReasonCode] = set()
        evidence: dict[str, Any] = {
            "fingerprint_evidence": fp_info,
            "extractor_evidence": ext_info,
        }

        if rec.raw_record.get("_malformed"):
            candidates.add(ReconciliationState.UNKNOWN)
            reasons.add(ReasonCode.MALFORMED_TRACKER)

        if not rec.chunk_ids:
            reasons.add(ReasonCode.ZERO_CHUNK)

        if len(rec.source_paths) > 1:
            reasons.add(ReasonCode.MULTI_PATH)

        # Invalid legacy IDs
        invalid_ids = [cid for cid in rec.chunk_ids if not is_uuid_like(cid)]
        if invalid_ids:
            reasons.add(ReasonCode.INVALID_LEGACY_ID)
            evidence["invalid_legacy_ids_sample"] = invalid_ids[:10]

        # Duplicate ownership
        dup_hits = [cid for cid in rec.chunk_ids if cid in duplicate_chunk_ids]
        if dup_hits:
            candidates.add(ReconciliationState.DUPLICATE_ACTIVE)
            reasons.add(ReasonCode.DUPLICATE_CHUNK_OWNERSHIP)
            evidence["duplicate_chunk_ids_sample"] = dup_hits[:10]
            evidence["duplicate_owners_sample"] = {
                cid: owners[cid] for cid in dup_hits[:5]
            }

        # Tracker ↔ Chroma join
        found_ids = [cid for cid in rec.chunk_ids if cid in chroma_ids]
        missing_ids = [cid for cid in rec.chunk_ids if cid not in chroma_ids]
        join_found += len(found_ids)
        join_missing += len(missing_ids)
        tracked_chroma_ids.update(found_ids)

        if missing_ids:
            candidates.add(ReconciliationState.CHUNK_COUNT_MISMATCH)
            reasons.add(ReasonCode.MISSING_CHROMA_ID)
            evidence["missing_chroma_ids_sample"] = missing_ids[:10]
            evidence["tracker_chunk_count"] = len(rec.chunk_ids)
            evidence["chroma_found_count"] = len(found_ids)
            count_mismatch += 1
        elif rec.chunk_ids:
            count_match += 1
        else:
            count_unresolved += 1

        # Path / collection metadata vs Chroma
        chroma_sources = []
        chroma_collections = []
        for cid in found_ids:
            crec = chroma[cid]
            if crec.source_path:
                chroma_sources.append(crec.source_path)
            else:
                missing_source_meta += 1
            if crec.collection_meta:
                chroma_collections.append(crec.collection_meta)
            elif crec.physical_collection_name:
                chroma_collections.append(crec.physical_collection_name)

        tracker_norms = []
        for p in rec.source_paths:
            n = normalize_locator(p, library_root=library_root)
            tracker_norms.append(n or p)

        chroma_norms = []
        for p in chroma_sources:
            n = normalize_locator(p, library_root=library_root)
            chroma_norms.append(n or p)

        evidence["tracker_paths"] = list(rec.source_paths)
        evidence["chroma_sources_sample"] = sorted(set(chroma_sources))[:10]

        if found_ids and tracker_norms and chroma_norms:
            tset = set(tracker_norms)
            cset = set(chroma_norms)
            if tset == cset:
                path_exact += 1
            elif tset & cset:
                path_norm += 1
            else:
                # Check string equality without norm already covered; drift
                if set(rec.source_paths) != set(chroma_sources):
                    path_drift += 1
                    candidates.add(ReconciliationState.METADATA_MISMATCH)
                    reasons.add(ReasonCode.PATH_DRIFT)

        if (
            rec.collection
            and chroma_collections
            and rec.collection not in set(chroma_collections)
            and found_ids
        ):
            collection_mismatch += 1
            candidates.add(ReconciliationState.METADATA_MISMATCH)
            reasons.add(ReasonCode.COLLECTION_MISMATCH)
            evidence["tracker_collection"] = rec.collection
            evidence["chroma_collections_sample"] = sorted(set(chroma_collections))[:10]

        # Source hash: historical = tracker digest; current = observed file bytes
        historical_hash = rec.source_hash
        current_hash: str | None = None
        observed_paths = []
        any_missing_file = False
        hashing_enabled = bool(library_root) and hash_existing_sources
        if hashing_enabled and rec.source_paths:
            for p in rec.source_paths:
                obs = observe_source_path(
                    p,
                    library_root=library_root,  # type: ignore[arg-type]
                    hash_if_exists=True,
                    cache=cache,
                )
                observed_paths.append(obs.to_dict())
                if not obs.exists:
                    any_missing_file = True
                    missing_source_files += 1
                    reasons.add(ReasonCode.MISSING_SOURCE_FILE)
                elif obs.source_hash:
                    if current_hash is None:
                        current_hash = obs.source_hash
                    elif current_hash != obs.source_hash:
                        candidates.add(ReconciliationState.HASH_MISMATCH)
                        evidence["conflicting_current_hashes"] = True
        evidence["source_observations"] = observed_paths[:5]

        if current_hash is not None:
            if current_hash == historical_hash:
                hash_same += 1
            else:
                hash_diff += 1
                candidates.add(ReconciliationState.HASH_MISMATCH)
                evidence["historical_source_hash"] = historical_hash
                evidence["current_observed_source_hash"] = current_hash
        elif hashing_enabled:
            hash_unavailable += 1
            reasons.add(ReasonCode.SOURCE_HASH_UNVERIFIED)
            if any_missing_file or not rec.source_paths:
                # Cannot verify; prefer UNKNOWN over guessing MATCH
                if not candidates.intersection(
                    {
                        ReconciliationState.HASH_MISMATCH,
                        ReconciliationState.DUPLICATE_ACTIVE,
                        ReconciliationState.CHUNK_COUNT_MISMATCH,
                        ReconciliationState.METADATA_MISMATCH,
                    }
                ):
                    candidates.add(ReconciliationState.UNKNOWN)
        else:
            hash_unavailable += 1

        # document_id from trustworthy historical source_hash (tracker digest)
        document_id = None
        subject_id = None
        try:
            validate_source_hash(historical_hash)
            document_id = document_id_from_source_hash(historical_hash)
            subject_id = subject_id_pending(historical_hash)
        except Exception:
            document_id = None
            subject_id = None

        # Stable chunk mapping — only if fingerprint + ordinals proven
        # Ordinals: tracker chunk_ids list order is treated as candidate ordinal
        # ONLY when historical fingerprint is known; page is never ordinal.
        ordinals_known = bool(historical_fp) and bool(rec.chunk_ids)
        stable_ids, stable_reasons, proven, unresolved = _stable_chunk_mapping(
            document_id=document_id,
            n_chunks=len(rec.chunk_ids),
            historical_fingerprint=historical_fp,
            ordinals_known=ordinals_known,
        )
        reasons.update(stable_reasons)
        stable_proven_total += proven
        stable_unresolved_total += unresolved
        evidence["stable_chunk_mapping"] = {
            "proven": proven,
            "unresolved": unresolved,
            "ordinal_basis": (
                "tracker_chunk_ids_list_order"
                if ordinals_known
                else "unavailable"
            ),
            "page_used_as_ordinal": False,
            "used_current_defaults": False,
        }

        # If no stronger mismatch and join ok → MATCH
        strong = {
            ReconciliationState.HASH_MISMATCH,
            ReconciliationState.DUPLICATE_ACTIVE,
            ReconciliationState.CHUNK_COUNT_MISMATCH,
            ReconciliationState.METADATA_MISMATCH,
            ReconciliationState.UNKNOWN,
        }
        if not candidates.intersection(strong):
            if missing_ids and rec.chunk_ids:
                candidates.add(ReconciliationState.UNKNOWN)
            else:
                # Hashing disabled: allow MATCH on exact ID join + metadata agreement.
                # Hashing enabled: MATCH only when current hash verified equal.
                if not hashing_enabled:
                    candidates.add(ReconciliationState.MATCH)
                elif current_hash == historical_hash:
                    candidates.add(ReconciliationState.MATCH)
                else:
                    candidates.add(ReconciliationState.UNKNOWN)
                    reasons.add(ReasonCode.SOURCE_HASH_UNVERIFIED)

        if not candidates:
            candidates.add(ReconciliationState.UNKNOWN)

        state = select_primary_state(candidates)
        readiness = _advisory(state, reasons)
        results.append(
            ReconciliationResult(
                state=state,
                reason_codes=tuple(sorted(reasons, key=lambda r: r.value)),
                unit_kind="tracker_digest",
                unit_id=digest,
                subject_id=subject_id,
                document_id=document_id,
                source_hash=historical_hash,
                historical_source_hash=historical_hash,
                current_observed_source_hash=current_hash,
                source_paths=rec.source_paths,
                tracker_chunk_ids=rec.chunk_ids,
                chroma_embedding_ids=tuple(found_ids),
                stable_chunk_ids=tuple(stable_ids),
                evidence=evidence,
                backfill_readiness=readiness,
            )
        )

    # CHROMA_ONLY
    if include_chroma_only:
        for eid in sorted(chroma_ids - tracked_chroma_ids):
            crec = chroma[eid]
            results.append(
                ReconciliationResult(
                    state=ReconciliationState.CHROMA_ONLY,
                    reason_codes=(ReasonCode.CHROMA_UNTRACKED,),
                    unit_kind="chroma_id",
                    unit_id=eid,
                    source_paths=(crec.source_path,) if crec.source_path else (),
                    chroma_embedding_ids=(eid,),
                    evidence={
                        "physical_collection_name": crec.physical_collection_name,
                        "source_path": crec.source_path,
                        "page": crec.page,
                        "collection_meta": crec.collection_meta,
                    },
                    backfill_readiness=BackfillReadiness.REVIEW_REQUIRED,
                )
            )

    # REGISTRY_ONLY
    if include_registry_only and registry:
        tracker_hashes = set(tracker.keys())
        for reg in registry:
            chroma_overlap = set(reg.chroma_embedding_ids) & chroma_ids
            tracker_overlap = (
                (reg.source_hash in tracker_hashes) if reg.source_hash else False
            )
            if chroma_overlap or tracker_overlap:
                continue
            results.append(
                ReconciliationResult(
                    state=ReconciliationState.REGISTRY_ONLY,
                    reason_codes=(ReasonCode.REGISTRY_ENTITY,),
                    unit_kind="registry_document",
                    unit_id=reg.document_id or reg.subject_id or "unknown",
                    subject_id=reg.subject_id,
                    document_id=reg.document_id,
                    source_hash=reg.source_hash,
                    historical_source_hash=reg.source_hash,
                    source_paths=reg.source_paths,
                    tracker_chunk_ids=(),
                    chroma_embedding_ids=reg.chroma_embedding_ids,
                    stable_chunk_ids=reg.chunk_ids,
                    evidence={"registry": reg.to_dict()},
                    backfill_readiness=BackfillReadiness.REVIEW_REQUIRED,
                )
            )

    # Deterministic ordering
    results.sort(key=lambda r: (r.state.value, r.unit_kind, r.unit_id))

    by_state = Counter(r.state.value for r in results)
    by_ready = Counter(r.backfill_readiness.value for r in results)

    summary = ReconciliationSummary(
        total=len(results),
        by_state={s.value: by_state.get(s.value, 0) for s in ReconciliationState},
        tracker_records=len(tracker),
        tracker_chunk_ids=sum(len(r.chunk_ids) for r in tracker.values()),
        chroma_records=len(chroma),
        stable_chunk_ids_proven=stable_proven_total,
        stable_chunk_ids_unresolved=stable_unresolved_total,
        missing_source_files=missing_source_files,
        historical_fingerprint_status=str(fp_info["status"]),
        extractor_version_status=str(ext_info["status"]),
        join={
            "tracker_chunk_ids_found_in_chroma": join_found,
            "tracker_chunk_ids_missing_in_chroma": join_missing,
            "chroma_ids_referenced_by_tracker": len(tracked_chroma_ids),
            "chroma_ids_not_referenced_by_tracker": len(chroma_ids - tracked_chroma_ids),
            "chroma_ids_with_multiple_tracker_owners": len(duplicate_chunk_ids),
        },
        hash_stats={
            "same": hash_same,
            "different": hash_diff,
            "unavailable": hash_unavailable,
        },
        path_stats={
            "exact_or_identical_normalized": path_exact,
            "partial_normalized_overlap": path_norm,
            "path_drift": path_drift,
            "collection_mismatch": collection_mismatch,
            "missing_source_metadata_on_linked_ids": missing_source_meta,
        },
        chunk_count_stats={
            "match": count_match,
            "mismatch": count_mismatch,
            "unresolved_zero_chunk": count_unresolved,
        },
        backfill_readiness={k.value: by_ready.get(k.value, 0) for k in BackfillReadiness},
        extras={
            "fingerprint_evidence": fp_info,
            "extractor_evidence": ext_info,
            "source_hash_cache": {"hits": cache.hits, "misses": cache.misses},
            "identity_scheme_version": IDENTITY_SCHEME_VERSION,
        },
    )
    return results, summary


def reconcile_paths(
    *,
    tracker_path: str | Path,
    chroma_sqlite_path: str | Path,
    library_root: str | Path | None = None,
    registry_db_path: str | Path | None = None,
    index_fingerprint_path: str | Path | None = None,
    historical_chunking_fingerprint: str | None = None,
    hash_existing_sources: bool = True,
) -> tuple[list[ReconciliationResult], ReconciliationSummary, dict[str, Any]]:
    """High-level path-based reconciliation entrypoint (all inputs explicit)."""
    tracker = load_tracker_readonly(tracker_path)
    chroma = load_chroma_snapshot_readonly(chroma_sqlite_path)
    registry = None
    if registry_db_path is not None:
        registry = load_registry_snapshot_readonly(registry_db_path)
    index_fp = None
    if index_fingerprint_path is not None:
        index_fp = read_index_fingerprint_readonly(index_fingerprint_path)

    results, summary = reconcile(
        tracker=tracker,
        chroma=chroma,
        library_root=library_root,
        registry=registry,
        historical_chunking_fingerprint=historical_chunking_fingerprint,
        index_fingerprint=index_fp,
        hash_existing_sources=hash_existing_sources,
    )
    audits = {
        "tracker_audit": audit_tracker(tracker),
        "chroma_audit": audit_chroma(dict(chroma)),
        "registry_audit": registry_audit(list(registry or [])),
    }
    return results, summary, audits

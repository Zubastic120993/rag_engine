"""Phase 6C operator-gated legacy-index certification workflow.

Inspection / evaluation / dry-run are read-only with respect to the target
index. Apply mode writes fingerprint authority + audit only when explicitly
requested, against a verified target binding, using historical evidence
(not runtime-config assumption).

Production apply is NOT authorized by Phase 6C itself.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from rag_engine.index_compatibility.builders import (
    build_corpus_spec,
    build_embedding_spec,
    build_index_spec,
    build_runtime_contracts_from_config,
    stored_envelope_from_specs,
)
from rag_engine.index_compatibility.chroma_inspect import count_vectors_readonly
from rag_engine.index_compatibility.compatibility import evaluate_compatibility
from rag_engine.index_compatibility.constants import (
    COMPAT_KNOWN_COMPATIBLE,
    COMPAT_UNKNOWN_LEGACY,
    DEFAULT_PHYSICAL_COLLECTION,
    FINGERPRINT_SCHEMA_VERSION,
    SIDECAR_V0_NAME,
    SIDECAR_V1_NAME,
)
from rag_engine.index_compatibility.exceptions import (
    CertificationConflictError,
    CertificationEvidenceError,
    CertificationRequiresOperatorApprovalError,
    CertificationTargetChangedError,
    LegacyIndexNotCertifiableError,
)
from rag_engine.index_compatibility.specs import (
    CorpusFingerprintSpec,
    EmbeddingFingerprintSpec,
    IndexFingerprintSpec,
    StoredIndexFingerprint,
    canonical_json,
    digest_hex,
)
from rag_engine.index_compatibility.state import (
    initialize_fingerprint_state,
    load_authoritative_state,
    read_sidecar_v1,
    sidecar_v1_path,
    write_registry_fingerprint,
    write_sidecar_v1,
)

CERTIFICATION_MANIFEST_SCHEMA = "legacy-cert-manifest-v1"
CERTIFICATION_AUDIT_SCHEMA = "legacy-cert-audit-v1"
CERTIFICATION_AUDIT_NAME = "index_embedding_certification_v1.json"

# Decision states (Phase 6C)
DEC_CERTIFIABLE = "CERTIFIABLE"
DEC_NOT_CERTIFIABLE = "NOT_CERTIFIABLE"
DEC_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
DEC_EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
DEC_MIXED_HISTORY_SUSPECTED = "MIXED_HISTORY_SUSPECTED"
DEC_REBUILD_REQUIRED = "REBUILD_REQUIRED"

# Evidence strength hierarchy
EVIDENCE_LEVEL_A = "LEVEL_A_DIRECT"
EVIDENCE_LEVEL_B = "LEVEL_B_CORROBORATED"
EVIDENCE_LEVEL_C = "LEVEL_C_CIRCUMSTANTIAL"

EVIDENCE_KIND_FACT = "fact"
EVIDENCE_KIND_DERIVED = "derived_fact"
EVIDENCE_KIND_INFERENCE = "inference"
EVIDENCE_KIND_UNKNOWN = "unknown"

# Compatibility-critical fields that Level A/B must cover for certification.
REQUIRED_EMBEDDING_FIELDS = (
    "embedding_provider",
    "embedding_model",
    "embedding_model_revision",
    "embedding_dimension",
    "embedding_normalization",
    "embedding_mode",
    "tokenizer_id",
    "max_input_tokens",
)
REQUIRED_CORPUS_FIELDS = (
    "identity_scheme_version",
    "chunk_size",
    "chunk_overlap",
    "separators",
    "normalization",
    "min_chunk_chars",
    "max_chunk_chars",
    "extractor",
    "extractor_version",
    "embedded_text_composition_version",
)
REQUIRED_INDEX_FIELDS = (
    "vector_store",
    "distance_space",
    "physical_collection_name",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def certification_audit_path(persist: str | Path) -> Path:
    return Path(persist) / CERTIFICATION_AUDIT_NAME


@dataclass(frozen=True)
class EvidenceItem:
    field: str
    value: Any
    kind: str  # fact | derived_fact | inference | unknown
    level: str  # LEVEL_A_* | LEVEL_B_* | LEVEL_C_*
    source: str
    reference: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "kind": self.kind,
            "level": self.level,
            "source": self.source,
            "reference": dict(self.reference),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class TargetBinding:
    persist_dir: str
    physical_collection_name: str
    collection_id: str | None
    vector_count: int
    chroma_sqlite_sha256: str | None
    structural_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "persist_dir": self.persist_dir,
            "physical_collection_name": self.physical_collection_name,
            "collection_id": self.collection_id,
            "vector_count": self.vector_count,
            "chroma_sqlite_sha256": self.chroma_sqlite_sha256,
            "structural_fingerprint": self.structural_fingerprint,
        }


@dataclass(frozen=True)
class CertificationDecision:
    decision: str
    reasons: tuple[str, ...]
    highest_evidence_level: str | None
    historical_index_fingerprint: str | None
    runtime_index_fingerprint: str | None
    mixed_history: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "highest_evidence_level": self.highest_evidence_level,
            "historical_index_fingerprint": self.historical_index_fingerprint,
            "runtime_index_fingerprint": self.runtime_index_fingerprint,
            "mixed_history": self.mixed_history,
            "details": dict(self.details),
        }


def inspect_legacy_target(
    persist: str | Path,
    *,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
) -> dict[str, Any]:
    """Read-only snapshot of legacy target identity. Never writes."""
    persist_path = Path(persist).resolve()
    vector_count = count_vectors_readonly(
        persist_path, physical_collection_name=physical_collection_name
    )
    chroma = persist_path / "chroma.sqlite3"
    chroma_hash = _sha256_file(chroma)
    collection_id = _read_collection_id_readonly(persist_path, physical_collection_name)
    compat = evaluate_compatibility(
        persist_path,
        physical_collection_name=physical_collection_name,
        vector_count=vector_count,
    )
    v0_path = persist_path / SIDECAR_V0_NAME
    v0 = None
    if v0_path.is_file():
        try:
            v0 = json.loads(v0_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            v0 = {"_error": "unreadable"}
    binding_preimage = {
        "persist_dir": str(persist_path),
        "physical_collection_name": physical_collection_name,
        "collection_id": collection_id,
        "vector_count": vector_count,
        "chroma_sqlite_sha256": chroma_hash,
        # Deliberately exclude sidecar v1 / audit presence so certification
        # itself does not invalidate the target binding used for idempotent apply.
        "v0_sha256": _sha256_file(v0_path),
    }
    structural = digest_hex(canonical_json(binding_preimage))
    binding = TargetBinding(
        persist_dir=str(persist_path),
        physical_collection_name=physical_collection_name,
        collection_id=collection_id,
        vector_count=vector_count,
        chroma_sqlite_sha256=chroma_hash,
        structural_fingerprint=structural,
    )
    return {
        "target": binding.to_dict(),
        "compatibility": compat.to_dict(),
        "sidecar_v0": v0,
        "sidecar_v1_present": sidecar_v1_path(persist_path).is_file(),
        "certification_audit_present": certification_audit_path(persist_path).is_file(),
    }


def _read_collection_id_readonly(
    persist: Path, physical_collection_name: str
) -> str | None:
    import sqlite3

    db = persist / "chroma.sqlite3"
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM collections WHERE name = ? LIMIT 1",
            (physical_collection_name,),
        ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def verify_target_unchanged(
    persist: str | Path,
    expected: Mapping[str, Any],
    *,
    physical_collection_name: str = DEFAULT_PHYSICAL_COLLECTION,
) -> TargetBinding:
    """Re-measure target; raise if binding diverged (TOCTOU)."""
    current = inspect_legacy_target(
        persist, physical_collection_name=physical_collection_name
    )["target"]
    checks = (
        "physical_collection_name",
        "vector_count",
        "chroma_sqlite_sha256",
        "structural_fingerprint",
        "collection_id",
    )
    diffs: dict[str, Any] = {}
    for key in checks:
        if expected.get(key) != current.get(key):
            diffs[key] = {"expected": expected.get(key), "actual": current.get(key)}
    if diffs:
        raise CertificationTargetChangedError(
            "target index changed since evidence review; certification aborted",
            details={"diffs": diffs},
        )
    return TargetBinding(
        persist_dir=str(current["persist_dir"]),
        physical_collection_name=str(current["physical_collection_name"]),
        collection_id=current.get("collection_id"),
        vector_count=int(current["vector_count"]),
        chroma_sqlite_sha256=current.get("chroma_sqlite_sha256"),
        structural_fingerprint=str(current["structural_fingerprint"]),
    )


def _specs_from_historical_contract(
    historical: Mapping[str, Any],
) -> tuple[EmbeddingFingerprintSpec, CorpusFingerprintSpec, IndexFingerprintSpec]:
    emb_raw = historical.get("embedding")
    corp_raw = historical.get("corpus")
    idx_raw = historical.get("index") or {}
    if not isinstance(emb_raw, Mapping) or not isinstance(corp_raw, Mapping):
        raise CertificationEvidenceError(
            "historical_contract must include embedding and corpus objects"
        )
    if not isinstance(idx_raw, Mapping):
        raise CertificationEvidenceError("historical_contract.index must be an object")

    if emb_raw.get("fingerprint_schema_version") == FINGERPRINT_SCHEMA_VERSION:
        emb = EmbeddingFingerprintSpec.from_mapping(emb_raw)
    else:
        if "embedding_model" not in emb_raw:
            raise CertificationEvidenceError("historical embedding_model required")
        emb = build_embedding_spec(
            embedding_provider=str(emb_raw.get("embedding_provider", "ollama")),
            embedding_model=str(emb_raw["embedding_model"]),
            embedding_model_revision=emb_raw.get("embedding_model_revision"),
            embedding_dimension=emb_raw.get("embedding_dimension"),
            embedding_normalization=emb_raw.get("embedding_normalization"),
            embedding_mode=str(emb_raw.get("embedding_mode", "symmetric_ollama_v1")),
            tokenizer_id=emb_raw.get("tokenizer_id"),
            max_input_tokens=emb_raw.get("max_input_tokens"),
        )

    if set(REQUIRED_CORPUS_FIELDS).issubset(corp_raw.keys()):
        corp = CorpusFingerprintSpec.from_mapping(corp_raw)
    else:
        corp = build_corpus_spec(
            **{k: corp_raw[k] for k in REQUIRED_CORPUS_FIELDS if k in corp_raw}
        )

    if idx_raw.get("fingerprint_schema_version") == FINGERPRINT_SCHEMA_VERSION:
        idx = IndexFingerprintSpec.from_mapping(idx_raw)
    else:
        idx = build_index_spec(
            embedding=emb,
            corpus=corp,
            vector_store=str(idx_raw.get("vector_store", "chroma")),
            distance_space=str(idx_raw.get("distance_space", "l2")),
            physical_collection_name=str(
                idx_raw.get("physical_collection_name", DEFAULT_PHYSICAL_COLLECTION)
            ),
            index_schema_notes=idx_raw.get("index_schema_notes"),
        )
    return emb, corp, idx


def _parse_evidence_items(raw_items: Any) -> list[EvidenceItem]:
    if not isinstance(raw_items, list) or not raw_items:
        raise CertificationEvidenceError("evidence must be a non-empty list")
    items: list[EvidenceItem] = []
    for i, row in enumerate(raw_items):
        if not isinstance(row, Mapping):
            raise CertificationEvidenceError(f"evidence[{i}] must be an object")
        kind = str(row.get("kind") or "")
        level = str(row.get("level") or "")
        if kind not in {
            EVIDENCE_KIND_FACT,
            EVIDENCE_KIND_DERIVED,
            EVIDENCE_KIND_INFERENCE,
            EVIDENCE_KIND_UNKNOWN,
        }:
            raise CertificationEvidenceError(f"evidence[{i}].kind invalid: {kind}")
        if level not in {EVIDENCE_LEVEL_A, EVIDENCE_LEVEL_B, EVIDENCE_LEVEL_C}:
            raise CertificationEvidenceError(f"evidence[{i}].level invalid: {level}")
        field_name = str(row.get("field") or "").strip()
        if not field_name:
            raise CertificationEvidenceError(f"evidence[{i}].field required")
        items.append(
            EvidenceItem(
                field=field_name,
                value=row.get("value"),
                kind=kind,
                level=level,
                source=str(row.get("source") or ""),
                reference=dict(row.get("reference") or {}),
                notes=str(row.get("notes") or ""),
            )
        )
    return items


def _field_coverage(items: list[EvidenceItem]) -> dict[str, str]:
    """Map field -> best evidence level present (A > B > C)."""
    rank = {EVIDENCE_LEVEL_A: 3, EVIDENCE_LEVEL_B: 2, EVIDENCE_LEVEL_C: 1}
    best: dict[str, str] = {}
    for item in items:
        if item.kind == EVIDENCE_KIND_UNKNOWN:
            continue
        prev = best.get(item.field)
        if prev is None or rank[item.level] > rank[prev]:
            best[item.field] = item.level
    return best


def _required_fields_certifiable(coverage: Mapping[str, str]) -> tuple[bool, list[str]]:
    """All required fields must be covered by Level A or B (not C alone)."""
    missing: list[str] = []
    for field_name in (
        *REQUIRED_EMBEDDING_FIELDS,
        *REQUIRED_CORPUS_FIELDS,
        *REQUIRED_INDEX_FIELDS,
    ):
        level = coverage.get(field_name)
        if level not in {EVIDENCE_LEVEL_A, EVIDENCE_LEVEL_B}:
            missing.append(field_name)
    return (not missing), missing


def evaluate_certification(
    *,
    historical_contract: Mapping[str, Any],
    evidence: list[EvidenceItem] | list[Mapping[str, Any]],
    target_inspection: Mapping[str, Any],
    mixed_history_assessment: str = "NO_EVIDENCE_OF_MIXING",
    require_runtime_match: bool = True,
) -> CertificationDecision:
    """Decide certifiability from evidence. Pure evaluation — no writes."""
    items = (
        evidence
        if evidence and isinstance(evidence[0], EvidenceItem)
        else _parse_evidence_items(evidence)
    )
    assert all(isinstance(i, EvidenceItem) for i in items)

    if mixed_history_assessment in {"PROBABLE_MIXING", "PROVEN_MIXING"}:
        return CertificationDecision(
            decision=DEC_MIXED_HISTORY_SUSPECTED,
            reasons=(
                f"mixed_history_assessment={mixed_history_assessment}; "
                "cannot certify a single embedding contract for the collection",
            ),
            highest_evidence_level=None,
            historical_index_fingerprint=None,
            runtime_index_fingerprint=None,
            mixed_history=mixed_history_assessment,
        )
    if mixed_history_assessment == "POSSIBLE_MIXING":
        # Possible mixing does not auto-certify; requires Level A exclusion proof.
        has_level_a_no_mix = any(
            i.field == "mixed_history_exclusion"
            and i.level == EVIDENCE_LEVEL_A
            and i.kind == EVIDENCE_KIND_FACT
            for i in items
        )
        if not has_level_a_no_mix:
            return CertificationDecision(
                decision=DEC_MIXED_HISTORY_SUSPECTED,
                reasons=(
                    "POSSIBLE_MIXING without Level-A exclusion proof; "
                    "fail closed (rebuild path recommended)",
                ),
                highest_evidence_level=EVIDENCE_LEVEL_C,
                historical_index_fingerprint=None,
                runtime_index_fingerprint=None,
                mixed_history=mixed_history_assessment,
                details={"rebuild_recommended": True},
            )

    try:
        emb, corp, idx = _specs_from_historical_contract(historical_contract)
    except Exception as exc:  # noqa: BLE001
        return CertificationDecision(
            decision=DEC_EVIDENCE_CONFLICT,
            reasons=(f"historical_contract invalid: {exc}",),
            highest_evidence_level=None,
            historical_index_fingerprint=None,
            runtime_index_fingerprint=None,
            mixed_history=mixed_history_assessment,
        )

    historical_ifp = idx.digest()
    coverage = _field_coverage(list(items))
    ok_cov, missing = _required_fields_certifiable(coverage)
    levels = {i.level for i in items if i.kind != EVIDENCE_KIND_UNKNOWN}
    highest = None
    if EVIDENCE_LEVEL_A in levels:
        highest = EVIDENCE_LEVEL_A
    elif EVIDENCE_LEVEL_B in levels:
        highest = EVIDENCE_LEVEL_B
    elif EVIDENCE_LEVEL_C in levels:
        highest = EVIDENCE_LEVEL_C

    # Level C alone is never enough.
    if not ok_cov or highest not in {EVIDENCE_LEVEL_A, EVIDENCE_LEVEL_B}:
        return CertificationDecision(
            decision=DEC_INSUFFICIENT_EVIDENCE,
            reasons=(
                "certification requires Level A or Level B coverage of all "
                "compatibility-critical fields; Level C (model name/dimension/"
                "v0/current defaults) is insufficient alone",
                f"missing_or_level_c_fields={missing}",
            ),
            highest_evidence_level=highest,
            historical_index_fingerprint=historical_ifp,
            runtime_index_fingerprint=None,
            mixed_history=mixed_history_assessment,
            details={"coverage": coverage, "missing_fields": missing},
        )

    # Conflicting evidence values for the same field.
    by_field: dict[str, set[str]] = {}
    for item in items:
        if item.kind == EVIDENCE_KIND_UNKNOWN:
            continue
        by_field.setdefault(item.field, set()).add(
            json.dumps(item.value, sort_keys=True, default=str)
        )
    conflicts = {f: vals for f, vals in by_field.items() if len(vals) > 1}
    if conflicts:
        return CertificationDecision(
            decision=DEC_EVIDENCE_CONFLICT,
            reasons=("evidence values conflict for one or more fields",),
            highest_evidence_level=highest,
            historical_index_fingerprint=historical_ifp,
            runtime_index_fingerprint=None,
            mixed_history=mixed_history_assessment,
            details={"conflicts": {k: sorted(v) for k, v in conflicts.items()}},
        )

    compat = target_inspection.get("compatibility") or {}
    if compat.get("state") not in {COMPAT_UNKNOWN_LEGACY, None}:
        # Already certified / other state — not a legacy certification candidate
        if compat.get("state") == COMPAT_KNOWN_COMPATIBLE:
            stored = compat.get("stored_index_fingerprint")
            if stored == historical_ifp:
                return CertificationDecision(
                    decision=DEC_CERTIFIABLE,
                    reasons=("already certified with matching historical fingerprint",),
                    highest_evidence_level=highest,
                    historical_index_fingerprint=historical_ifp,
                    runtime_index_fingerprint=compat.get("runtime_index_fingerprint"),
                    mixed_history=mixed_history_assessment,
                    details={"already_compatible": True},
                )
            return CertificationDecision(
                decision=DEC_EVIDENCE_CONFLICT,
                reasons=("target already has conflicting KNOWN_COMPATIBLE authority",),
                highest_evidence_level=highest,
                historical_index_fingerprint=historical_ifp,
                runtime_index_fingerprint=compat.get("runtime_index_fingerprint"),
                mixed_history=mixed_history_assessment,
            )
        if compat.get("state") not in {COMPAT_UNKNOWN_LEGACY}:
            return CertificationDecision(
                decision=DEC_NOT_CERTIFIABLE,
                reasons=(f"target compatibility state is {compat.get('state')}",),
                highest_evidence_level=highest,
                historical_index_fingerprint=historical_ifp,
                runtime_index_fingerprint=compat.get("runtime_index_fingerprint"),
                mixed_history=mixed_history_assessment,
            )

    runtime_ifp = None
    try:
        _e, _c, ridx = build_runtime_contracts_from_config(
            physical_collection_name=str(
                (historical_contract.get("index") or {}).get(
                    "physical_collection_name", DEFAULT_PHYSICAL_COLLECTION
                )
            )
        )
        runtime_ifp = ridx.digest()
    except Exception:  # noqa: BLE001
        runtime_ifp = compat.get("runtime_index_fingerprint")

    if require_runtime_match and runtime_ifp and runtime_ifp != historical_ifp:
        return CertificationDecision(
            decision=DEC_NOT_CERTIFIABLE,
            reasons=(
                "historical fingerprint does not match current runtime fingerprint; "
                "do not certify as compatible-with-runtime (rebuild or change runtime)",
            ),
            highest_evidence_level=highest,
            historical_index_fingerprint=historical_ifp,
            runtime_index_fingerprint=runtime_ifp,
            mixed_history=mixed_history_assessment,
            details={"historical_ifp": historical_ifp, "runtime_ifp": runtime_ifp},
        )

    # Reject certification built solely by copying runtime without evidence.
    if any(
        i.source == "runtime_config_assumption" and i.level != EVIDENCE_LEVEL_A
        for i in items
    ) and highest != EVIDENCE_LEVEL_A:
        return CertificationDecision(
            decision=DEC_INSUFFICIENT_EVIDENCE,
            reasons=("runtime_config_assumption is not historical proof",),
            highest_evidence_level=highest,
            historical_index_fingerprint=historical_ifp,
            runtime_index_fingerprint=runtime_ifp,
            mixed_history=mixed_history_assessment,
        )

    return CertificationDecision(
        decision=DEC_CERTIFIABLE,
        reasons=(
            "Level A/B evidence covers all compatibility-critical fields; "
            "historical contract is consistent; target is UNKNOWN_LEGACY (or matching)",
        ),
        highest_evidence_level=highest,
        historical_index_fingerprint=historical_ifp,
        runtime_index_fingerprint=runtime_ifp,
        mixed_history=mixed_history_assessment,
    )


def build_certification_manifest(
    *,
    target: Mapping[str, Any],
    historical_contract: Mapping[str, Any],
    evidence: list[EvidenceItem] | list[Mapping[str, Any]],
    decision: CertificationDecision,
    operator_reason: str,
    actor: str | None = None,
    mixed_history_assessment: str = "NO_EVIDENCE_OF_MIXING",
) -> dict[str, Any]:
    """Build machine-readable evidence manifest. Does not write."""
    items = (
        [i.to_dict() for i in evidence]
        if evidence and isinstance(evidence[0], EvidenceItem)
        else [dict(x) for x in _parse_evidence_items(evidence)]
    )
    emb, corp, idx = _specs_from_historical_contract(historical_contract)
    envelope = stored_envelope_from_specs(emb, corp, idx)
    payload = {
        "schema_version": CERTIFICATION_MANIFEST_SCHEMA,
        "created_at": _utc_now(),
        "actor": actor,
        "operator_reason": operator_reason,
        "target": dict(target),
        "historical_contract": {
            "embedding": emb.to_contract(),
            "corpus": corp.to_contract(),
            "index": idx.to_contract(),
        },
        "proposed_authority_envelope": envelope,
        "evidence": items,
        "mixed_history_assessment": mixed_history_assessment,
        "decision": decision.to_dict(),
    }
    payload["manifest_hash"] = digest_hex(canonical_json(payload))
    return payload


def _read_audit(persist: Path) -> dict[str, Any] | None:
    path = certification_audit_path(persist)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CertificationConflictError(
            f"existing certification audit is unreadable: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise CertificationConflictError("certification audit root must be object")
    return data


def certify_legacy_index(
    persist: str | Path,
    *,
    evidence_manifest: Mapping[str, Any],
    apply: bool = False,
    operator_reason: str | None = None,
    actor: str | None = None,
    expected_vector_count: int | None = None,
    expected_structural_fingerprint: str | None = None,
    expected_compatibility_state: str = COMPAT_UNKNOWN_LEGACY,
    registry_db: str | Path | None = None,
    write_registry: bool = False,
) -> dict[str, Any]:
    """Dry-run (default) or apply legacy certification.

    Apply requires ``apply=True`` and a non-empty operator reason.
    Never rebuilds or rewrites vectors.
    """
    persist_path = Path(persist).resolve()
    if not isinstance(evidence_manifest, Mapping):
        raise CertificationEvidenceError("evidence_manifest must be an object")

    reason = (operator_reason or evidence_manifest.get("operator_reason") or "").strip()
    decision_raw = evidence_manifest.get("decision") or {}
    decision_name = (
        decision_raw.get("decision")
        if isinstance(decision_raw, Mapping)
        else None
    )
    target_expected = dict(evidence_manifest.get("target") or {})
    if expected_vector_count is not None:
        target_expected["vector_count"] = int(expected_vector_count)
    if expected_structural_fingerprint is not None:
        target_expected["structural_fingerprint"] = expected_structural_fingerprint

    # Always re-inspect (TOCTOU for dry-run reporting and apply).
    inspection = inspect_legacy_target(
        persist_path,
        physical_collection_name=str(
            target_expected.get("physical_collection_name") or DEFAULT_PHYSICAL_COLLECTION
        ),
    )
    proposed_writes = {
        "sidecar_v1": str(sidecar_v1_path(persist_path)),
        "certification_audit": str(certification_audit_path(persist_path)),
        "registry": str(registry_db) if write_registry and registry_db else None,
        "vectors_rewritten": False,
        "embedded_json_rewritten": False,
    }

    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "persist_dir": str(persist_path),
        "inspection": inspection,
        "decision": decision_raw,
        "operator_reason": reason,
        "actor": actor,
        "proposed_writes": proposed_writes,
        "applied": False,
        "idempotent_noop": False,
        "rollback_containment": (
            "Certification writes authority + audit only. "
            "Vectors/tracker unchanged. Containment: revoke via future explicit "
            "invalidation keeping audit history; do not silently rewrite audits. "
            "Rollback to pre-cert: remove sidecar v1 only after recording revocation "
            "event (operations phase)."
        ),
    }

    if decision_name != DEC_CERTIFIABLE:
        report["blocked"] = True
        report["block_reason"] = f"manifest decision is {decision_name}, not CERTIFIABLE"
        if apply:
            raise LegacyIndexNotCertifiableError(
                report["block_reason"], details=report
            )
        return report

    if not reason:
        report["blocked"] = True
        report["block_reason"] = "non-empty operator_reason required"
        if apply:
            raise CertificationEvidenceError(report["block_reason"], details=report)
        return report

    # Target binding checks
    try:
        verify_target_unchanged(
            persist_path,
            target_expected,
            physical_collection_name=str(
                target_expected.get("physical_collection_name")
                or DEFAULT_PHYSICAL_COLLECTION
            ),
        )
    except CertificationTargetChangedError:
        if apply:
            raise
        report["blocked"] = True
        report["block_reason"] = "target binding mismatch (dry-run report)"
        report["target_actual"] = inspection["target"]
        report["target_expected"] = target_expected
        return report

    if inspection["compatibility"].get("state") != expected_compatibility_state:
        # Allow already-compatible exact match for idempotent apply.
        existing = load_authoritative_state(
            persist_path,
            registry_db=registry_db,
            physical_collection_name=str(
                target_expected.get("physical_collection_name")
                or DEFAULT_PHYSICAL_COLLECTION
            ),
        )
        hist_ifp = (decision_raw or {}).get("historical_index_fingerprint")
        if not (
            inspection["compatibility"].get("state") == COMPAT_KNOWN_COMPATIBLE
            and existing is not None
            and existing.index_fingerprint == hist_ifp
        ):
            msg = (
                f"compatibility state is {inspection['compatibility'].get('state')}, "
                f"expected {expected_compatibility_state}"
            )
            report["blocked"] = True
            report["block_reason"] = msg
            if apply:
                raise CertificationTargetChangedError(msg, details=report)
            return report

    envelope = evidence_manifest.get("proposed_authority_envelope")
    if not isinstance(envelope, Mapping):
        raise CertificationEvidenceError("manifest missing proposed_authority_envelope")
    StoredIndexFingerprint.from_mapping(envelope, source="sidecar_v1")

    # Existing authority conflict / idempotency
    existing_auth = load_authoritative_state(
        persist_path,
        registry_db=registry_db,
        physical_collection_name=str(envelope["physical_collection_name"]),
    )
    existing_audit = _read_audit(persist_path)
    manifest_hash = evidence_manifest.get("manifest_hash")

    if existing_auth is not None:
        if existing_auth.index_fingerprint != envelope["index_fingerprint"]:
            raise CertificationConflictError(
                "conflicting fingerprint authority already present",
                details={
                    "existing": existing_auth.index_fingerprint,
                    "proposed": envelope["index_fingerprint"],
                },
            )
        # Exact same fingerprint — idempotent if audit matches or absent.
        if (
            existing_audit
            and existing_audit.get("index_fingerprint") == envelope["index_fingerprint"]
            and (
                manifest_hash is None
                or existing_audit.get("evidence_manifest_hash") == manifest_hash
            )
        ):
            report["idempotent_noop"] = True
            report["applied"] = False
            if not apply:
                return report
            return report

    if existing_audit and existing_audit.get("index_fingerprint") not in {
        None,
        envelope["index_fingerprint"],
    }:
        raise CertificationConflictError(
            "conflicting certification audit already present",
            details={
                "existing": existing_audit.get("index_fingerprint"),
                "proposed": envelope["index_fingerprint"],
            },
        )

    if not apply:
        report["blocked"] = False
        report["would_apply"] = True
        return report

    # Explicit apply path
    if apply is not True:
        raise CertificationRequiresOperatorApprovalError(
            "apply must be exactly True for mutation"
        )

    audit = {
        "schema_version": CERTIFICATION_AUDIT_SCHEMA,
        "previous_state": COMPAT_UNKNOWN_LEGACY,
        "new_state": COMPAT_KNOWN_COMPATIBLE,
        "index_fingerprint": envelope["index_fingerprint"],
        "embedding_fingerprint": envelope["embedding_fingerprint"],
        "corpus_fingerprint": envelope["corpus_fingerprint"],
        "evidence_manifest_hash": manifest_hash,
        "operator_reason": reason,
        "actor": actor,
        "certified_at": _utc_now(),
        "target_binding": inspection["target"],
        "note": (
            "Certification timestamp is when proof was accepted, "
            "not when vectors were created."
        ),
    }

    # Write authority then audit (authority first so crash mid-way leaves
    # recoverable state: missing audit with matching authority is inspectable).
    write_sidecar_v1(persist_path, dict(envelope))
    if write_registry and registry_db is not None:
        write_registry_fingerprint(registry_db, dict(envelope))
    from rag_engine.index_compatibility.state import _atomic_write_json

    _atomic_write_json(certification_audit_path(persist_path), audit)

    after = evaluate_compatibility(
        persist_path,
        registry_db=registry_db if write_registry else None,
        physical_collection_name=str(envelope["physical_collection_name"]),
        vector_count=inspection["target"]["vector_count"],
    )
    report["applied"] = True
    report["compatibility_after"] = after.to_dict()
    report["audit"] = audit
    return report


def circumstantial_evidence_from_v0_and_runtime(
    *,
    embed_model: str,
    embedding_dimension: int,
    chunk_size: int,
    chunk_overlap: int,
) -> list[EvidenceItem]:
    """Helper: Level-C-only evidence package (must NOT certify)."""
    return [
        EvidenceItem(
            field="embedding_model",
            value=embed_model,
            kind=EVIDENCE_KIND_INFERENCE,
            level=EVIDENCE_LEVEL_C,
            source="runtime_or_v0_alias",
            notes="mutable alias; not immutable revision",
        ),
        EvidenceItem(
            field="embedding_dimension",
            value=embedding_dimension,
            kind=EVIDENCE_KIND_DERIVED,
            level=EVIDENCE_LEVEL_C,
            source="chroma_collection_dimension",
            notes="dimension is necessary but not sufficient",
        ),
        EvidenceItem(
            field="chunk_size",
            value=chunk_size,
            kind=EVIDENCE_KIND_INFERENCE,
            level=EVIDENCE_LEVEL_C,
            source="v0_or_current_defaults",
        ),
        EvidenceItem(
            field="chunk_overlap",
            value=chunk_overlap,
            kind=EVIDENCE_KIND_INFERENCE,
            level=EVIDENCE_LEVEL_C,
            source="v0_or_current_defaults",
        ),
    ]


def strong_evidence_for_contract(
    emb: EmbeddingFingerprintSpec,
    corp: CorpusFingerprintSpec,
    idx: IndexFingerprintSpec,
    *,
    source: str = "synthetic_build_manifest",
    git_commit: str | None = None,
    model_digest: str | None = None,
) -> list[EvidenceItem]:
    """Construct Level-A evidence covering all required fields (tests / tooling)."""
    items: list[EvidenceItem] = []
    emb_c = emb.to_contract()
    corp_c = corp.to_contract()
    idx_c = idx.to_contract()
    ref = {
        "git_commit": git_commit,
        "model_digest": model_digest,
        "source": source,
    }
    for field_name in REQUIRED_EMBEDDING_FIELDS:
        items.append(
            EvidenceItem(
                field=field_name,
                value=emb_c[field_name],
                kind=EVIDENCE_KIND_FACT,
                level=EVIDENCE_LEVEL_A,
                source=source,
                reference=ref,
            )
        )
    for field_name in REQUIRED_CORPUS_FIELDS:
        items.append(
            EvidenceItem(
                field=field_name,
                value=corp_c[field_name],
                kind=EVIDENCE_KIND_FACT,
                level=EVIDENCE_LEVEL_A,
                source=source,
                reference=ref,
            )
        )
    for field_name in REQUIRED_INDEX_FIELDS:
        items.append(
            EvidenceItem(
                field=field_name,
                value=idx_c[field_name],
                kind=EVIDENCE_KIND_FACT,
                level=EVIDENCE_LEVEL_A,
                source=source,
                reference=ref,
            )
        )
    items.append(
        EvidenceItem(
            field="mixed_history_exclusion",
            value=True,
            kind=EVIDENCE_KIND_FACT,
            level=EVIDENCE_LEVEL_A,
            source=source,
            reference=ref,
            notes="build manifest proves single contract for entire collection",
        )
    )
    return items

"""Read-only quarantine-delete approval-artifact validator (DELETE Phase A).

Pure validation only: no filesystem mutation, registry writes, resolver calls,
approval issuance, or executor behavior.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from rag_engine.governed_delete.quarantine_preflight import QuarantineDeleteEvidence
from rag_engine.library_state.contract import (
    CLASS_EXACT_DUPLICATE,
    EMBEDDING_NONE,
    INTENT_PLAN_DELETE,
    OP_RETIREMENT_PROPOSAL,
)
from rag_engine.library_state.plan import OperationPlan, TargetClassification, canonical_json
from rag_engine.stable_identity import PathNormalizationError, normalize_relative_path

_SCHEMA_VERSION = 1
_OPERATION = "QUARANTINE_DELETE"
_MAX_LIFETIME = timedelta(hours=24)

_REGISTRY_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)
_CANONICAL_APPROVAL_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

_MANDATORY_FIELDS = frozenset(
    {
        "schema_version",
        "approval_id",
        "operation",
        "intent",
        "request_id",
        "plan_digest",
        "target_path",
        "retained_path",
        "quarantine_path",
        "document_id",
        "source_hash",
        "resolver_classification",
        "proposed_operation",
        "expected_embedding_action",
        "expected_new_vectors",
        "approved_vector_ids",
        "retained_aliases",
        "registry_db_path",
        "library_root",
        "persist_dir",
        "tracker_path",
        "issued_at",
        "expires_at",
        "approval_digest",
    }
)

_INTAKE_FORBIDDEN_KEYS = frozenset(
    {
        "approved",
        "approved_at_utc",
        "approved_by",
        "records_path",
        "records_sha256",
        "plan_path",
        "plan_sha256",
        "validator",
    }
)

_MOVE_FORBIDDEN_KEYS = frozenset(
    {
        "source_path",
        "destination_path",
        "affected_source_file_ids",
    }
)

_DELETE_ELIGIBLE: dict[str, str] = {
    CLASS_EXACT_DUPLICATE: OP_RETIREMENT_PROPOSAL,
}


class QuarantineDeleteApprovalValidationError(ValueError):
    """Raised when a quarantine-delete approval artifact fails validation."""


@dataclass(frozen=True)
class QuarantineDeleteApprovalContext:
    """Approved store paths for quarantine-delete validation."""

    registry_db_path: str
    library_root: str
    persist_dir: str
    tracker_path: str


@dataclass(frozen=True)
class QuarantineDeleteApprovalValidationResult:
    """Structured outcome for a validated quarantine-delete approval artifact."""

    approval_id: str
    request_id: str
    plan_digest: str
    approval_digest: str
    target_path: str
    retained_path: str
    quarantine_path: str
    document_id: str
    source_hash: str
    resolver_classification: str
    proposed_operation: str


def plan_digest(plan: OperationPlan) -> str:
    """SHA-256 of the plan canonical JSON (lowercase hex)."""
    return hashlib.sha256(plan.to_canonical_json().encode("utf-8")).hexdigest()


def approval_digest_payload(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Binding fields for ``approval_digest`` recomputation (excludes digest itself)."""
    return {key: artifact[key] for key in sorted(artifact) if key != "approval_digest"}


def compute_approval_digest(artifact: Mapping[str, Any]) -> str:
    """Recompute ``approval_digest`` from canonical JSON of binding fields."""
    payload = approval_digest_payload(artifact)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def validate_quarantine_delete_approval(
    artifact: Mapping[str, Any],
    plan: OperationPlan,
    *,
    target_path: str,
    retained_path: str,
    quarantine_path: str,
    context: QuarantineDeleteApprovalContext,
    preflight_evidence: QuarantineDeleteEvidence,
    now_utc: str,
) -> QuarantineDeleteApprovalValidationResult:
    """Validate a quarantine-delete approval artifact against preflight evidence."""
    _require_mapping(artifact)
    _reject_foreign_shapes(artifact)
    _require_exact_field_set(artifact)
    _validate_now_timestamp(now_utc)
    normalized_target = _validate_library_relative_path(target_path, field="target_path")
    normalized_retained = _validate_library_relative_path(retained_path, field="retained_path")
    normalized_quarantine = _validate_library_relative_path(
        quarantine_path,
        field="quarantine_path",
    )
    if len({normalized_target, normalized_retained, normalized_quarantine}) != 3:
        raise QuarantineDeleteApprovalValidationError(
            "target_path, retained_path, and quarantine_path must differ"
        )
    _validate_delete_eligible_plan(plan)
    _validate_preflight_binding(
        preflight_evidence,
        plan=plan,
        target_path=normalized_target,
        retained_path=normalized_retained,
        quarantine_path=normalized_quarantine,
        context=context,
    )

    _validate_schema_version(artifact["schema_version"])
    _validate_non_empty_str(artifact["approval_id"], "approval_id")
    _validate_closed_string(artifact["operation"], "operation", {_OPERATION})
    _validate_closed_string(artifact["intent"], "intent", {INTENT_PLAN_DELETE})
    _validate_path_binding(artifact["target_path"], normalized_target, "target_path")
    _validate_path_binding(artifact["retained_path"], normalized_retained, "retained_path")
    _validate_path_binding(
        artifact["quarantine_path"],
        normalized_quarantine,
        "quarantine_path",
    )

    expected = _expected_bindings(
        plan,
        target_path=normalized_target,
        retained_path=normalized_retained,
        quarantine_path=normalized_quarantine,
        context=context,
        preflight_evidence=preflight_evidence,
    )
    _validate_exact_binding(artifact["request_id"], expected["request_id"], "request_id")
    _validate_exact_binding(artifact["plan_digest"], expected["plan_digest"], "plan_digest")
    _validate_exact_binding(artifact["document_id"], expected["document_id"], "document_id")
    _validate_exact_binding(artifact["source_hash"], expected["source_hash"], "source_hash")
    _validate_exact_binding(
        artifact["resolver_classification"],
        expected["resolver_classification"],
        "resolver_classification",
    )
    _validate_exact_binding(
        artifact["proposed_operation"],
        expected["proposed_operation"],
        "proposed_operation",
    )
    _validate_closed_string(
        artifact["expected_embedding_action"],
        "expected_embedding_action",
        {EMBEDDING_NONE},
    )
    _validate_expected_new_vectors(artifact["expected_new_vectors"])
    _validate_string_list_binding(
        artifact["approved_vector_ids"],
        expected["approved_vector_ids"],
        "approved_vector_ids",
    )
    _validate_string_list_binding(
        artifact["retained_aliases"],
        expected["retained_aliases"],
        "retained_aliases",
    )
    _validate_absolute_path_binding(
        artifact["registry_db_path"],
        expected["registry_db_path"],
        "registry_db_path",
    )
    _validate_absolute_path_binding(
        artifact["library_root"],
        expected["library_root"],
        "library_root",
    )
    _validate_absolute_path_binding(
        artifact["persist_dir"],
        expected["persist_dir"],
        "persist_dir",
    )
    _validate_absolute_path_binding(
        artifact["tracker_path"],
        expected["tracker_path"],
        "tracker_path",
    )
    issued_at = _validate_artifact_timestamp(artifact["issued_at"], "issued_at")
    expires_at = _validate_artifact_timestamp(artifact["expires_at"], "expires_at")
    _validate_lifetime(issued_at, expires_at, now_utc=now_utc)
    digest = _validate_approval_digest_field(artifact["approval_digest"])
    recomputed = compute_approval_digest(artifact)
    if digest != recomputed:
        raise QuarantineDeleteApprovalValidationError("approval_digest mismatch")

    return QuarantineDeleteApprovalValidationResult(
        approval_id=str(artifact["approval_id"]),
        request_id=str(artifact["request_id"]),
        plan_digest=str(artifact["plan_digest"]),
        approval_digest=digest,
        target_path=normalized_target,
        retained_path=normalized_retained,
        quarantine_path=normalized_quarantine,
        document_id=str(artifact["document_id"]),
        source_hash=str(artifact["source_hash"]),
        resolver_classification=str(artifact["resolver_classification"]),
        proposed_operation=str(artifact["proposed_operation"]),
    )


def _reject_foreign_shapes(artifact: Mapping[str, Any]) -> None:
    if any(key in artifact for key in _INTAKE_FORBIDDEN_KEYS):
        raise QuarantineDeleteApprovalValidationError(
            "Intake approval.json shape is not a quarantine-delete approval artifact"
        )
    if any(key in artifact for key in _MOVE_FORBIDDEN_KEYS):
        raise QuarantineDeleteApprovalValidationError(
            "MOVE approval shape is not a quarantine-delete approval artifact"
        )
    if artifact.get("operation") == "MOVE":
        raise QuarantineDeleteApprovalValidationError(
            "MOVE approval shape is not a quarantine-delete approval artifact"
        )


def _require_mapping(artifact: Mapping[str, Any]) -> None:
    if not isinstance(artifact, Mapping):
        raise QuarantineDeleteApprovalValidationError("artifact must be a mapping")


def _require_exact_field_set(artifact: Mapping[str, Any]) -> None:
    keys = set(artifact.keys())
    missing = sorted(_MANDATORY_FIELDS - keys)
    if missing:
        raise QuarantineDeleteApprovalValidationError(
            f"missing mandatory fields: {', '.join(missing)}"
        )
    unknown = sorted(keys - _MANDATORY_FIELDS)
    if unknown:
        raise QuarantineDeleteApprovalValidationError(f"unknown fields: {', '.join(unknown)}")


def _validate_now_timestamp(now_utc: str) -> None:
    _parse_utc_timestamp(now_utc, "now_utc")


def _validate_delete_eligible_plan(plan: OperationPlan) -> None:
    if plan.intent != INTENT_PLAN_DELETE:
        raise QuarantineDeleteApprovalValidationError(
            f"plan intent must be {INTENT_PLAN_DELETE!r}; got {plan.intent!r}"
        )
    allowed_operation = _DELETE_ELIGIBLE.get(plan.classification)
    if allowed_operation is None:
        raise QuarantineDeleteApprovalValidationError(
            "plan classification is not quarantine-delete eligible: "
            f"{plan.classification!r}"
        )
    if plan.proposed_operation != allowed_operation:
        raise QuarantineDeleteApprovalValidationError(
            "plan proposed_operation is not quarantine-delete eligible: "
            f"{plan.proposed_operation!r}"
        )
    if plan.embedding_action != EMBEDDING_NONE:
        raise QuarantineDeleteApprovalValidationError(
            f"plan embedding_action must be {EMBEDDING_NONE!r}"
        )


def _validate_preflight_binding(
    evidence: QuarantineDeleteEvidence,
    *,
    plan: OperationPlan,
    target_path: str,
    retained_path: str,
    quarantine_path: str,
    context: QuarantineDeleteApprovalContext,
) -> None:
    if evidence.target_path != target_path:
        raise QuarantineDeleteApprovalValidationError("preflight target_path mismatch")
    if evidence.retained_path != retained_path:
        raise QuarantineDeleteApprovalValidationError("preflight retained_path mismatch")
    if evidence.quarantine_path != quarantine_path:
        raise QuarantineDeleteApprovalValidationError("preflight quarantine_path mismatch")
    if evidence.plan.request_id != plan.request_id:
        raise QuarantineDeleteApprovalValidationError("preflight request_id mismatch")
    if plan_digest(evidence.plan) != plan_digest(plan):
        raise QuarantineDeleteApprovalValidationError("preflight plan_digest mismatch")
    if evidence.resolver_classification != CLASS_EXACT_DUPLICATE:
        raise QuarantineDeleteApprovalValidationError(
            "preflight resolver_classification must be EXACT_DUPLICATE"
        )
    if evidence.proposed_operation != OP_RETIREMENT_PROPOSAL:
        raise QuarantineDeleteApprovalValidationError(
            "preflight proposed_operation must be RETIREMENT_PROPOSAL"
        )
    if _normalize_absolute_path(context.library_root, "library_root") != evidence.library_root:
        raise QuarantineDeleteApprovalValidationError("preflight library_root mismatch")
    if _normalize_absolute_path(context.persist_dir, "persist_dir") != evidence.persist_dir:
        raise QuarantineDeleteApprovalValidationError("preflight persist_dir mismatch")
    if _normalize_absolute_path(context.registry_db_path, "registry_db_path") != evidence.registry_db:
        raise QuarantineDeleteApprovalValidationError("preflight registry_db mismatch")
    if evidence.tracker_path is not None:
        if _normalize_absolute_path(context.tracker_path, "tracker_path") != evidence.tracker_path:
            raise QuarantineDeleteApprovalValidationError("preflight tracker_path mismatch")


def _classification_for_target(
    plan: OperationPlan,
    *,
    target_path: str,
) -> TargetClassification:
    matches = [c for c in plan.classifications if c.target == target_path]
    if len(matches) == 1:
        return matches[0]
    if len(plan.classifications) == 1:
        only = plan.classifications[0]
        if only.target == target_path:
            return only
    raise QuarantineDeleteApprovalValidationError(
        f"plan has no classification for target_path {target_path!r}"
    )


def _expected_bindings(
    plan: OperationPlan,
    *,
    target_path: str,
    retained_path: str,
    quarantine_path: str,
    context: QuarantineDeleteApprovalContext,
    preflight_evidence: QuarantineDeleteEvidence,
) -> dict[str, Any]:
    item = _classification_for_target(plan, target_path=target_path)
    if item.document_id is None:
        raise QuarantineDeleteApprovalValidationError(
            "plan classification missing document_id for target_path"
        )
    if item.source_hash is None:
        raise QuarantineDeleteApprovalValidationError(
            "plan classification missing source_hash for target_path"
        )
    if item.source_hash != preflight_evidence.source_hash:
        raise QuarantineDeleteApprovalValidationError("preflight source_hash mismatch")
    if str(item.document_id) != preflight_evidence.document_id:
        raise QuarantineDeleteApprovalValidationError("preflight document_id mismatch")
    if item.expected_new_vectors not in (0, None):
        raise QuarantineDeleteApprovalValidationError(
            "plan classification expected_new_vectors must be 0 for quarantine delete"
        )
    return {
        "request_id": plan.request_id,
        "plan_digest": plan_digest(plan),
        "document_id": preflight_evidence.document_id,
        "source_hash": preflight_evidence.source_hash,
        "resolver_classification": preflight_evidence.resolver_classification,
        "proposed_operation": preflight_evidence.proposed_operation,
        "approved_vector_ids": list(preflight_evidence.approved_vector_ids),
        "retained_aliases": list(preflight_evidence.retained_aliases),
        "registry_db_path": _normalize_absolute_path(
            context.registry_db_path,
            "registry_db_path",
        ),
        "library_root": _normalize_absolute_path(context.library_root, "library_root"),
        "persist_dir": _normalize_absolute_path(context.persist_dir, "persist_dir"),
        "tracker_path": _normalize_absolute_path(context.tracker_path, "tracker_path"),
        "target_path": target_path,
        "retained_path": retained_path,
        "quarantine_path": quarantine_path,
    }


def _validate_schema_version(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QuarantineDeleteApprovalValidationError("schema_version must be integer 1")
    if value != _SCHEMA_VERSION:
        raise QuarantineDeleteApprovalValidationError(
            f"schema_version must be {_SCHEMA_VERSION}; got {value!r}"
        )


def _validate_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise QuarantineDeleteApprovalValidationError(f"{field} must be a non-empty string")
    if "\0" in value:
        raise QuarantineDeleteApprovalValidationError(f"{field} must not contain NUL")
    stripped = value.strip()
    if not stripped:
        raise QuarantineDeleteApprovalValidationError(f"{field} must be a non-empty string")
    if stripped != value:
        raise QuarantineDeleteApprovalValidationError(
            f"{field} must not contain leading/trailing whitespace"
        )
    return stripped


def _validate_closed_string(value: Any, field: str, allowed: set[str]) -> None:
    text = _validate_non_empty_str(value, field)
    if text not in allowed:
        allowed_display = ", ".join(sorted(allowed))
        raise QuarantineDeleteApprovalValidationError(
            f"{field} must be one of {{{allowed_display}}}; got {text!r}"
        )


def _validate_exact_binding(value: Any, expected: str, field: str) -> None:
    text = _validate_non_empty_str(value, field)
    if text != expected:
        raise QuarantineDeleteApprovalValidationError(f"{field} mismatch")


def _validate_path_binding(value: Any, expected: str, field: str) -> None:
    text = _validate_library_relative_path(str(value), field=field)
    if text != expected:
        raise QuarantineDeleteApprovalValidationError(f"{field} mismatch")


def _validate_library_relative_path(raw: str, *, field: str) -> str:
    if not isinstance(raw, str):
        raise QuarantineDeleteApprovalValidationError(f"{field} must be a relative path string")
    if "\0" in raw:
        raise QuarantineDeleteApprovalValidationError(f"{field} must not contain NUL")
    if not raw.strip():
        raise QuarantineDeleteApprovalValidationError(f"{field} must be non-empty")
    if raw != raw.strip():
        raise QuarantineDeleteApprovalValidationError(
            f"{field} must not contain leading/trailing whitespace"
        )
    if raw.startswith("/") or raw.startswith("\\"):
        raise QuarantineDeleteApprovalValidationError(f"{field} must not be absolute")
    if ".." in raw.replace("\\", "/").split("/"):
        raise QuarantineDeleteApprovalValidationError(
            f"{field} must not contain traversal segments"
        )
    try:
        return normalize_relative_path(raw)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        raise QuarantineDeleteApprovalValidationError(f"invalid {field}: {exc}") from exc


def _normalize_absolute_path(raw: str, field: str) -> str:
    text = _validate_non_empty_str(raw, field)
    path = Path(text)
    if not path.is_absolute():
        raise QuarantineDeleteApprovalValidationError(f"{field} must be an absolute path")
    return str(path)


def _validate_absolute_path_binding(value: Any, expected: str, field: str) -> None:
    text = _normalize_absolute_path(str(value), field)
    if text != expected:
        raise QuarantineDeleteApprovalValidationError(f"{field} mismatch")


def _validate_expected_new_vectors(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QuarantineDeleteApprovalValidationError("expected_new_vectors must be integer 0")
    if value != 0:
        raise QuarantineDeleteApprovalValidationError("expected_new_vectors must be 0")


def _validate_string_list_binding(value: Any, expected: list[str], field: str) -> None:
    if not isinstance(value, list):
        raise QuarantineDeleteApprovalValidationError(f"{field} must be a JSON array")
    if len(value) != len(expected):
        raise QuarantineDeleteApprovalValidationError(f"{field} mismatch")
    normalized = [_validate_non_empty_str(item, f"{field}[]") for item in value]
    if normalized != expected:
        raise QuarantineDeleteApprovalValidationError(f"{field} mismatch")


def _validate_artifact_timestamp(value: Any, field: str) -> datetime:
    text = _validate_non_empty_str(value, field)
    return _parse_utc_timestamp(text, field)


def _parse_utc_timestamp(value: str, field: str) -> datetime:
    if not _REGISTRY_TIMESTAMP_RE.match(value):
        raise QuarantineDeleteApprovalValidationError(
            f"{field} must be UTC ISO-8601 with Z suffix and second resolution"
        )
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _validate_lifetime(
    issued_at: datetime,
    expires_at: datetime,
    *,
    now_utc: str,
) -> None:
    now = _parse_utc_timestamp(now_utc, "now_utc")
    if expires_at <= issued_at:
        raise QuarantineDeleteApprovalValidationError("expires_at must be strictly after issued_at")
    if expires_at - issued_at > _MAX_LIFETIME:
        raise QuarantineDeleteApprovalValidationError("approval lifetime must not exceed 24 hours")
    if now < issued_at:
        raise QuarantineDeleteApprovalValidationError("approval is not yet valid")
    if now > expires_at:
        raise QuarantineDeleteApprovalValidationError("approval has expired")


def _validate_approval_digest_field(value: Any) -> str:
    if not isinstance(value, str):
        raise QuarantineDeleteApprovalValidationError(
            "approval_digest must be a canonical SHA-256 hex digest"
        )
    if value != value.strip():
        raise QuarantineDeleteApprovalValidationError(
            "approval_digest must not contain leading or trailing whitespace"
        )
    if not _CANONICAL_APPROVAL_DIGEST_RE.fullmatch(value):
        raise QuarantineDeleteApprovalValidationError(
            "approval_digest must be exactly 64 lowercase hexadecimal characters"
        )
    return value

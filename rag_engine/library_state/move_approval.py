"""Read-only MOVE approval-artifact validator (Roadmap v7 Phase C).

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

from rag_engine.library_state.contract import (
    CLASS_INDEXED_OK,
    CLASS_SAME_BYTES_MOVED,
    EMBEDDING_NONE,
    INTENT_PLAN_MOVE,
    OP_NO_OP,
)
from rag_engine.library_state.move_preflight import PreMoveEvidence
from rag_engine.library_state.plan import OperationPlan, TargetClassification, canonical_json
from rag_engine.stable_identity import PathNormalizationError, normalize_relative_path

_SCHEMA_VERSION = 1
_OPERATION = "MOVE"
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
        "source_path",
        "destination_path",
        "document_id",
        "source_hash",
        "resolver_classification",
        "proposed_operation",
        "expected_embedding_action",
        "expected_new_vectors",
        "affected_source_file_ids",
        "registry_db_path",
        "library_root",
        "persist_dir",
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

_MOVE_ELIGIBLE: dict[str, str] = {
    CLASS_INDEXED_OK: OP_NO_OP,
}


class MoveApprovalValidationError(ValueError):
    """Raised when a MOVE approval artifact fails validation."""


@dataclass(frozen=True)
class MoveApprovalContext:
    """Approved evidence not represented on ``OperationPlan``."""

    affected_source_file_ids: tuple[str, str]
    registry_db_path: str
    library_root: str
    persist_dir: str


@dataclass(frozen=True)
class MoveApprovalValidationResult:
    """Structured outcome for a validated MOVE approval artifact."""

    approval_id: str
    request_id: str
    plan_digest: str
    approval_digest: str
    source_path: str
    destination_path: str
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


def validate_move_approval(
    artifact: Mapping[str, Any],
    plan: OperationPlan,
    *,
    source_path: str,
    destination_path: str,
    context: MoveApprovalContext,
    pre_move_evidence: PreMoveEvidence,
    now_utc: str,
) -> MoveApprovalValidationResult:
    """Validate a MOVE approval artifact against pre-move evidence and bindings."""
    _require_mapping(artifact)
    _reject_intake_shape(artifact)
    _require_exact_field_set(artifact)
    _validate_now_timestamp(now_utc)
    normalized_source = _validate_library_relative_path(source_path, field="source_path")
    normalized_destination = _validate_library_relative_path(
        destination_path,
        field="destination_path",
    )
    if normalized_source == normalized_destination:
        raise MoveApprovalValidationError(
            "source_path and destination_path must differ"
        )
    _validate_move_eligible_plan(plan)
    _validate_pre_move_evidence_binding(
        pre_move_evidence,
        plan=plan,
        source_path=normalized_source,
        destination_path=normalized_destination,
        context=context,
    )

    _validate_schema_version(artifact["schema_version"])
    _validate_non_empty_str(artifact["approval_id"], "approval_id")
    _validate_closed_string(artifact["operation"], "operation", {_OPERATION})
    _validate_closed_string(artifact["intent"], "intent", {INTENT_PLAN_MOVE})
    _validate_path_binding(artifact["source_path"], normalized_source, "source_path")
    _validate_path_binding(
        artifact["destination_path"],
        normalized_destination,
        "destination_path",
    )

    expected = _expected_bindings(
        plan,
        source_path=normalized_source,
        destination_path=normalized_destination,
        context=context,
        pre_move_evidence=pre_move_evidence,
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
    _validate_affected_source_file_ids(
        artifact["affected_source_file_ids"],
        expected["affected_source_file_ids"],
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
    issued_at = _validate_artifact_timestamp(artifact["issued_at"], "issued_at")
    expires_at = _validate_artifact_timestamp(artifact["expires_at"], "expires_at")
    _validate_lifetime(issued_at, expires_at, now_utc=now_utc)
    digest = _validate_approval_digest_field(artifact["approval_digest"])
    recomputed = compute_approval_digest(artifact)
    if digest != recomputed:
        raise MoveApprovalValidationError("approval_digest mismatch")

    return MoveApprovalValidationResult(
        approval_id=str(artifact["approval_id"]),
        request_id=str(artifact["request_id"]),
        plan_digest=str(artifact["plan_digest"]),
        approval_digest=digest,
        source_path=normalized_source,
        destination_path=normalized_destination,
        document_id=str(artifact["document_id"]),
        source_hash=str(artifact["source_hash"]),
        resolver_classification=str(artifact["resolver_classification"]),
        proposed_operation=str(artifact["proposed_operation"]),
    )


def _reject_intake_shape(artifact: Mapping[str, Any]) -> None:
    if any(key in artifact for key in _INTAKE_FORBIDDEN_KEYS):
        raise MoveApprovalValidationError(
            "Intake approval.json shape is not a MOVE approval artifact"
        )


def _require_mapping(artifact: Mapping[str, Any]) -> None:
    if not isinstance(artifact, Mapping):
        raise MoveApprovalValidationError("artifact must be a mapping")


def _require_exact_field_set(artifact: Mapping[str, Any]) -> None:
    keys = set(artifact.keys())
    missing = sorted(_MANDATORY_FIELDS - keys)
    if missing:
        raise MoveApprovalValidationError(
            f"missing mandatory fields: {', '.join(missing)}"
        )
    unknown = sorted(keys - _MANDATORY_FIELDS)
    if unknown:
        raise MoveApprovalValidationError(f"unknown fields: {', '.join(unknown)}")


def _validate_now_timestamp(now_utc: str) -> None:
    _parse_utc_timestamp(now_utc, "now_utc")


def _validate_move_eligible_plan(plan: OperationPlan) -> None:
    if plan.intent != INTENT_PLAN_MOVE:
        raise MoveApprovalValidationError(
            f"plan intent must be {INTENT_PLAN_MOVE!r}; got {plan.intent!r}"
        )
    if plan.classification == CLASS_SAME_BYTES_MOVED:
        raise MoveApprovalValidationError(
            "SAME_BYTES_MOVED is post-move reconciliation evidence, not pre-move approval"
        )
    allowed_operation = _MOVE_ELIGIBLE.get(plan.classification)
    if allowed_operation is None:
        raise MoveApprovalValidationError(
            "plan classification is not pre-move eligible: "
            f"{plan.classification!r}"
        )
    if plan.proposed_operation != allowed_operation:
        raise MoveApprovalValidationError(
            "plan proposed_operation is not pre-move eligible: "
            f"{plan.proposed_operation!r}"
        )
    if plan.embedding_action != EMBEDDING_NONE:
        raise MoveApprovalValidationError(
            f"plan embedding_action must be {EMBEDDING_NONE!r}"
        )


def _validate_pre_move_evidence_binding(
    evidence: PreMoveEvidence,
    *,
    plan: OperationPlan,
    source_path: str,
    destination_path: str,
    context: MoveApprovalContext,
) -> None:
    if evidence.source_path != source_path:
        raise MoveApprovalValidationError("pre_move_evidence source_path mismatch")
    if evidence.destination_path != destination_path:
        raise MoveApprovalValidationError(
            "pre_move_evidence destination_path mismatch"
        )
    if evidence.plan.request_id != plan.request_id:
        raise MoveApprovalValidationError("pre_move_evidence request_id mismatch")
    if plan_digest(evidence.plan) != plan_digest(plan):
        raise MoveApprovalValidationError("pre_move_evidence plan_digest mismatch")
    if evidence.resolver_classification != CLASS_INDEXED_OK:
        raise MoveApprovalValidationError(
            "pre_move_evidence resolver_classification must be INDEXED_OK"
        )
    if evidence.proposed_operation != OP_NO_OP:
        raise MoveApprovalValidationError(
            "pre_move_evidence proposed_operation must be NO_OP"
        )
    if _normalize_absolute_path(context.library_root, "library_root") != evidence.library_root:
        raise MoveApprovalValidationError("pre_move_evidence library_root mismatch")
    if _normalize_absolute_path(context.persist_dir, "persist_dir") != evidence.persist_dir:
        raise MoveApprovalValidationError("pre_move_evidence persist_dir mismatch")
    if _normalize_absolute_path(context.registry_db_path, "registry_db_path") != evidence.registry_db:
        raise MoveApprovalValidationError("pre_move_evidence registry_db mismatch")


def _classification_for_target(
    plan: OperationPlan,
    *,
    source_path: str,
) -> TargetClassification:
    matches = [c for c in plan.classifications if c.target == source_path]
    if len(matches) == 1:
        return matches[0]
    if len(plan.classifications) == 1:
        only = plan.classifications[0]
        if only.target == source_path:
            return only
    raise MoveApprovalValidationError(
        f"plan has no classification for source_path {source_path!r}"
    )


def _expected_bindings(
    plan: OperationPlan,
    *,
    source_path: str,
    destination_path: str,
    context: MoveApprovalContext,
    pre_move_evidence: PreMoveEvidence,
) -> dict[str, Any]:
    item = _classification_for_target(plan, source_path=source_path)
    if item.document_id is None:
        raise MoveApprovalValidationError(
            "plan classification missing document_id for source_path"
        )
    if item.source_hash is None:
        raise MoveApprovalValidationError(
            "plan classification missing source_hash for source_path"
        )
    if item.source_hash != pre_move_evidence.source_hash:
        raise MoveApprovalValidationError("pre_move source_hash mismatch")
    if str(item.document_id) != pre_move_evidence.document_id:
        raise MoveApprovalValidationError("pre_move document_id mismatch")
    if item.expected_new_vectors not in (0, None):
        raise MoveApprovalValidationError(
            "plan classification expected_new_vectors must be 0 for MOVE"
        )
    affected = _normalize_affected_ids(context.affected_source_file_ids)
    return {
        "request_id": plan.request_id,
        "plan_digest": plan_digest(plan),
        "document_id": pre_move_evidence.document_id,
        "source_hash": pre_move_evidence.source_hash,
        "resolver_classification": pre_move_evidence.resolver_classification,
        "proposed_operation": pre_move_evidence.proposed_operation,
        "affected_source_file_ids": list(affected),
        "registry_db_path": _normalize_absolute_path(
            context.registry_db_path,
            "registry_db_path",
        ),
        "library_root": _normalize_absolute_path(context.library_root, "library_root"),
        "persist_dir": _normalize_absolute_path(context.persist_dir, "persist_dir"),
        "source_path": source_path,
        "destination_path": destination_path,
    }


def _normalize_affected_ids(raw: Any) -> tuple[str, str]:
    if not isinstance(raw, (list, tuple)):
        raise MoveApprovalValidationError(
            "affected_source_file_ids must be a list of exactly two distinct IDs"
        )
    if len(raw) != 2:
        raise MoveApprovalValidationError(
            "affected_source_file_ids must contain exactly two IDs"
        )
    first = _validate_non_empty_str(raw[0], "affected_source_file_ids[0]")
    second = _validate_non_empty_str(raw[1], "affected_source_file_ids[1]")
    if first == second:
        raise MoveApprovalValidationError(
            "affected_source_file_ids must contain two distinct IDs"
        )
    return first, second


def _validate_schema_version(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoveApprovalValidationError("schema_version must be integer 1")
    if value != _SCHEMA_VERSION:
        raise MoveApprovalValidationError(
            f"schema_version must be {_SCHEMA_VERSION}; got {value!r}"
        )


def _validate_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MoveApprovalValidationError(f"{field} must be a non-empty string")
    if "\0" in value:
        raise MoveApprovalValidationError(f"{field} must not contain NUL")
    stripped = value.strip()
    if not stripped:
        raise MoveApprovalValidationError(f"{field} must be a non-empty string")
    if stripped != value:
        raise MoveApprovalValidationError(f"{field} must not contain leading/trailing whitespace")
    return stripped


def _validate_closed_string(value: Any, field: str, allowed: set[str]) -> None:
    text = _validate_non_empty_str(value, field)
    if text not in allowed:
        allowed_display = ", ".join(sorted(allowed))
        raise MoveApprovalValidationError(
            f"{field} must be one of {{{allowed_display}}}; got {text!r}"
        )


def _validate_exact_binding(value: Any, expected: str, field: str) -> None:
    text = _validate_non_empty_str(value, field)
    if text != expected:
        raise MoveApprovalValidationError(f"{field} mismatch")


def _validate_path_binding(value: Any, expected: str, field: str) -> None:
    text = _validate_library_relative_path(str(value), field=field)
    if text != expected:
        raise MoveApprovalValidationError(f"{field} mismatch")


def _validate_library_relative_path(raw: str, *, field: str) -> str:
    if not isinstance(raw, str):
        raise MoveApprovalValidationError(f"{field} must be a relative path string")
    if "\0" in raw:
        raise MoveApprovalValidationError(f"{field} must not contain NUL")
    if not raw.strip():
        raise MoveApprovalValidationError(f"{field} must be non-empty")
    if raw != raw.strip():
        raise MoveApprovalValidationError(
            f"{field} must not contain leading/trailing whitespace"
        )
    if raw.startswith("/") or raw.startswith("\\"):
        raise MoveApprovalValidationError(f"{field} must not be absolute")
    if ".." in raw.replace("\\", "/").split("/"):
        raise MoveApprovalValidationError(f"{field} must not contain traversal segments")
    try:
        return normalize_relative_path(raw)
    except (PathNormalizationError, TypeError, ValueError) as exc:
        raise MoveApprovalValidationError(f"invalid {field}: {exc}") from exc


def _normalize_absolute_path(raw: str, field: str) -> str:
    text = _validate_non_empty_str(raw, field)
    path = Path(text)
    if not path.is_absolute():
        raise MoveApprovalValidationError(f"{field} must be an absolute path")
    return str(path)


def _validate_absolute_path_binding(value: Any, expected: str, field: str) -> None:
    text = _normalize_absolute_path(str(value), field)
    if text != expected:
        raise MoveApprovalValidationError(f"{field} mismatch")


def _validate_expected_new_vectors(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoveApprovalValidationError("expected_new_vectors must be integer 0")
    if value != 0:
        raise MoveApprovalValidationError("expected_new_vectors must be 0")


def _validate_affected_source_file_ids(value: Any, expected: list[str]) -> None:
    if not isinstance(value, list):
        raise MoveApprovalValidationError(
            "affected_source_file_ids must be a JSON array"
        )
    if len(value) != 2:
        raise MoveApprovalValidationError(
            "affected_source_file_ids must contain exactly two IDs"
        )
    first = _validate_non_empty_str(value[0], "affected_source_file_ids[0]")
    second = _validate_non_empty_str(value[1], "affected_source_file_ids[1]")
    if first == second:
        raise MoveApprovalValidationError(
            "affected_source_file_ids must contain two distinct IDs"
        )
    if [first, second] != expected:
        raise MoveApprovalValidationError("affected_source_file_ids mismatch")


def _validate_artifact_timestamp(value: Any, field: str) -> datetime:
    text = _validate_non_empty_str(value, field)
    return _parse_utc_timestamp(text, field)


def _parse_utc_timestamp(value: str, field: str) -> datetime:
    if not _REGISTRY_TIMESTAMP_RE.match(value):
        raise MoveApprovalValidationError(
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
        raise MoveApprovalValidationError("expires_at must be strictly after issued_at")
    if expires_at - issued_at > _MAX_LIFETIME:
        raise MoveApprovalValidationError("approval lifetime must not exceed 24 hours")
    if now < issued_at:
        raise MoveApprovalValidationError("approval is not yet valid")
    if now > expires_at:
        raise MoveApprovalValidationError("approval has expired")


def _validate_approval_digest_field(value: Any) -> str:
    if not isinstance(value, str):
        raise MoveApprovalValidationError(
            "approval_digest must be a canonical SHA-256 hex digest"
        )
    if value != value.strip():
        raise MoveApprovalValidationError(
            "approval_digest must not contain leading or trailing whitespace"
        )
    if not _CANONICAL_APPROVAL_DIGEST_RE.fullmatch(value):
        raise MoveApprovalValidationError(
            "approval_digest must be exactly 64 lowercase hexadecimal characters"
        )
    return value

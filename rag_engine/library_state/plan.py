"""Stable operation-plan schema for the read-only library state resolver."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from rag_engine.library_state.contract import (
    APPROVAL_CATEGORIES,
    CLASSIFICATIONS,
    EMBEDDING_ACTIONS,
    PROPOSED_OPERATIONS,
    RESULT_VOCABULARY,
    SUPPORTED_INTENTS,
)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no extra whitespace, UTF-8 safe."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_request_id(intent: str, targets: Sequence[str]) -> str:
    payload = {"intent": intent, "targets": list(targets)}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _require(value: str, allowed: frozenset[str], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"invalid {name}: {value!r}")
    return value


@dataclass(frozen=True)
class TargetClassification:
    target: str
    classification: str
    confidence: str
    proposed_operation: str
    approval: str
    embedding_action: str
    result: str
    expected_new_vectors: int | None = 0
    document_id: str | None = None
    source_hash: str | None = None
    subject_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(self.classification, CLASSIFICATIONS, "classification")
        _require(self.proposed_operation, PROPOSED_OPERATIONS, "proposed_operation")
        _require(self.approval, APPROVAL_CATEGORIES, "approval")
        _require(self.embedding_action, EMBEDDING_ACTIONS, "embedding_action")
        _require(self.result, RESULT_VOCABULARY, "result")

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "classification": self.classification,
            "confidence": self.confidence,
            "proposed_operation": self.proposed_operation,
            "approval": self.approval,
            "embedding_action": self.embedding_action,
            "result": self.result,
            "expected_new_vectors": self.expected_new_vectors,
            "document_id": self.document_id,
            "source_hash": self.source_hash,
            "subject_id": self.subject_id,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class OperationPlan:
    """Machine-readable Phase 1 plan. The resolver never executes it."""

    request_id: str
    intent: str
    targets: tuple[str, ...]
    classification: str
    authority_snapshot: Mapping[str, Any]
    proposed_operation: str
    approval: str
    embedding_action: str
    affected_stores: tuple[str, ...]
    risk_flags: tuple[str, ...]
    ambiguity_flags: tuple[str, ...]
    verification_contract: Mapping[str, Any]
    evidence_summary: str
    result: str
    classifications: tuple[TargetClassification, ...] = ()
    evidence_gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(self.intent, SUPPORTED_INTENTS, "intent")
        _require(self.classification, CLASSIFICATIONS, "classification")
        _require(self.proposed_operation, PROPOSED_OPERATIONS, "proposed_operation")
        _require(self.approval, APPROVAL_CATEGORIES, "approval")
        _require(self.embedding_action, EMBEDDING_ACTIONS, "embedding_action")
        _require(self.result, RESULT_VOCABULARY, "result")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "intent": self.intent,
            "targets": list(self.targets),
            "classification": self.classification,
            "classifications": [c.to_dict() for c in self.classifications],
            "authority_snapshot": _jsonable(self.authority_snapshot),
            "proposed_operation": self.proposed_operation,
            "approval": self.approval,
            "embedding_action": self.embedding_action,
            "affected_stores": list(self.affected_stores),
            "risk_flags": list(self.risk_flags),
            "ambiguity_flags": list(self.ambiguity_flags),
            "verification_contract": _jsonable(self.verification_contract),
            "evidence_summary": self.evidence_summary,
            "result": self.result,
            "evidence_gaps": list(self.evidence_gaps),
        }

    def to_canonical_json(self) -> str:
        return canonical_json(self.to_dict())


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)

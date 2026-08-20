"""Read-only CE Library state resolver (Phase 0 contract + Phase 1 planner)."""

from __future__ import annotations

from rag_engine.library_state.contract import (
    APPROVAL_CATEGORIES,
    CLASSIFICATIONS,
    EMBEDDING_ACTIONS,
    PROPOSED_OPERATIONS,
    RESULT_VOCABULARY,
    SUPPORTED_INTENTS,
)
from rag_engine.library_state.evidence import (
    CHROMA_ID_BATCH_SIZE,
    lookup_chroma_by_embedding_ids,
)
from rag_engine.library_state.move_approval import (
    MoveApprovalContext,
    MoveApprovalValidationError,
    MoveApprovalValidationResult,
    validate_move_approval,
)
from rag_engine.library_state.move_preflight import (
    PreMoveEvidence,
    PreMoveEvidenceError,
    collect_pre_move_evidence,
)
from rag_engine.library_state.plan import OperationPlan, make_request_id
from rag_engine.library_state.resolver import resolve_library_state

__all__ = [
    "APPROVAL_CATEGORIES",
    "CHROMA_ID_BATCH_SIZE",
    "CLASSIFICATIONS",
    "EMBEDDING_ACTIONS",
    "OperationPlan",
    "PROPOSED_OPERATIONS",
    "RESULT_VOCABULARY",
    "SUPPORTED_INTENTS",
    "lookup_chroma_by_embedding_ids",
    "make_request_id",
    "MoveApprovalContext",
    "MoveApprovalValidationError",
    "MoveApprovalValidationResult",
    "PreMoveEvidence",
    "PreMoveEvidenceError",
    "collect_pre_move_evidence",
    "resolve_library_state",
    "validate_move_approval",
]

"""Phase 0 operator contract - closed vocabularies for the read-only resolver.

Importable source of truth. No I/O, no production defaults, no mutation.
"""

from __future__ import annotations

from typing import Final, Mapping

# ---------------------------------------------------------------------------
# Supported intents
# ---------------------------------------------------------------------------

INTENT_CHECK: Final = "check"
INTENT_PLAN_MOVE: Final = "plan_move"
INTENT_PLAN_RENAME: Final = "plan_rename"
INTENT_PLAN_DELETE: Final = "plan_delete"
INTENT_PLAN_ADD: Final = "plan_add"
INTENT_PLAN_RECONCILE: Final = "plan_reconcile"
INTENT_VERIFY: Final = "verify"

SUPPORTED_INTENTS: Final[frozenset[str]] = frozenset(
    {
        INTENT_CHECK,
        INTENT_PLAN_MOVE,
        INTENT_PLAN_RENAME,
        INTENT_PLAN_DELETE,
        INTENT_PLAN_ADD,
        INTENT_PLAN_RECONCILE,
        INTENT_VERIFY,
    }
)

# ---------------------------------------------------------------------------
# Approval categories
# ---------------------------------------------------------------------------

APPROVAL_NOT_REQUIRED: Final = "NOT_REQUIRED"
APPROVAL_REQUIRED: Final = "REQUIRED"
APPROVAL_SEPARATE_INDEX_APPROVAL: Final = "SEPARATE_INDEX_APPROVAL"
APPROVAL_BLOCKED: Final = "BLOCKED"

APPROVAL_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        APPROVAL_NOT_REQUIRED,
        APPROVAL_REQUIRED,
        APPROVAL_SEPARATE_INDEX_APPROVAL,
        APPROVAL_BLOCKED,
    }
)

# ---------------------------------------------------------------------------
# Result vocabulary
# ---------------------------------------------------------------------------

RESULT_VERIFIED: Final = "VERIFIED"
RESULT_PARTIAL: Final = "PARTIAL"
RESULT_FAILED: Final = "FAILED"
RESULT_BLOCKED: Final = "BLOCKED"
RESULT_AMBIGUOUS: Final = "AMBIGUOUS"

RESULT_VOCABULARY: Final[frozenset[str]] = frozenset(
    {
        RESULT_VERIFIED,
        RESULT_PARTIAL,
        RESULT_FAILED,
        RESULT_BLOCKED,
        RESULT_AMBIGUOUS,
    }
)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

CLASS_SAME_BYTES_MOVED: Final = "SAME_BYTES_MOVED"
CLASS_ALIAS_ONLY: Final = "ALIAS_ONLY"
CLASS_EXACT_DUPLICATE: Final = "EXACT_DUPLICATE"
CLASS_DIFFERENT_REVISION: Final = "DIFFERENT_REVISION"
CLASS_NEW_DOCUMENT: Final = "NEW_DOCUMENT"
CLASS_STALE_METADATA: Final = "STALE_METADATA"
CLASS_INDEXED_OK: Final = "INDEXED_OK"
CLASS_NOT_INDEXED: Final = "NOT_INDEXED"
CLASS_AMBIGUOUS: Final = "AMBIGUOUS"

CLASSIFICATIONS: Final[frozenset[str]] = frozenset(
    {
        CLASS_SAME_BYTES_MOVED,
        CLASS_ALIAS_ONLY,
        CLASS_EXACT_DUPLICATE,
        CLASS_DIFFERENT_REVISION,
        CLASS_NEW_DOCUMENT,
        CLASS_STALE_METADATA,
        CLASS_INDEXED_OK,
        CLASS_NOT_INDEXED,
        CLASS_AMBIGUOUS,
    }
)

# ---------------------------------------------------------------------------
# Proposed operations (Phase 1 proposes; never executes)
# ---------------------------------------------------------------------------

OP_NO_OP: Final = "NO_OP"
OP_METADATA_ONLY_RECONCILE: Final = "METADATA_ONLY_RECONCILE"
OP_ALIAS_REGISTER: Final = "ALIAS_REGISTER"
OP_CERTIFIED_APPEND_PROPOSAL: Final = "CERTIFIED_APPEND_PROPOSAL"
OP_RETIREMENT_PROPOSAL: Final = "RETIREMENT_PROPOSAL"
OP_MANUAL_REVIEW: Final = "MANUAL_REVIEW"

PROPOSED_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        OP_NO_OP,
        OP_METADATA_ONLY_RECONCILE,
        OP_ALIAS_REGISTER,
        OP_CERTIFIED_APPEND_PROPOSAL,
        OP_RETIREMENT_PROPOSAL,
        OP_MANUAL_REVIEW,
    }
)

# ---------------------------------------------------------------------------
# Embedding actions
# ---------------------------------------------------------------------------

EMBEDDING_NONE: Final = "NONE"
EMBEDDING_PENDING_SEPARATE_APPROVAL: Final = "PENDING_SEPARATE_APPROVAL"
EMBEDDING_APPROVED_APPEND_ONLY: Final = "APPROVED_APPEND_ONLY"

EMBEDDING_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        EMBEDDING_NONE,
        EMBEDDING_PENDING_SEPARATE_APPROVAL,
        EMBEDDING_APPROVED_APPEND_ONLY,
    }
)

# ---------------------------------------------------------------------------
# Intent matrix
# ---------------------------------------------------------------------------

INTENT_MATRIX: Final[Mapping[str, Mapping[str, str]]] = {
    INTENT_CHECK: {
        "mode": "read",
        "default_approval": APPROVAL_NOT_REQUIRED,
        "default_operation": OP_NO_OP,
        "description": "Classify current state; do not imply mutation.",
    },
    INTENT_VERIFY: {
        "mode": "read",
        "default_approval": APPROVAL_NOT_REQUIRED,
        "default_operation": OP_NO_OP,
        "description": "Read-only coherence check against configured stores.",
    },
    INTENT_PLAN_MOVE: {
        "mode": "plan",
        "default_approval": APPROVAL_REQUIRED,
        "default_operation": OP_METADATA_ONLY_RECONCILE,
        "description": "Plan same-byte path move metadata repair; never execute.",
    },
    INTENT_PLAN_RENAME: {
        "mode": "plan",
        "default_approval": APPROVAL_REQUIRED,
        "default_operation": OP_METADATA_ONLY_RECONCILE,
        "description": "Plan same-byte rename metadata repair; never execute.",
    },
    INTENT_PLAN_DELETE: {
        "mode": "plan",
        "default_approval": APPROVAL_REQUIRED,
        "default_operation": OP_RETIREMENT_PROPOSAL,
        "description": "Plan alias/duplicate retirement only after retained live alias is proven.",
    },
    INTENT_PLAN_ADD: {
        "mode": "plan",
        "default_approval": APPROVAL_SEPARATE_INDEX_APPROVAL,
        "default_operation": OP_CERTIFIED_APPEND_PROPOSAL,
        "description": "Plan genuinely-new-byte intake; indexing is a separate approval.",
    },
    INTENT_PLAN_RECONCILE: {
        "mode": "plan",
        "default_approval": APPROVAL_REQUIRED,
        "default_operation": OP_METADATA_ONLY_RECONCILE,
        "description": "Plan metadata-only repair for path drift; never execute.",
    },
}

# No governed same-byte identity and no bound vector set.
# Classification is intent-gated and must not be inferred from siblings.
ABSENCE_CLASS_BY_INTENT: Final[Mapping[str, str]] = {
    INTENT_PLAN_ADD: CLASS_NEW_DOCUMENT,
    INTENT_CHECK: CLASS_NOT_INDEXED,
    INTENT_VERIFY: CLASS_NOT_INDEXED,
    INTENT_PLAN_RECONCILE: CLASS_NOT_INDEXED,
    INTENT_PLAN_MOVE: CLASS_AMBIGUOUS,
    INTENT_PLAN_RENAME: CLASS_AMBIGUOUS,
    INTENT_PLAN_DELETE: CLASS_AMBIGUOUS,
}

ABSENCE_OPERATION_BY_INTENT: Final[Mapping[str, str]] = {
    INTENT_PLAN_ADD: OP_CERTIFIED_APPEND_PROPOSAL,
    INTENT_CHECK: OP_NO_OP,
    INTENT_VERIFY: OP_NO_OP,
    INTENT_PLAN_RECONCILE: OP_NO_OP,
    INTENT_PLAN_MOVE: OP_MANUAL_REVIEW,
    INTENT_PLAN_RENAME: OP_MANUAL_REVIEW,
    INTENT_PLAN_DELETE: OP_MANUAL_REVIEW,
}

CLASSIFICATION_TABLE: Final[Mapping[str, Mapping[str, str]]] = {
    CLASS_SAME_BYTES_MOVED: {
        "required_evidence": (
            "Requested file exists; SHA-256 identity known; one governed old-path "
            "candidate; that candidate is absent on the filesystem."
        ),
        "disqualifying_evidence": (
            "ID-set mismatch among present stores; multiple current-path candidates; "
            "filesystem hash disagrees with governed source_hash."
        ),
        "allowed_operation": OP_METADATA_ONLY_RECONCILE,
        "embedding": EMBEDDING_NONE,
    },
    CLASS_ALIAS_ONLY: {
        "required_evidence": (
            "Requested extra path exists with the same SHA-256/document_id as an "
            "indexed revision; at least one other governed path still exists."
        ),
        "disqualifying_evidence": "Different bytes; no live retained alias.",
        "allowed_operation": OP_ALIAS_REGISTER,
        "embedding": EMBEDDING_NONE,
    },
    CLASS_EXACT_DUPLICATE: {
        "required_evidence": (
            "Candidate and retained path share SHA-256/document_id; at least one "
            "live retained alias is proven."
        ),
        "disqualifying_evidence": "Different bytes; last live alias not proven.",
        "allowed_operation": OP_RETIREMENT_PROPOSAL,
        "embedding": EMBEDDING_NONE,
    },
    CLASS_DIFFERENT_REVISION: {
        "required_evidence": (
            "Filesystem SHA-256 differs from the source_hash bound to the requested "
            "path in registry or tracker."
        ),
        "disqualifying_evidence": "Exact same-byte identity (that is EXACT_DUPLICATE or alias).",
        "allowed_operation": OP_CERTIFIED_APPEND_PROPOSAL,
        "embedding": EMBEDDING_PENDING_SEPARATE_APPROVAL,
    },
    CLASS_NEW_DOCUMENT: {
        "required_evidence": "plan_add and no governed same-byte identity or bound vector set.",
        "disqualifying_evidence": "Any governed same-byte mapping for this hash.",
        "allowed_operation": OP_CERTIFIED_APPEND_PROPOSAL,
        "embedding": EMBEDDING_PENDING_SEPARATE_APPROVAL,
    },
    CLASS_STALE_METADATA: {
        "required_evidence": (
            "Filesystem identity is coherent with document_id; a lower store still "
            "holds a currently-invalid path while IDs agree."
        ),
        "disqualifying_evidence": "ID-set mismatch (then AMBIGUOUS).",
        "allowed_operation": OP_METADATA_ONLY_RECONCILE,
        "embedding": EMBEDDING_NONE,
    },
    CLASS_INDEXED_OK: {
        "required_evidence": (
            "Filesystem, identity, registry aliases, tracker, and Chroma IDs agree "
            "for the requested path."
        ),
        "disqualifying_evidence": "Any present-store ID mismatch or path/hash conflict.",
        "allowed_operation": OP_NO_OP,
        "embedding": EMBEDDING_NONE,
    },
    CLASS_NOT_INDEXED: {
        "required_evidence": (
            "check/verify/plan_reconcile and no governed same-byte identity or vector mapping."
        ),
        "disqualifying_evidence": "Governed same-byte mapping exists.",
        "allowed_operation": OP_NO_OP,
        "embedding": EMBEDDING_NONE,
    },
    CLASS_AMBIGUOUS: {
        "required_evidence": (
            "Conflicting hashes or ID sets, multiple current candidates, or "
            "insufficient identity evidence for move/rename/delete."
        ),
        "disqualifying_evidence": "A single coherent closed-set classification applies.",
        "allowed_operation": OP_MANUAL_REVIEW,
        "embedding": EMBEDDING_NONE,
    },
}

# ---------------------------------------------------------------------------
# Authority precedence (read order; never overwrite a higher authority)
# ---------------------------------------------------------------------------

AUTHORITY_PRECEDENCE: Final[tuple[str, ...]] = (
    "filesystem_bytes_sha256",
    "stable_identity",
    "sqlite_registry",
    "certified_generation_tracker",
    "chroma_vector_ids",
    "journals_historic",
)

# ---------------------------------------------------------------------------
# Evidence gaps
# ---------------------------------------------------------------------------

GAP_REGISTRY_ABSENT: Final = "registry_absent"
GAP_TRACKER_ABSENT: Final = "tracker_absent"
GAP_CHROMA_ABSENT: Final = "chroma_absent"
GAP_REGISTRY_VECTOR_MAP_ABSENT: Final = "registry_vector_map_absent"
GAP_JOURNAL_NO_EXACT_LOCATOR: Final = "journal:no_exact_locator"
GAP_JOURNAL_EXACT_LOCATOR_MISSING: Final = "journal:exact_locator_missing"
GAP_GENERATION_FINGERPRINT_ABSENT: Final = "generation_fingerprint_absent"
GAP_CHROMA_QUERY_IDS_EMPTY: Final = "chroma_query_ids_empty"
GAP_CHROMA_READ_ERROR: Final = "chroma_read_error"
GAP_TRACKER_READ_ERROR: Final = "tracker_read_error"
GAP_REGISTRY_READ_ERROR: Final = "registry_read_error"

# ---------------------------------------------------------------------------
# Ambiguity templates (operator-facing focused questions)
# ---------------------------------------------------------------------------

AMBIGUITY_TEMPLATES: Final[Mapping[str, str]] = {
    "conflicting_identity": (
        "Stores disagree on source_hash or vector IDs for this target. "
        "Which store should be treated as currently authoritative?"
    ),
    "destination_collision": (
        "The destination path is already bound to a different identity. "
        "Should the existing locator be kept, replaced, or reviewed manually?"
    ),
    "last_alias_removal": (
        "No live retained alias was proven. Is this the last live locator "
        "(retirement/historical evidence) or a redundant copy?"
    ),
    "revision_ambiguity": (
        "Filesystem bytes differ from the hash bound to this path. "
        "Is this a new revision to index separately, or a corrupt/partial copy?"
    ),
    "insufficient_identity": (
        "plan_move/plan_rename/plan_delete needs a governed same-byte identity. "
        "Provide the known old path or index the document first."
    ),
}

# ---------------------------------------------------------------------------
# Lock policy (documented only - Phase 1 never acquires a lock)
# ---------------------------------------------------------------------------

LOCK_POLICY: Final[Mapping[str, object]] = {
    "ingest_lock_name": "ingest.lock",
    "append_lock_name": "certified_append.lock",
    "owner_identity": "process_id plus unix mtime written by the acquiring writer",
    "stale_time_basis": "filesystem mtime of the lock file; never inferred from journals",
    "validation": "Phase 1 must not create, clear, or wait on locks",
    "safe_clear_conditions": (
        "Only a future writer may clear a lock after proving the owner pid is dead "
        "and no ingest/append is running; the resolver must never clear locks"
    ),
    "operator_message": (
        "Library state resolution is read-only and does not take ingest or append locks."
    ),
}

CONFIDENCE_HIGH: Final = "HIGH"
CONFIDENCE_MEDIUM: Final = "MEDIUM"
CONFIDENCE_LOW: Final = "LOW"

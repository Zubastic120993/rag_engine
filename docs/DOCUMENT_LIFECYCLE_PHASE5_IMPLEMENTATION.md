# DOCUMENT LIFECYCLE PHASE 5 IMPLEMENTATION

**Status:** Implemented (not committed; production authority NOT activated)
**Date:** 2026-08-11
**Baseline HEAD:** `24572aadd3975947f3eb725eb345d3b635b5bec9`
**Schema version:** `2` (additive Phase 5 lifecycle on Phase 3 registry)

---

## 1. Objective

Implement registry-level **revision / replacement lifecycle** at:

```text
subject_id  →  document_id (immutable revision)
```

Phase 5 governs document lineage and revisions, **not** vector identity.

Explicit boundary (from fingerprint recovery release):

- Legacy Chroma UUIDs remain unchanged
- Legacy stable `chunk_id` may remain unresolved
- No re-index, backfill, UUID rewrite, or production registry population
- No production retrieval filtering activation
- No Phase 6 work

---

## 2. State definitions

| State | Meaning |
|-------|---------|
| **ACTIVE** | Current usable revision for a `subject_id` (at most one per subject). |
| **SUPERSEDED** | Older revision replaced by a newer revision **within the same subject**. |
| **ARCHIVED** | Retained for history/reference; not operationally current. |
| **REPLACED** | Explicitly replaced by another identified revision (may be another subject). |
| **WITHDRAWN** | Intentionally removed from operational use; replacement optional. |
| **DUPLICATE** | Declared duplicate of an explicit canonical revision; row retained. |

Do not conflate ARCHIVED / WITHDRAWN / SUPERSEDED — all may be non-current for different reasons.

---

## 3. Identity roles

| Identity | Role in Phase 5 |
|----------|-----------------|
| `subject_id` | Logical lineage family |
| `document_id` | Immutable revision (`docrev:<source_hash>`); lifecycle attaches here |
| `chunk_id` | **Not required** for lifecycle ops (legacy may be unresolved) |
| `chroma_embedding_id` | **Not required** for lifecycle ops; preserved when present |

---

## 4. State vs relationship

Lifecycle status is stored on `document_versions.lifecycle_status`.

Normalized relations are stored separately in `document_version_relations`:

| relation_type | Meaning | Subject rule |
|---------------|---------|--------------|
| `supersedes` | source (new) supersedes target (old) | **same** `subject_id` |
| `replaces` | source replaces target | **may cross** subjects |
| `duplicate_of` | source is duplicate of canonical target | any subjects; target required |

Example:

```text
A.lifecycle_status = SUPERSEDED
B.lifecycle_status = ACTIVE
relation: B supersedes A
```

---

## 5. Schema changes (v1 → v2)

Additive only.

### Columns on `document_versions`

- `lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE'`
- `lifecycle_updated_at TEXT`

### Tables

**`document_lifecycle_events`** (append-only)

- `event_id`, `document_id`, `previous_state`, `new_state`
- `relation_type`, `related_document_id`, `reason`, `actor`, `source`, `created_at`

**`document_version_relations`**

- `source_document_id`, `target_document_id`, `relation_type`
- `reason`, `created_at`, `actor`, `source`
- UNIQUE `(source, target, relation_type)`

### Constraint

Partial unique index:

```sql
CREATE UNIQUE INDEX idx_document_versions_one_active_per_subject
  ON document_versions(subject_id) WHERE lifecycle_status = 'ACTIVE';
```

### v1 → v2 migration compatibility policy

Migration preserves known facts and **does not infer historical succession**.

| v1 subject shape | After migration |
|------------------|-----------------|
| Exactly one revision | That revision remains / becomes **ACTIVE** (unambiguous). |
| Multiple revisions | **All** revisions → **WITHDRAWN**; **zero ACTIVE**; **no** `supersedes` / `replaces` / `duplicate_of` edges. |

For each multi-revision row, migration appends one audit event:

- `new_state = WITHDRAWN`
- `previous_state = NULL` (schema default is not treated as historical truth)
- `reason = migration_v1_to_v2_multi_revision_current_unknown`
- `source = schema_migration_v1_to_v2`
- `related_document_id = NULL`
- no relation row

**Note:** `WITHDRAWN` here is a conservative migration state meaning “not declared operationally current by migration.” It does **not** assert that a historical withdrawal action actually occurred.

Selecting the operational current revision later requires an explicit governed operation (see `activate_revision`), not timestamp/path/`document_id` ordering.

---

## 6. Transition matrix (simple ops)

| From \ To | ACTIVE | SUPERSEDED | ARCHIVED | REPLACED | WITHDRAWN | DUPLICATE |
|-----------|--------|------------|----------|----------|-----------|-----------|
| ACTIVE | — | via `supersede_revision` | yes | via `replace_revision` | yes | via `mark_duplicate` |
| SUPERSEDED | no | — | yes | no | no | no |
| ARCHIVED | no | no | — | no | no | no |
| REPLACED | no | no | yes | — | no | no |
| WITHDRAWN | no | no | yes | via `replace_revision` | — | via `mark_duplicate` |
| DUPLICATE | no | no | yes | no | no | — |

`set_revision_status` rejects SUPERSEDED / REPLACED / DUPLICATE (must use compound ops).

Invalid examples (fail closed): `WITHDRAWN→ACTIVE`, `DUPLICATE→ACTIVE`, `SUPERSEDED→ACTIVE`, `ARCHIVED→ACTIVE`.

---

## 7. ACTIVE uniqueness

**Policy: at most one ACTIVE revision per `subject_id`.**

Enforced by:

1. Partial unique index
2. `register_document_version` refusal to create a second ACTIVE
3. `supersede_revision` transactional demote-then-promote

Staging rule: while an ACTIVE exists, register the successor with an explicit non-ACTIVE status (commonly `WITHDRAWN` as staging), then call `supersede_revision`.

Silent deactivation of another ACTIVE is forbidden outside an explicit lifecycle operation that records events.

---

## 8. High-level API

Module: `rag_engine.metadata_registry.lifecycle`

| Operation | Purpose |
|-----------|---------|
| `get_revision_status` | Read revision + lifecycle |
| `get_active_revision` | ACTIVE row for subject or None |
| `get_lifecycle_history` | Ordered append-only events |
| `get_relations` | Edges involving a revision |
| `set_revision_status` | Simple transitions only |
| `archive_revision` / `withdraw_revision` | Convenience wrappers |
| `activate_revision` | Narrow WITHDRAWN→ACTIVE for migration/current-selection (mandatory reason; no inferred relation) |
| `supersede_revision` | Same-subject atomic switch |
| `replace_revision` | Cross-subject replacement edge + REPLACED |
| `mark_duplicate` | DUPLICATE + `duplicate_of` (no delete) |
| `transition_matrix` | Inspect simple allowed edges |

All IDs validated via Phase 2 `stable_identity` validators.

`activate_revision` is **not** a generic lifecycle bypass:

- source must be `WITHDRAWN` (rejects `SUPERSEDED` / `DUPLICATE` / `ARCHIVED`)
- subject must have no other ACTIVE revision
- reason mandatory; audit event recorded; no relation invented
- general `set_revision_status` still rejects `WITHDRAWN → ACTIVE`

---

## 9. Audit / event model

- Every status change appends a `document_lifecycle_events` row
- Initial registration writes `previous_state=NULL`, `new_state=<initial>`
- Events are **immutable** (no UPDATE of historical rows)
- Later decisions create new events
- Timestamps: UTC ISO-8601 via `utc_now()` (`...Z`)

---

## 10. Transaction model

Compound ops run inside `registry_transaction` (`BEGIN IMMEDIATE`).

`supersede_revision` order:

1. Validate same subject + states
2. Demote old → SUPERSEDED (frees ACTIVE slot)
3. Promote new → ACTIVE
4. Upsert `supersedes` relation
5. Commit

Any failure → full rollback (proven by forced relation failure test).

---

## 11. Idempotency

- Identical completed `supersede_revision(A→B)` → no-op, `idempotent=True`
- Identical completed `replace_revision` / `mark_duplicate` → no-op
- Conflicting relation to a different target → `RegistryConflictError`

---

## 12. Legacy UUID / unresolved chunk_id compatibility

Lifecycle APIs touch only:

- `documents`
- `document_versions`
- `document_lifecycle_events`
- `document_version_relations`

They do **not** require `chunks` or `chunk_vector_map`.

Test `test_lifecycle_without_chunks_or_vector_map` proves supersede works with zero chunk/vector rows.

---

## 13. Retrieval boundary

Phase 5 **does not** change production query/ingest behavior.

Future retrieval may filter by joining:

```text
subject_id → ACTIVE document_id → source_hash / tracker chunk_ids (UUIDs)
```

without deterministic legacy `chunk_id`.

Physical vector deletion is out of scope.

---

## 14. Non-goals

- Production `.rag_state` creation / population
- Legacy stable `chunk_id` backfill
- Chroma UUID rewrite
- Re-index / embedding rebuild
- Activating lifecycle filters in live retrieval
- Phase 6 embedding fingerprint system
- Commit / push (separate release gate)

---

## 15. Test results

```text
env -u PYTHONPATH -u PYTHONHOME PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  venv/bin/python -m pytest tests/test_document_lifecycle.py -q
→ 19 passed

venv/bin/python -m pytest tests/test_metadata_registry.py -q
→ 13 passed

venv/bin/python -m pytest tests/test_reconciliation.py -q
→ 19 passed

venv/bin/python -m pytest tests/test_stable_identity.py -q
→ 35 passed

env -u PYTHONPATH -u PYTHONHOME PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  venv/bin/python -m pytest tests -q \
  --ignore=tests/test_chroma_persistence.py \
  -k "not live and not integration and not openai"
→ 300 passed, 5 deselected, 1 warning
```

Migration remediation (2026-08-11): multi-revision v1 subjects no longer invent ACTIVE/SUPERSEDED/`supersedes`; all → WITHDRAWN with audit events only. `activate_revision` added for explicit current selection.

---

## 16. Production safety

All tests use temporary SQLite paths only.

Confirmed unchanged:

- `.rag_db/chroma.sqlite3`
- `.rag_db/embedded.json`
- `.rag_db/index_fingerprint.json`
- `.rag_state` absent
- production registry absent

re-index / backfill / UUID rewrite / registry population / retrieval activation: **NO**

---

## 17. Phase 6 readiness

**NOT READY** — not started. Embedding fingerprint / Phase 6 is a separate gated track.

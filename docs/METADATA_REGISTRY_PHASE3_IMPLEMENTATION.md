# METADATA REGISTRY PHASE 3 IMPLEMENTATION

**Status:** Implemented and independently verified for commit (production authority NOT activated)
**Date:** 2026-08-11
**Baseline HEAD:** `4d56aa630ec7ca006f36142931d6e96bbee6cbf5` (`feat: establish stable document and chunk identity`)
**Schema version:** `1` (fresh identity-aligned registry; branch scaffold never production)

---

## 1. Scope

Phase 3 implements the **target authoritative SQLite metadata-registry architecture** for:

- logical subject lineage
- immutable document revisions
- source path locators
- stable chunks
- Chroma vector mappings (including legacy UUID4)

Non-goals (explicit):

- production `.rag_state` creation / population
- production backfill
- Chroma UUID rewrite
- live ingest authority switch
- Phase 4 reconciliation
- Phase 5 ACTIVE/SUPERSEDED lifecycle automation
- Phase 6 embedding fingerprint system

---

## 2. Architecture / authority boundary

| Store | Responsibility |
|-------|----------------|
| **SQLite registry** (target) | Authoritative identity + lifecycle + relationships |
| **Chroma** | Vector storage + retrieval index |
| **embedded.json** | Legacy ingest tracker only (unchanged; migration later) |

Wording: *target authoritative registry architecture implemented; production authority switch not yet activated.*

Approved production path (not created by Phase 3):

`<LIBRARY_ROOT>/.rag_state/metadata_registry/metadata_registry_v1.sqlite3`

API requires an **explicit** DB path. Path helpers compute production locations without opening/creating DBs.

---

## 3. Terminology alignment

| Phase-1 / Phase-3 term | Meaning | Old scaffold (branch) |
|------------------------|---------|------------------------|
| `subject_id` | Logical lineage PK on `documents` | was `documents.document_id` (`doc:<uuid>`) |
| `document_id` | Immutable revision PK on `document_versions` (`docrev:<sha256>`) | was `document_versions.document_version_id` (`docver:…`) |
| `chunk_id` | Deterministic chunk PK | not present |
| `document_version_id` | **Not used** in Phase 3 schema | obsolete name |

One authoritative meaning: **`document_id` = immutable `docrev:` revision only.**

---

## 4. Exact schema (tables)

### `documents`

- PK `subject_id` (validated Phase-2 subject grammar)
- optional `document_type`, `scope`, `canonical_title`, `document_number`, notes, timestamps

### `document_versions`

- PK `document_id` (`docrev:<source_hash>`)
- FK `subject_id` → `documents`
- `source_hash`, `identity_scheme_version`, optional `content_hash`, extractor fields
- UNIQUE(`source_hash`)
- CHECK(`document_id = 'docrev:' || source_hash`)

### `source_files`

- PK `source_file_id` (`src:<digest>`)
- FK `document_id` → `document_versions`
- UNIQUE(`document_id`, `relative_path`)
- path normalized via Phase-2 `normalize_relative_path`

### `chunks`

- PK `chunk_id`
- FK `document_id` → `document_versions`
- `identity_scheme_version`, `chunking_fingerprint`, `chunk_ordinal`, optional `content_hash`/`page`
- UNIQUE(document_id, fingerprint, ordinal, scheme)

### `chunk_vector_map`

- PK (`chunk_id`, `physical_collection_name`)
- UNIQUE(`physical_collection_name`, `chroma_embedding_id`)
- `mapping_status` ∈ {`legacy_uuid`, `native_chunk_id`, `pending`}
- legacy UUID4 and future `chunk_id`-equal embeddings both representable

### `registry_schema_version`

- schema versioning; unknown newer versions fail closed; downgrade forbidden

---

## 5. Foreign keys / uniqueness

- All connections: `PRAGMA foreign_keys = ON`
- Orphan chunks / mappings rejected
- Unique constraints on source_hash, locator paths, vector IDs per collection

---

## 6. Registry API

| Symbol | Purpose |
|--------|---------|
| `open_registry` / `connect_registry` | Explicit-path SQLite open + FK ON |
| `initialize_registry` | Create/migrate at explicit path |
| `get_schema_version` | Current version (fail on unknown newer) |
| `register_subject` | Logical lineage |
| `register_document_version` | Immutable revision + hash invariant |
| `register_source_file` | Locator (path ≠ identity) |
| `register_chunk` | Stable chunk + constituent check |
| `register_vector_mapping` | chunk ↔ Chroma id map |
| `register_document_lifecycle` | Atomic multi-table registration |
| `registry_transaction` | Explicit BEGIN/COMMIT/ROLLBACK |
| `production_registry_path` | Path compute only (no open/create) |

---

## 7. Identity invariants

- `document_id == "docrev:" + source_hash` (validated + SQL CHECK)
- `chunk_id` must match Phase-2 preimage (scheme|doc|fingerprint|ordinal)
- All IDs validated via `rag_engine.stable_identity` validators
- Vector mapping conflicts fail hard (no silent move)

---

## 8. Transaction / idempotency

- `register_document_lifecycle` and `registry_transaction` roll back on any error
- Identical repeated registrations are idempotent no-ops
- Same ID with conflicting authoritative fields → `RegistryConflictError`

---

## 9. Legacy Chroma UUID support

```text
chunk_id = chunk:<deterministic>
chroma_embedding_id = <legacy UUID4>
mapping_status = legacy_uuid
```

No UUID rewrite. Population deferred to Phase 4 / gated backfill.

---

## 10. Scaffold port / merge details

**Audited:** `feature/metadata-registry-v1-scaffold` (`889f695`), `integration/rag-reliability-registry-v1`.

**Reused patterns (manual port, not branch merge):**

- explicit absolute DB path discipline
- `PRAGMA foreign_keys = ON`
- forward-only schema versioning / no downgrade
- exception taxonomy shape
- approved production path policy location

**Not imported:**

- obsolete `doc:` / `docver:` identifier generators
- full 14-table maritime metadata graph as live authority (authorities, vessels, applicability, …)
- production dry-run CLI / planning that could confuse Phase 3 boundary
- vocabulary seed requiring those tables

**Changed vs scaffold:** terminology aligned to stable-id-v1; added `chunks` + `chunk_vector_map`; dropped status-in-ID helpers.

Phase 2 `stable_identity/registry_tables.py` marked **superseded** (kept for Phase 2 tests only).

---

## 11. Production non-goals

- No production registry created
- No Chroma / embedded.json mutation
- No ingest wiring
- Production authority switch remains deferred

---

## 12. Tests

- `tests/test_metadata_registry.py`
- temp-DB smoke + `PRAGMA integrity_check` / `foreign_key_check`
- Phase 2 + broader suite regressions

---

## 13. Phase 4 handoff

Phase 4 may begin as **read-only** registry ↔ Chroma / tracker reconciliation planning against temporary or explicitly approved non-production DBs first; production backfill remains gated.

Do not start Phase 4 in this task.

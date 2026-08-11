# STABLE IDENTITY PHASE 2 PLAN

**Status:** Bounded implementation plan only (not authorization to execute)
**Depends on:** `docs/STABLE_IDENTITY_SPEC_V1.md` (`stable-id-v1`)
**Date:** 2026-08-11

---

## 1. Objective (minimum)

Implement **offline-capable identity helpers + schema deltas + isolated tests** for `stable-id-v1` **without**:

- mutating `/Users/vladymyrzub/CE_Library/.rag_db`
- production registry creation
- production ingest wiring
- production re-index
- confidence-gate changes

Phase 2 ends when helpers and migrations work against **temporary fixture DBs** and unit tests — not when production is converted.

---

## 2. Exact files likely to change / add

### Add (preferred on `main` or a dedicated feature branch)

| Path | Purpose |
|------|---------|
| `rag_engine/stable_identity/__init__.py` | Public exports |
| `rag_engine/stable_identity/scheme.py` | `IDENTITY_SCHEME_VERSION = "stable-id-v1"` |
| `rag_engine/stable_identity/hashes.py` | `source_hash_file`, `content_hash_text`, `chunking_fingerprint` |
| `rag_engine/stable_identity/ids.py` | `make_document_id`, `make_chunk_id`, `make_provisional_subject_id`, validators |
| `rag_engine/stable_identity/paths.py` | NFC relative-path normalization per Spec §9 |
| `tests/test_stable_identity_ids.py` | Determinism / collision / example vectors |
| `tests/test_stable_identity_paths.py` | Path normalization cases |
| `docs/STABLE_IDENTITY_PHASE2_RECEIPT.md` | Optional execution receipt (later) |

### Likely touch if registry scaffold is merged first

| Path | Purpose |
|------|---------|
| `rag_engine/metadata_registry/schema.py` | Add `chunks`, `chunk_vector_map`; optional `subject_id` synonym column |
| `rag_engine/metadata_registry/migrations.py` | v1→v2 (or v1.1) additive migration |
| `rag_engine/metadata_registry/identifiers.py` | Align/`deprecate` conflicting `doc:` logical helpers; delegate to `stable_identity` |
| `tests/test_metadata_registry.py` | Schema presence for new tables |

### Explicitly do **not** change in Phase 2

| Path | Reason |
|------|--------|
| `rag_engine/ingest.py` production path | Wiring deferred to gated Phase 3+ |
| `rag_engine/query.py` / authority confidence | Out of scope |
| Production `.rag_db` / `.rag_state` | Forbidden |
| `rag_engine/reconcile_path.py` behavior | Except read-only imports of path helpers if needed |

---

## 3. Functions / classes to add

```text
stable_identity.hashes
  source_hash_bytes(data: bytes) -> str
  source_hash_file(path: Path) -> str
  content_hash_text(text: str) -> str
  chunking_fingerprint(contract: Mapping) -> str
  default_chunking_contract_from_config() -> dict   # reads config; marks extractor_version UNKNOWN if unknown

stable_identity.ids
  make_document_id(source_hash: str) -> str          # docrev:<sha256>
  make_chunk_id(*, document_id, chunking_fingerprint, ordinal) -> str
  make_provisional_subject_id(source_hash: str) -> str
  make_key_subject_id(kind: str, normalized_key: str) -> str
  validate_document_id / validate_chunk_id / validate_subject_id

stable_identity.paths
  normalize_relative_path(path: str, *, library_root: Path | None) -> str
```

Pure functions only in Phase 2 — no Chroma writes.

---

## 4. Schema migration needs

Additive only:

1. Table `chunks` (PK `chunk_id`, FK `document_id` / `document_version_id` per synonym decision)
2. Table `chunk_vector_map` (`chunk_id`, `chroma_embedding_id`, `physical_collection_name`, `mapping_status`, `identity_scheme_version`)
3. Column `identity_scheme_version` on version/chunk rows if not present
4. Optional: `documents.subject_id` TEXT UNIQUE alias **or** document that existing `documents.document_id` stores Phase-1 `subject_id` values going forward (prefer **documentation + validators** over dual columns unless merge requires it)

Bump `registry_schema_version` with `backward_compatible=1`.

**Production migration:** not executed in Phase 2.

---

## 5. Isolated test fixture strategy

1. Use `tmp_path` pytest fixtures only.
2. Golden vectors for Spec examples 1–10 (hash/id expectations).
3. Property: identical inputs ⇒ identical IDs across process restarts (invoke helpers twice).
4. Property: ordinal collision resistance for identical chunk text.
5. Property: fingerprint change ⇒ chunk_id change; document_id unchanged for same bytes.
6. If schema tests run: `initialize_registry(tmp_sqlite)` then assert new tables; never point at `.rag_db`.

---

## 6. Temporary DB validation

```text
1. Create temp registry sqlite under pytest tmp_path
2. Apply migrations
3. Insert synthetic subject + document_id=docrev:<hash> + two locators (dup copy)
4. Insert chunks with deterministic chunk_ids
5. Insert chunk_vector_map rows with fake legacy UUIDs
6. Assert round-trip queries
7. Delete temp DB automatically with fixture teardown
```

No use of production paths.

---

## 7. Production changes explicitly excluded

- No writes to `.rag_db/chroma.sqlite3`, `embedded.json`, `index_fingerprint.json`
- No creation of production `.rag_state`
- No `run_ingest` / `reindex_loop` against library root
- No Chroma metadata backfill
- No Hermes / ce_rag_query contract changes
- No commits required by this plan document itself

---

## 8. Rollback / containment

| Risk | Containment |
|------|-------------|
| Accidental production import side effect | Helpers must not open persist_dir on import |
| Schema merge conflict with registry branch | Keep `stable_identity` package standalone until registry merge |
| ID format regret | Frozen behind `identity_scheme_version`; v2 only via new version |
| Test pollution | tmp_path only; assert env `RAG_DB` unset or redirected in tests |

Rollback of Phase 2 code = revert feature branch / delete new package; no data rollback needed if production untouched.

---

## 9. Release-review checklist (before declaring Phase 2 code complete)

- [ ] Spec examples 1–10 have automated assertions
- [ ] `git diff` touches only planned paths
- [ ] No production DB mtime changes during test runs
- [ ] Validators reject path-based `document_id`
- [ ] Collision tests assert raise/STOP behavior (no overwrite helpers)
- [ ] Registry migration (if included) is additive and dry-runnable on temp DB
- [ ] README / docs note: production wiring **not** enabled
- [ ] Independent review of ID helpers vs Spec §5–§7
- [ ] Explicit operator acknowledgment that Phase 3 is required before ingest emits IDs

---

## 10. Suggested Phase 2 exit criteria

Phase 2 **PASS** when:

1. `stable-id-v1` helpers exist and are tested.
2. Optional additive schema for chunks/mapping exists **or** is clearly sequenced behind registry merge without blocking helpers.
3. Production remains unmodified.
4. A short receipt lists commits/tests (when implementation is authorized).

Phase 2 **does not** require production backfill.

---

## 11. Immediate next phase (preview only)

**Phase 3 (not authorized here):** dry-run backfill planner reading tracker + source files → proposed registry rows; still no production write without approval gate.

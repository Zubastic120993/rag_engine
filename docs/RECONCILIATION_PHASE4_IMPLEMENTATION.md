# RECONCILIATION PHASE 4 IMPLEMENTATION

**Status:** Implemented and independently verified for commit (no production mutation; authority not activated)
**Date:** 2026-08-11
**Baseline HEAD:** `d1b9902cf4618b43988292fb1214617bcd451966` (`feat: integrate stable metadata registry`)

---

## 1. Objective

Phase 4 builds a **read-only** reconciliation engine that classifies agreement and disagreement among:

- legacy `embedded.json` tracker
- Chroma persistence (`chroma.sqlite3` metadata / embedding IDs)
- stable-id-v1 identity model
- optional metadata registry snapshot (temp / explicit path only)

Phase 4 **identifies and classifies**. It does **not** repair.

---

## 2. Data sources

| Source | Role | Access |
|--------|------|--------|
| `embedded.json` | Historical source_hash (= digest), paths, chunk UUID IDs, collection | `O_RDONLY` JSON parse |
| `chroma.sqlite3` | Physical embedding IDs + source/page/collection metadata | SQLite `mode=ro` URI |
| Source files under library root | Current observed raw-byte hash | Read-only hash when needed |
| `index_fingerprint.json` | Partial cohort evidence (embed/chunk_size/overlap) | Read-only; **not** Spec §7.1 fingerprint |
| Metadata registry sqlite | Optional REGISTRY_ONLY comparisons | Explicit path, `mode=ro` |

Production registry path is **not** created or opened by default.

---

## 3. Read-only guarantees

- Tracker opened with `os.O_RDONLY` only; never rewritten.
- Chroma inspected via `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` + `PRAGMA query_only=ON`.
- **No chromadb / LangChain client** against production (those can mutate persistence metadata).
- Full embedding vectors and `chroma:document` text are **not** loaded by default.
- Report writers refuse paths under `.rag_db` / `.rag_state`.
- Import of `rag_engine.reconciliation` has no I/O side effects.

---

## 4. Reconciliation states

Exact roadmap states:

| State | Meaning |
|-------|---------|
| `MATCH` | Tracker↔Chroma ID join agrees; no stronger mismatch; hash verified when hashing enabled |
| `REGISTRY_ONLY` | Registry entity with no tracker/Chroma association |
| `CHROMA_ONLY` | Chroma embedding ID not referenced by tracker |
| `METADATA_MISMATCH` | Identity join defensible but path/collection metadata disagrees |
| `CHUNK_COUNT_MISMATCH` | Tracker chunk IDs missing from Chroma (count disagreement) |
| `HASH_MISMATCH` | Historical tracker source_hash ≠ current observed file bytes |
| `DUPLICATE_ACTIVE` | Same Chroma ID owned by multiple tracker digests |
| `UNKNOWN` | Insufficient evidence (missing file, unresolved fingerprint, etc.) |

---

## 5. State precedence

When multiple conditions apply to one unit:

1. `HASH_MISMATCH`
2. `DUPLICATE_ACTIVE`
3. `CHUNK_COUNT_MISMATCH`
4. `METADATA_MISMATCH`
5. `REGISTRY_ONLY`
6. `CHROMA_ONLY`
7. `MATCH`
8. `UNKNOWN`

Dictionary iteration order never selects state.

---

## 6. Reason codes (subordinate)

Examples: `TRACKER_ONLY`, `MISSING_SOURCE_FILE`, `MISSING_CHROMA_ID`, `INVALID_LEGACY_ID`, `PATH_DRIFT`, `COLLECTION_MISMATCH`, `ORDINAL_UNRESOLVED`, `FINGERPRINT_UNKNOWN`, `SOURCE_HASH_UNVERIFIED`, `MULTI_PATH`, `ZERO_CHUNK`, `CHROMA_UNTRACKED`, `REGISTRY_ENTITY`, `DUPLICATE_CHUNK_OWNERSHIP`, `PAGE_NOT_ORDINAL`, `STABLE_CHUNK_PROVEN`.

Primary externally reported state remains one of the eight roadmap states.

---

## 7. Tracker / Chroma models

- `TrackerRecord(source_hash, source_paths, chunk_ids, collection, …)`
- `ChromaRecord(chroma_embedding_id, physical_collection_name, source_path, page, collection_meta, …)`

Path is a **locator**, never business identity.

Page metadata is **not** chunk ordinal.

---

## 8. Stable-ID mapping rules

- `document_id = docrev:<source_hash>` only from trustworthy source_hash (tracker digest when valid).
- Unresolved lineage planning uses in-memory `subj:pending:<source_hash>` only; never persisted to production.
- Stable `chunk_id` requires **all** of:
  - `document_id`
  - **explicit historical** `chunking_fingerprint`
  - proven ordinal
  - `identity_scheme_version`

### Critical limitation

`index_fingerprint.json` is **not** the Spec §7.1 chunking contract fingerprint (missing separators, min/max chars, extractor, extractor_version, identity_scheme_version).

Therefore production cohorts are classified:

- historical fingerprint: **UNKNOWN** (unless an explicit fingerprint is supplied)
- extractor version: **PARTIAL** / **UNKNOWN**

**Current/default chunking settings are never substituted** for unknown historical fingerprints.

Ordinal may be taken from tracker `chunk_ids` list order **only** when historical fingerprint is known. Page is never used as ordinal.

---

## 9. Source hash semantics

| Field | Meaning |
|-------|---------|
| `historical_source_hash` | Tracker digest (ingest-time bytes) |
| `current_observed_source_hash` | Fresh SHA-256 of current file bytes |

Never conflated. Difference ⇒ `HASH_MISMATCH` without asserting which side is “correct”.

---

## 10. Performance model

- Complexity ≈ **O(T + C + E)** with hash maps (tracker digests, chroma IDs, chunk ownership).
- No O(N²) joins.
- Batched SQL metadata load for keys `source`, `page`, `collection`, `probe` only.
- Embedding arrays not loaded.
- Source hashes cached in-memory for the run only.

---

## 11. Package layout

```text
rag_engine/reconciliation/
  __init__.py
  __main__.py
  models.py
  tracker_reader.py
  chroma_reader.py
  source_observer.py
  fingerprint_evidence.py
  registry_snapshot.py
  engine.py
  report.py
```

CLI (explicit paths required):

```bash
python -m rag_engine.reconciliation \
  --tracker /path/to/embedded.json \
  --chroma-sqlite /path/to/chroma.sqlite3 \
  --library-root /path/to/CE_Library \
  --index-fingerprint /path/to/index_fingerprint.json \
  --json-out /tmp/report.json \
  --md-out /tmp/report.md
```

---

## 12. Production run summary

Independently verified production read-only reconciliation (2026-08-11):

| State | Count |
|-------|------:|
| MATCH | 1549 |
| METADATA_MISMATCH | 187 |
| CHROMA_ONLY | 1 |
| REGISTRY_ONLY | 0 |
| CHUNK_COUNT_MISMATCH | 0 |
| HASH_MISMATCH | 0 |
| DUPLICATE_ACTIVE | 0 |
| UNKNOWN | 0 |

Join: tracker chunk IDs found 124569 / missing 0; Chroma-only 1 (doctor probe leftover).
Source hashes: 1736 match / 0 mismatch / 0 missing files.
Stable chunk IDs proven: 0; unresolved: 124569 (`FINGERPRINT_UNKNOWN`).

Machine reports are written under `/private/tmp` only (not committed).

---

## 13. Non-goals

- No production `.rag_state` / registry creation or population
- No Chroma ID rewrite
- No tracker rewrite
- No metadata repair
- No backfill
- No re-index
- No live ingest / query changes
- No Phase 5 lifecycle automation
- No Phase 6 embedding fingerprint migration
- Production authority switch remains deferred

---

## 14. Phase 5 handoff

Phase 5 (lifecycle / ACTIVE–SUPERSEDED) should begin only after reconciliation findings are reviewed.

Unresolved `FINGERPRINT_UNKNOWN` / ordinal gaps must be addressed (evidence recovery or controlled re-index policy) before any bulk UUID↔stable-chunk backfill.

Phase 5 readiness is **not** automatic authorization to mutate production.

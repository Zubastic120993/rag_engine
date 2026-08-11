# PHASE 6A — EMBEDDING FINGERPRINT AUDIT

**Status:** Phase 6A evidence package
**Date:** 2026-08-11
**Repository HEAD:** `9a85dade34d54adda6250626d6c551da1991cf99`
**Phase 5 ancestor check:** `git merge-base --is-ancestor 9a85dad… HEAD` → `0` (PASS)
**Contract:** `docs/EMBEDDING_FINGERPRINT_CONTRACT_V1.md`

This audit is read-only with respect to production state. No ingest/retrieval enforcement was activated.

---

## 1. Repository baseline

| Item | Value |
|------|-------|
| Root | `/Users/vladymyrzub/CE_Library/Tools/rag_engine` |
| Branch | `main` |
| HEAD | `9a85dade34d54adda6250626d6c551da1991cf99` (`feat: add document revision lifecycle`) |
| Commits since Phase 5 | none (HEAD **is** Phase 5) |
| Tracked dirty files | none at Phase 6A start |
| Untracked artifacts | many pre-existing (`audit_reports/`, `backups/`, `*.bak`, older embedding design drafts) — **excluded** from Phase 6A commit |

---

## 2. Relevant source files

| Area | Files |
|------|-------|
| Fingerprint sidecar | `rag_engine/fingerprint.py` |
| Ingest / skip / Chroma write | `rag_engine/ingest.py` |
| Retrieval embed | `rag_engine/query.py` |
| Config | `rag_engine/config.py`, scopes registry YAML |
| Text normalization | `rag_engine/text.py` |
| Doctor | `rag_engine/doctor.py` |
| CLI | `rag_engine/cli.py` |
| Spec §7.1 chunking fingerprint | `rag_engine/stable_identity/canonical.py`, `constants.py` |
| Reconciliation evidence | `rag_engine/reconciliation/fingerprint_evidence.py` |
| Backfill / reindex | `rag_engine/backfill_collections.py`, `rag_engine/reindex_loop.py` |
| Prior design drafts (untracked) | `docs/EMBEDDING_IDENTITY_ARCHITECTURE_V1.md`, `docs/EMBEDDING_IDENTITY_MIGRATION_PLAN_V1.md` |

---

## 3. Current embedding flow

```text
library files
  → ingest._iter_docs / os_walk_filtered
  → SHA-256(file bytes) as tracker key
  → if digest in embedded.json and not --force: SKIP re-embed (_attach_path only)
  → else load PDF/text → Documents
  → RecursiveCharacterTextSplitter(chunk_size, chunk_overlap, separators)
  → normalize_text (NFKC) + length filter (50, 3000)
  → OllamaEmbeddings(model=embed_model())
  → Chroma.add_documents (UUID chunk ids)
  → update embedded.json {paths, chunk_ids, collection, ingested_at, extraction?}
  → optionally write index_fingerprint.json (live config snapshot)
```

Retrieval:

```text
query
  → OllamaEmbeddings(model=embed_model())  # no fingerprint gate
  → Chroma similarity search
  → ranking / authority filters
```

Exact anchors:

- Embeddings constructed: `ingest.py` (`OllamaEmbeddings`), `query.py` (~L224)
- Chunking: `ingest._clean_chunks` (~L85–98)
- Skip: `ingest._run_ingest_locked` `if digest in tracker and not force` (~L360–363)
- Fingerprint write: `ingest` end (~L397–409) via `fingerprint.write_fingerprint`
- Fingerprint compare: `doctor.run_doctor` only (`compare_fingerprint`) — **not** ingest/query

---

## 4. Current indexing flow

- Physical Chroma persist dir: `config.persist_dir()` → `<LIBRARY_ROOT>/.rag_db`
- Client: `langchain_chroma.Chroma` + `chroma_client_settings()` (`is_persistent=True`)
- Physical collection name observed: `langchain`
- Tracker: `embedded.json` keyed by content SHA-256
- Fingerprint sidecar written after successful ingest when `new_count > 0` or force or file missing

---

## 5. Current retrieval flow

- `query.py` builds `OllamaEmbeddings(model=embed_model())`
- **No** call to `compare_fingerprint`
- **No** lifecycle ACTIVE filtering (Phase 5 boundary intact)
- Dimension/model mismatch would surface as poor retrieval quality, not a hard gate

---

## 6. Current fingerprint implementation

File: `rag_engine/fingerprint.py`

### Live / stored payload (sidecar v0)

```json
{
  "embed_model": "<string>",
  "llm_model": "<string>",
  "chunk_size": <int>,
  "chunk_overlap": <int>,
  "normalization": "nfkc"
}
```

Production file contents (read-only):

```json
{
  "embed_model": "mxbai-embed-large",
  "llm_model": "qwen2.5:3b",
  "chunk_size": 800,
  "chunk_overlap": 100,
  "normalization": "nfkc"
}
```

### Semantics

| Question | Answer |
|----------|--------|
| Who writes? | `write_fingerprint()` from ingest only |
| Who reads? | `read_fingerprint` / `compare_fingerprint`; doctor; reconciliation evidence helper (read-only) |
| Validated? | JSON parse only; no schema version |
| Enforcement on ingest? | **No** |
| Enforcement on query? | **No** |
| Doctor on mismatch/missing? | Check FAILS (`match=False`) but doctor is advisory |
| Hash? | **None** — raw field snapshot |
| Includes separators/min/max/extractor? | **No** |
| Includes provider/dimension/revision? | **No** |
| Includes distance space? | **No** |
| `llm_model` in compare keys? | **No** (informational); live may differ and still MATCH |

Observed live compare (Phase 6A):

- stored `llm_model=qwen2.5:3b`, live `llm_model=gpt-5.6-luna`
- `compare_fingerprint().status == MATCH` (llm excluded) — proves llm is non-gating today

Atomic write: temp `*.json.tmp` + `replace` (good pattern).

---

## 7. `index_fingerprint.json` findings

| Property | Evidence |
|----------|----------|
| Path | `/Users/vladymyrzub/CE_Library/.rag_db/index_fingerprint.json` |
| Size | 141 bytes |
| Role | Informational drift snapshot for doctor |
| Name vs power | Name suggests authority; semantics are **weak cohort fields only** |
| Sufficient for Spec §7.1? | **No** (already concluded in Phase 4 / HISTORICAL_CHUNKING recovery) |
| Sufficient for `embedding-fp-v1`? | **No** |
| Can certify historical vectors? | **No** |

---

## 8. `embedded.json` findings

| Property | Evidence |
|----------|----------|
| Path | `/Users/vladymyrzub/CE_Library/.rag_db/embedded.json` |
| Entries | 1736 digest keys (sample) |
| Per-entry keys | `paths`, `chunk_ids`, `collection`, `ingested_at` (+ optional `extraction`) |
| Records embed model? | **No** |
| Records chunk config? | **No** |
| Records fingerprint? | **No** |
| Skip logic | `if digest in tracker and not force: continue` without fingerprint check |

**Stale-reuse risk (P0):**
Yes. After an embedding-model or chunk-config change, unchanged file bytes keep the same digest → ingest **skips** re-embedding while Chroma retains old vectors. Fingerprint sidecar may later be rewritten to the new live config when *some other* file is newly embedded (`new_count > 0`), making doctor MATCH while most vectors remain from the prior space.

Proven path: `ingest._run_ingest_locked` L358–363 + L397–405.

---

## 9. Chroma findings (read-only SQLite)

URI: `file:…/chroma.sqlite3?mode=ro`

| Item | Value |
|------|-------|
| Collections | 1 — `langchain` |
| Dimension | **1024** |
| Embedding rows | **124570** |
| ID format | UUID4 strings (0 `chunk:` ids) |
| `collection_metadata` rows | **empty** (no model/fingerprint in collection meta) |
| Segment | HNSW local persisted; `config_json_str` space **`l2`** |
| Chunk metadata keys (top) | `chroma:document`, `source`, `page`, `collection`, plus sparse authority fields |
| Model identity in metadata | **Absent** |

No production client mutation performed. Counts differ from older design docs that cited ~108685 — current evidence is **124570**.

---

## 10. Existing config inputs

| Input | Source | Class |
|-------|--------|-------|
| `embed_model` | env `RAG_EMBED_MODEL` / default `mxbai-embed-large` | **compatibility-critical** (alias risk) |
| Ollama provider | hardcoded `OllamaEmbeddings` | **compatibility-critical** |
| Model revision | not recorded | **unknown / residual risk** |
| Dimension | Chroma collection `1024`; not in sidecar | guardrail |
| `chunk_size` / `chunk_overlap` | scopes defaults 800/100 | **corpus-critical** |
| separators | hardcoded in `ingest._clean_chunks` | **corpus-critical** (missing from sidecar) |
| NFKC normalization | `text.normalize_text` | **corpus-critical** (+ query) |
| min/max chunk chars | hardcoded 50/3000 filters | **corpus-critical** |
| extractor identity | not recorded in sidecar | **corpus-critical / unknown** |
| `llm_model` | env/default | **operational-only** (answer gen) |
| HNSW `ef_search`, threads, M | Chroma collection config | mostly **operational**; `distance_space=l2` is **index-critical** |
| persist path | library `.rag_db` | operational location, not fingerprint input |

---

## 11. Existing compatibility checks

| Check | Where | Blocks mutation? |
|-------|-------|------------------|
| Sidecar field compare | doctor | No (advisory FAIL) |
| Missing sidecar | doctor FAIL | No |
| Ingest fingerprint gate | — | **None** |
| Query fingerprint gate | — | **None** |
| Spec §7.1 historical FP | reconciliation | Read-only; refuses to invent |

---

## 12. Existing tests

| Test | What it proves | Weakness |
|------|----------------|----------|
| `tests/test_hardening.py::test_fingerprint_mismatch_detection` | sidecar field mismatch detection | Does not prove hash contract; does not block ingest; no legacy/malformed/conflict cases |
| `test_doctor_skip_ollama_runs` | doctor returns fingerprint check name | Structure only |
| `tests/test_stable_identity.py` chunking FP tests | Spec §7.1 determinism/sensitivity | Corpus only — not embedding space |
| `tests/test_reconciliation.py` fingerprint evidence | index_fingerprint alone ≠ historical chunk FP | Good negative proof; not embedding-fp-v1 |

**Gaps:** no tests for fail-closed append, UNKNOWN_LEGACY, authority conflict, same-dimension/different-model `efp`, embedded.json skip across model change, malformed sidecar repair ban.

---

## 13. Existing doctor checks

Doctor:

- Calls `compare_fingerprint()`
- FAILs on missing/mismatch of sidecar v0 compare keys
- Does **not** classify UNKNOWN_LEGACY
- Does **not** inspect Chroma collection_metadata for embedding identity (empty anyway)
- Does **not** stop ingest CLI
- Opens Chroma PersistentClient for collection listing (inspection; not fingerprint cert)

Phase 6B should add explicit states: missing-authority, mismatch, malformed, conflict, legacy-unknown, and separate advisory vs mutation-blocking surfaces.

---

## 14. Identified failure modes

### P0

1. **Silent skip after model/config change** via `embedded.json` digest short-circuit.
2. **Sidecar rewrite without full rebuild** can make doctor MATCH while vectors remain mixed/stale.
3. **No ingest/query fail-closed gate** on fingerprint state.
4. **Mutable Ollama model alias** with no revision field.

### P1

5. Sidecar omits separators / min-max / extractor / dimension / distance_space / provider.
6. Empty Chroma collection_metadata — no on-collection embedding identity.
7. Dimension-only thinking would be insufficient (collection has dimension, still not a contract).
8. Prior untracked embedding-identity drafts proposed Chroma-meta authority; must not override registry-first consistency model without evidence.

### P2

9. `llm_model` stored in sidecar confuses operators (differs live, still MATCH).
10. Doctor advisory FAIL easy to ignore in automation.

---

## 15. Production legacy-state classification

**`UNKNOWN_LEGACY`**

Evidence:

- 124570 UUID vectors exist in `langchain` with dimension 1024 and space `l2`.
- No collection_metadata embedding identity.
- Sidecar v0 is a partial field snapshot, not `embedding-fp-v1` / Spec §7.1 proof.
- Tracker does not record model/fingerprint per digest.
- Cannot cryptographically prove all vectors were built under current alias+chunk settings.
- Live config equality with sidecar compare-keys is **not** historical certification (and ingest can refresh sidecar without re-embedding all digests).

Not `KNOWN_COMPATIBLE`. Not proven `KNOWN_INCOMPATIBLE`. Not `CORRUPT` (files parse).

---

## 16. Recommended Phase 6B change boundary

**Required**

- Implement `embedding-fp-v1` builders (pure functions) using stable canonical JSON/SHA helpers
- Schema-versioned authority record (registry table and/or new sidecar filename)
- Ingest preflight: refuse append/skip-as-embedded on UNKNOWN/MISMATCH/CORRUPT/CONFLICT
- Doctor: report contract states without auto-repair writes
- Tests listed in contract §21

**Optional**

- Derived Chroma collection_metadata mirror
- Model revision capture if Ollama exposes stable digests
- Degraded retrieval banner/API flag for UNKNOWN_LEGACY

**Out of scope / do not sneak in**

- Production re-index
- UUID rewrite / chunk_id backfill
- Lifecycle retrieval activation
- Certifying UNKNOWN_LEGACY as compatible
- Auto-delete on mismatch

---

## 17. Risks

- Operators may treat doctor MATCH as certification — documentation must forbid this until `embedding-fp-v1` authority exists.
- Alias drift remains even after Phase 6B if revision stays null.
- Mixing Phase 5 lifecycle activation with fingerprint work could confuse failure modes — keep gates separate.

---

## 18. Non-goals (Phase 6A)

- Runtime enforcement
- Production mutation
- Commit of unrelated untracked audits/backups
- Implementing Phase 6B modules beyond docs/spec

---

## 19. Verification evidence (Phase 6A)

| Check | Result |
|-------|--------|
| Production hashes captured before work | yes (`/tmp/phase6a_prod_before.json`) |
| Read-only Chroma inspection via `mode=ro` | yes |
| No ingest/query code wired | yes (docs only) |
| Contract answers §27 questions | yes — see independent review |
| Safe test suites run | recorded in final report |
| Production unchanged after | required gate |

---

## 20. Final Phase 6A verdict

**PASS** (design + audit complete; production immutable; no runtime activation)

**Production index certification:** `UNKNOWN_LEGACY`
**Phase 6B readiness:** see final report gate (requires independent review PASS)

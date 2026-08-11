# PHASE 6C — LEGACY INDEX TREATMENT

**Status:** Phase 6C complete (mechanism + evidence; production not certified)
**Date:** 2026-08-11
**Baseline HEAD (start):** `66aa07ab953b5e18837d36069b91b7ab82244066`
**Contract:** `docs/LEGACY_INDEX_CERTIFICATION_CONTRACT_V1.md`

Production certification applied: **NO**  
Production reindex: **NO**

---

## 1. Repository baseline

| Item | Value |
|------|-------|
| Root | `/Users/vladymyrzub/CE_Library/Tools/rag_engine` |
| Branch | `main` |
| Starting HEAD | `66aa07a` (Phase 6B) |
| Phase 6B ancestor | PASS (`0`) |

---

## 2. Production baseline

Relevant-file manifest (index state + HNSW segment; excludes `intake_renders` images):

| Path | Notes |
|------|-------|
| `chroma.sqlite3` | 616194048 bytes |
| `embedded.json` | 6237744 bytes |
| `index_fingerprint.json` | v0 advisory |
| HNSW segment dir `52a34a4a-…` | 5 files |
| `index_embedding_fingerprint_v1.json` | **absent** |
| `.rag_state` | **absent** |

Baseline JSON: `/private/tmp/rag_engine_phase6c_baseline/before.json`

---

## 3. Historical evidence reviewed

- Git history for `embedding`, `ollama`, `index_fingerprint`, `chunk_size`, `mxbai`, `OllamaEmbeddings`, `write_fingerprint`
- Historical `scopes.yaml` / `ingest.py` / `fingerprint.py` via `git show`
- Feature-branch embedding-identity work (`e17de2f`) — **not on main production path**
- Read-only Chroma SQLite copy under `/private/tmp/rag_engine_phase6c_forensics/`
- Production `embedded.json` and v0 fingerprint (read-only)

---

## 4. Historical model findings

| Field | Historical value | Evidence | Confidence |
|-------|------------------|----------|------------|
| embedding_provider | ollama | code path `OllamaEmbeddings` since scoped RAG | high (supporting) |
| embedding_model | `mxbai-embed-large` | scopes default since `08672f4` (2026-07-21); v0 file | medium (alias) |
| embedding_model_revision | UNKNOWN | no digest/manifest captured | none |
| embedding_dimension | 1024 | Chroma `collections.dimension` | high (guardrail only) |
| embedding_normalization | null / unknown provider-side | not recorded historically | low |
| embedding_mode | `symmetric_ollama_v1` (current contract label) | inferred from LangChain path | low–medium |
| tokenizer_id | null | not exposed | unknown |
| max_input_tokens | null | not recorded | unknown |

Ollama tag is a **mutable alias**. Alias equality ≠ immutable model identity.

---

## 5. Historical chunk findings

| Field | Value | Evidence | Confidence |
|-------|-------|----------|------------|
| chunk_size | 800 | scopes defaults since tool packaging; v0 | medium |
| chunk_overlap | 100 | same | medium |
| separators | `["\n\n","\n","."," "]` | ingest `_clean_chunks` since early package | medium |
| normalization | nfkc | `normalize_text` / v0 | medium |
| min/max chunk chars | 51 / 2999 | ingest filters (`len > 50`, `< 3000`) | medium |
| extractor | unknown | not recorded in tracker/sidecar | low |
| extractor_version | UNKNOWN | not recorded | low |
| composition | page_content_nfkc_v1 | current path embeds normalized page_content | medium (inferred) |

No proof these never changed across all 124k vectors.

---

## 6. Historical index findings

| Field | Value | Evidence | Confidence |
|-------|-------|----------|------------|
| vector_store | chroma | persist layout | high |
| distance_space | l2 | collection `config_json_str` | high (current snapshot) |
| physical_collection_name | langchain | collections table | high |
| collection_metadata embedding id | empty | SQLite | proves absence of on-collection identity |

---

## 7. Mixed-history assessment

**POSSIBLE_MIXING**

Reasons:

- Primary bulk ingest ~2026-07-21 (~105k embeddings) then additional cohorts 2026-07-26, 2026-08-08, 2026-08-10
- Pre–Phase 6B ingest had **no** embedding fingerprint gate; `embedded.json` digest skip could retain old vectors after config change
- v0 fingerprint can be rewritten on later ingest without full rebuild
- No immutable model digest for the Ollama alias across the window
- No Level-A exclusion proof of single embedding space

Not `PROVEN_MIXING` (no direct evidence of two model names in metadata).

---

## 8. V0 fingerprint evidence value

**UNRELIABLE** (as sole certification basis); at best **WEAK** supporting.

Reason: written/refreshed by ingest from **live** config; can update without re-embedding all digests; omits provider/dimension/revision/distance/separators/extractor/composition; no schema version.

---

## 9. embedded.json evidence value

**WEAK** / supporting for timeline only.

Stores paths, chunk_ids, ingested_at, collection, optional extraction. Does **not** store embed model, fingerprint, or chunk contract. 1673/1736 entries lack `extraction` (pre–F-02).

---

## 10. Chroma forensic findings

- Collection `langchain`, id `e42037ad-…`, dimension **1024**, HNSW space **l2**
- Embedding rows ≈ **124569** (join count 124569)
- Monthly: 2026-07 ≈ 108686; 2026-08 ≈ 15884
- Peak day 2026-07-21 ≈ 105079
- `collection_metadata` empty
- Metadata keys include source/page/collection/authority fields; **no model identity**

---

## 11. Certification decision (tooling)

Decision engine states implemented: `CERTIFIABLE`, `NOT_CERTIFIABLE`,
`INSUFFICIENT_EVIDENCE`, `EVIDENCE_CONFLICT`, `MIXED_HISTORY_SUSPECTED`,
`REBUILD_REQUIRED`.

---

## 12. Production certifiability verdict

**MIXED_HISTORY_SUSPECTED**

(with underlying `INSUFFICIENT_EVIDENCE` for Level A/B coverage and
`POSSIBLE_MIXING` without Level-A exclusion)

---

## 13. Operator workflow

```text
rag-engine fingerprint inspect-legacy --persist-dir DIR [--json]
rag-engine fingerprint certify-legacy --persist-dir DIR --manifest PATH \
  [--reason TEXT] [--apply] [--json]
```

Dry-run is default. `--apply` required for writes. No production default path.

---

## 14. Implementation summary

| Module | Role |
|--------|------|
| `index_compatibility/certification.py` | inspect / evaluate / manifest / certify |
| `exceptions.py` | certification error types |
| `cli.py` | `fingerprint` subcommands |

---

## 15. Test results

See Phase 6C final report (focused + broader suites).

---

## 16. Production immutability

Required gate: `CHANGED_COUNT=0` after Phase 6C work.

---

## 17. Rebuild requirements if needed

Future Phase 6D should:

- build a **parallel** candidate index (not in-place overwrite)
- establish v1 fingerprint before first vector
- preserve stable IDs where contract permits
- retain old index until validation
- reconcile counts/coverage/citations/performance
- atomic cutover + rollback

Do **not** execute in 6C.

---

## 18. Remaining issues

- No recoverable Ollama model digest for historical alias
- Multi-day append cohorts without fingerprint gate
- Production remains operational for **degraded retrieval** only until rebuild/certification execution phase

---

## 19. Next-phase recommendation

**REBUILD_PATH_REQUIRED** — design/execute a controlled parallel rebuild + cutover plan for the UNKNOWN_LEGACY production index (separate explicit approval).

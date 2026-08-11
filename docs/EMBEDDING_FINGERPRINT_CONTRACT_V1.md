# EMBEDDING FINGERPRINT CONTRACT V1

**Status:** Phase 6A design contract (frozen for Phase 6B implementation planning)
**Date:** 2026-08-11
**fingerprint_schema_version:** `embedding-fp-v1`
**Baseline HEAD:** `9a85dade34d54adda6250626d6c551da1991cf99`
**Production authority:** NOT activated — this document does not change live ingest/retrieval.

---

## 1. Purpose

Define a deterministic, fail-closed compatibility contract so the system can distinguish:

| Claim | Meaning |
|-------|---------|
| Same document / source hash | Same bytes |
| Same chunk text / chunk_id | Same logical chunk under a chunking contract |
| Same vector dimension | Same output width |
| Same embedding/index contract | Vectors are comparable in one collection |

Phase 6A establishes the contract. Phase 6B implements enforcement. Phase 6A does **not** certify the existing production index as compatible.

---

## 2. Scope

In scope:

- Canonical embedding, corpus/chunk, and index fingerprint contracts
- Serialization and hashing rules
- Compatibility / legacy / conflict decision matrix
- Authoritative storage model (design)
- Retrieval, ingest-append, and re-index policies for Phase 6B
- Boundaries vs stable identity (Phases 2–5) and document lifecycle (Phase 5)
- Phase 6B test requirements

Out of scope (Phase 6A):

- Wiring enforcement into live ingest/query
- Writing fingerprints into production Chroma / registry
- Re-index, backfill, UUID rewrite, registry population
- Activating lifecycle filtering in retrieval/ingest
- Certifying historical production vectors without evidence

---

## 3. Definitions

| Term | Definition |
|------|------------|
| **Embedding contract** | Parameters that determine the numerical embedding space and how text is encoded into vectors. |
| **Corpus/chunk contract** | Parameters that determine how source bytes become the text units that are embedded. |
| **Index contract** | Combined compatibility contract for a stored vector collection (embedding + corpus + store metric/schema). |
| **Embedding fingerprint (`efp`)** | SHA-256 of the canonical embedding contract JSON. |
| **Corpus fingerprint (`cfp`)** | SHA-256 of the canonical corpus/chunk contract JSON (aligned with Spec §7.1 `chunking_fingerprint`). |
| **Index fingerprint (`ifp`)** | SHA-256 of the canonical index contract JSON (includes `efp`, `cfp`, and store fields). |
| **Sidecar v0** | Today’s `.rag_db/index_fingerprint.json` (plain field snapshot; **not** `embedding-fp-v1`). |
| **UNKNOWN_LEGACY** | Existing vectors/state whose historical contract cannot be proven. |

---

## 4. Threat / failure model

Primary failures this contract exists to prevent:

1. **Silent mixed embedding spaces** — vectors from different models/configs in one collection.
2. **Stale skip** — `embedded.json` treats content as “already embedded” after model/chunk-config change.
3. **False confidence from dimension** — same `dimension` treated as compatibility.
4. **False confidence from source/chunk hash** — same text treated as same vector contract.
5. **False confidence from stable `chunk_id`** — identity reused across incompatible embedding contracts.
6. **Alias drift** — mutable model names (e.g. Ollama tags) silently change weights.
7. **Authority drift** — disagreeing copies of fingerprint state; “pick whichever exists”.
8. **Auto-mutation** — mismatch triggering re-index, delete, or rewrite without explicit operator workflow.

---

## 5. Embedding fingerprint contract

### 5.1 Canonical object (`embedding_contract`)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `fingerprint_schema_version` | string | yes | Always `embedding-fp-v1` inside nested objects when hashed alone; see §8. |
| `embedding_provider` | string | yes | Current production: `"ollama"`. |
| `embedding_model` | string | yes | Configured model name/alias (e.g. `mxbai-embed-large`). |
| `embedding_model_revision` | string \| null | yes | Strongest available immutable revision if knowable; else `null`. |
| `embedding_dimension` | integer | yes | Expected output dimension (guardrail; **not** sufficiency). |
| `embedding_normalization` | string \| null | yes | Provider-side vector L2-normalization policy if applicable; else `null`. |
| `embedding_mode` | string | yes | How query vs document text is encoded. Current LangChain `OllamaEmbeddings` path: `"symmetric_ollama_v1"`. |
| `tokenizer_id` | string \| null | yes | Explicit tokenizer identity if exposed; else `null`. |
| `max_input_tokens` | integer \| null | yes | Truncation policy if part of encoding; else `null`. |

### 5.2 Residual risk (model alias)

Ollama model tags such as `mxbai-embed-large` are **mutable aliases**. When `embedding_model_revision` is `null`, two runs with the same alias may still differ in weights.

Classification:

- Record alias + dimension + provider honestly.
- Do **not** pretend an immutable revision exists.
- Phase 6B may later add an optional recorded digest of pulled model metadata if Ollama exposes one; until then residual alias risk remains **accepted and documented**, not hidden.

### 5.3 Explicitly excluded from embedding fingerprint

- `llm_model` / answer-generation model (not embedding space)
- Absolute paths, venv, hostname, username, cwd, PID, timestamps
- Batch size / worker count (unless proven to change numerical outputs)
- Logging level, UI settings
- Chroma persist directory path

---

## 6. Corpus / chunk fingerprint contract

### 6.1 Alignment with Spec §7.1

Phase 2 already defines a deterministic `chunking_fingerprint` over a Spec §7.1 contract
(`rag_engine.stable_identity.canonical.default_chunking_contract` /
`chunking_fingerprint`).

Phase 6A **reuses that corpus contract** as `corpus_contract` and adds one composition field.

### 6.2 Canonical object (`corpus_contract`)

| Field | Type | Required | Current production-era values (evidence-based defaults) |
|-------|------|----------|-----------------------------------------------------------|
| `identity_scheme_version` | string | yes | `stable-id-v1` (for future native chunk IDs; legacy UUIDs still dominate store) |
| `chunk_size` | int | yes | `800` |
| `chunk_overlap` | int | yes | `100` |
| `separators` | array[string] | yes | `["\n\n","\n","."," "]` |
| `normalization` | string | yes | `nfkc` (see `rag_engine.text.normalize_text`) |
| `min_chunk_chars` | int | yes | `51` (filter `len > 50`) |
| `max_chunk_chars` | int | yes | `2999` (filter `len < 3000`) |
| `extractor` | string | yes | Must be explicit; do not invent. Use recorded extractor id when known; else `"unknown"`. |
| `extractor_version` | string | yes | Explicit or `"UNKNOWN"`. |
| `embedded_text_composition_version` | string | yes | Current path embeds normalized `page_content` only → `"page_content_nfkc_v1"`. |

`cfp = SHA-256(canonical_json(corpus_contract))` (64 lowercase hex), same algorithm as Spec §7.1.

### 6.3 Notes

- Changing separators / min/max / composition changes `cfp` even if size/overlap stay equal.
- Today’s sidecar v0 does **not** include separators/min/max/extractor/composition → insufficient for Spec §7.1 / Phase 6A corpus proof.

---

## 7. Index fingerprint contract

### 7.1 Canonical object (`index_contract`)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `fingerprint_schema_version` | string | yes | `embedding-fp-v1` |
| `embedding_fingerprint` | string | yes | 64-hex `efp` |
| `corpus_fingerprint` | string | yes | 64-hex `cfp` |
| `vector_store` | string | yes | `"chroma"` |
| `distance_space` | string | yes | Production HNSW space: `"l2"` |
| `physical_collection_name` | string | yes | Production: `"langchain"` |
| `index_schema_notes` | string \| null | yes | Optional free-form null-safe; default `null`. Not for machine-noise. |

`ifp = SHA-256(canonical_json(index_contract))`.

### 7.2 Binding rule

An `ifp` is meaningful only when bound to a specific physical collection identity
(`physical_collection_name` + store root policy). Sidecar/registry rows must
name the collection they certify.

---

## 8. Canonical serialization rules

Shared rules for `embedding_contract`, `corpus_contract`, and `index_contract`:

1. UTF-8 JSON object
2. `sort_keys=True`
3. Separators: `(",", ":")` (compact; no spaces)
4. `ensure_ascii=False`
5. `allow_nan=False`
6. Integers as JSON numbers (no floats in contracts; floats fail closed)
7. Booleans as JSON true/false (if ever needed)
8. `null` is a distinct value and must be present when the field is required-nullable
9. Arrays preserve element order (separators are order-significant)
10. No absolute paths; no host-specific values
11. String trimming: field values must already be canonical; do not silently trim inside hash preimage beyond what constructors define

Hash:

- Algorithm: SHA-256 over UTF-8 bytes of canonical JSON
- Encoding: lowercase hexadecimal, length 64
- Optional display prefix (not stored inside hashed object):
  - `efp:<hex>`
  - `cfp:<hex>`
  - `ifp:<hex>`

Determinism requirement: same logical contract ⇒ same fingerprint across process restarts and dict key insertion orders.

---

## 9. Hashing rules (summary)

```text
efp = sha256_hex(canonical_json(embedding_contract_without_redundant_wrapper))
cfp = sha256_hex(canonical_json(corpus_contract))
ifp = sha256_hex(canonical_json(index_contract including efp + cfp))
```

Implementation note for Phase 6B: reuse `rag_engine.stable_identity.canonical` patterns
(`canonicalize_chunking_contract` / `sha256_utf8`) rather than inventing a second canonicalizer.

---

## 10. Required vs optional fields

- All fields listed in §§5–7 as “Required = yes” are mandatory in `embedding-fp-v1`.
- Optional future fields require a **new** `fingerprint_schema_version`.
- Unknown keys in a stored `embedding-fp-v1` object ⇒ **malformed / unsupported** (fail closed), not silently ignored.

---

## 11. Compatibility rules

Two index states are **compatible** iff:

1. `fingerprint_schema_version` is supported, and
2. `ifp` equal (equivalently: equal `efp`, `cfp`, `distance_space`, `vector_store`, and bound collection identity per policy).

Guards that are **necessary but not sufficient**:

- equal `embedding_dimension`
- equal source hash / document_id
- equal chunk_id
- equal sidecar v0 field snapshot

---

## 12. Legacy / unknown behavior

| State | Meaning |
|-------|---------|
| `KNOWN_COMPATIBLE` | Historical contract proven and matches runtime `ifp`. |
| `KNOWN_INCOMPATIBLE` | Historical contract proven and disagrees with runtime. |
| `UNKNOWN_LEGACY` | Vectors/state exist but historical contract cannot be proven. |
| `CORRUPT` | Malformed fingerprint payload. |
| `CONFLICT` | Authoritative copies disagree. |

**Certification ban:** current runtime config alone must never upgrade `UNKNOWN_LEGACY` → `KNOWN_COMPATIBLE`.

Sidecar v0 (`index_fingerprint.json`) is evidence of a **partial cohort snapshot only**, not Spec §7.1 / `embedding-fp-v1` proof.

---

## 13. Source-of-truth / storage rules

### 13.1 Target authority (Phase 6B+)

**Authoritative:** metadata registry (SQLite) rows bound to physical collection identity,
schema-versioned, transactional, auditable.

**Derived mirrors (optional):**

- Sidecar file under persist dir (operational convenience)
- Chroma `collection_metadata` (non-authoritative cache)

### 13.2 Consistency model

1. Registry (or interim Phase 6B sidecar **v1 schema**) is authoritative.
2. Mirrors may lag only inside an explicit write transaction that rolls back on failure.
3. On read: if any present copy disagrees with authority ⇒ `CONFLICT` (fail closed).
4. Missing authority with existing vectors ⇒ `UNKNOWN_LEGACY` (not compatible).
5. Chroma metadata alone is **never** sufficient to certify compatibility.

### 13.3 Interim Phase 6B note

Until the production registry is populated, Phase 6B may introduce a **new**
schema-versioned sidecar (recommended name distinct from v0), e.g.
`index_embedding_fingerprint_v1.json`, without treating v0 as certified.

Read/check must **not** rewrite missing fingerprints into production.

---

## 14. Retrieval policy (Phase 6B)

| Index state | Retrieval |
|-------------|-----------|
| `KNOWN_COMPATIBLE` | Allowed |
| `UNKNOWN_LEGACY` | Allowed only as **degraded/legacy** with explicit status in doctor/API logs; never labeled compatible |
| `KNOWN_INCOMPATIBLE` | Blocked by default |
| `CORRUPT` / `CONFLICT` | Blocked |

Phase 6A: retrieval behavior **unchanged** (no gate added).

---

## 15. Ingest / append policy (Phase 6B)

Appending/upserting new vectors into a collection is:

| Index state | Append |
|-------------|--------|
| `KNOWN_COMPATIBLE` + runtime `ifp` match | Allowed |
| `UNKNOWN_LEGACY` | **Forbidden** (fail closed) |
| `KNOWN_INCOMPATIBLE` / mismatch | **Forbidden** |
| `CORRUPT` / `CONFLICT` | **Forbidden** |

`embedded.json` digest presence must **not** authorize skip/reuse across fingerprint mismatch.

Phase 6A: ingest behavior **unchanged** (no gate added).

---

## 16. Rebuild / re-index policy

- Fingerprint mismatch / unknown legacy **must not** auto-reindex.
- Fingerprint mismatch **must not** auto-delete or rewrite vectors.
- Rebuild requires an explicit operator command/workflow with backup + receipt.
- Writing a new fingerprint without rebuilding is forbidden for certification.

---

## 17. Stable identity relationship

| Concern | Answers |
|---------|---------|
| `subject_id` / `document_id` / `chunk_id` | *What logical object is this?* |
| `efp` / `cfp` / `ifp` | *Under what embedding/index contract was this vector produced?* |

Rules:

- Stable IDs are not embedding compatibility certificates.
- A stable `chunk_id` must not make an incompatible vector reusable after contract change.
- Legacy Chroma UUID `chroma_embedding_id` remains a physical handle; mapping does not imply contract match.
- Spec §7.1 `chunking_fingerprint` is the corpus half of this design; it is necessary for native chunk identity and for `cfp`, but not for embedding-space identity.

---

## 18. Lifecycle relationship (Phase 5)

Lifecycle states (`ACTIVE`, `SUPERSEDED`, `ARCHIVED`, `REPLACED`, `WITHDRAWN`, `DUPLICATE`) govern **revision currency**, not embedding-space compatibility.

- Withdrawing/superseding a revision does not change `ifp`.
- An ACTIVE revision can still sit on an `UNKNOWN_LEGACY` index.
- Phase 5 must remain non-activated in retrieval/ingest until separately gated; fingerprint gates are independent.

---

## 19. Error classes expected for Phase 6B

| Class | When |
|-------|------|
| `FingerprintMissingError` | Authority missing while vectors exist / mutation requested |
| `FingerprintMismatchError` | Runtime `ifp` ≠ stored authority |
| `FingerprintMalformedError` | JSON/schema invalid |
| `FingerprintUnsupportedVersionError` | Unknown `fingerprint_schema_version` |
| `FingerprintConflictError` | Authority vs mirror disagreement |
| `FingerprintLegacyBlockedError` | Mutation attempted on `UNKNOWN_LEGACY` |
| `FingerprintIncompatibleRetrievalError` | Retrieval blocked for incompatible/corrupt/conflict |

---

## 20. Migration principles

1. Preserve production vectors until an explicit rebuild.
2. Classify honestly (`UNKNOWN_LEGACY` by default when unproven).
3. Do not backfill “compatible” from live config.
4. Prefer additive registry/sidecar schema; do not weaken fail-closed rules.
5. Dimension / source-hash / chunk-id equality never certify migration success.

---

## 21. Test requirements (Phase 6B)

See companion audit §22 / this section’s checklist:

1. Determinism (reorder keys, fresh process)
2. Sensitivity (each compatibility-critical field)
3. Insensitivity (paths, timestamps, llm_model, batch size if irrelevant)
4. Runtime mismatch fail-closed on append
5. Missing → `UNKNOWN_LEGACY` (not compatible)
6. Malformed → hard failure; no auto-repair write on read
7. Conflict between stores → hard conflict
8. Transactionality of authority writes
9. No silent rewrite on doctor/read
10. Same dimension / different model ⇒ different `efp`
11. Same source hash / different model ⇒ still incompatible for append
12. Stable chunk_id does not bypass gate
13. Production inspection path does not certify or mutate

---

## 22. Explicit non-goals

- Certifying today’s production Chroma index as `KNOWN_COMPATIBLE`
- Using sidecar v0 as `embedding-fp-v1`
- Treating `llm_model` as embedding-critical
- Treating HNSW `ef_search` / thread count as compatibility-critical by default
- Activating lifecycle filters in the same breath as fingerprint work
- Automatic production re-index on mismatch
- Claiming Ollama aliases are immutable

---

## 23. Compatibility decision matrix (normative)

| Stored | Runtime | Result |
|--------|---------|--------|
| exact `ifp` match | exact match | `KNOWN_COMPATIBLE` |
| missing authority | present runtime | `UNKNOWN_LEGACY` (vectors exist) / config error if mutation requested without resolution workflow |
| present authority | missing runtime contract builder | configuration error |
| `ifp` mismatch | mismatch | `KNOWN_INCOMPATIBLE` |
| malformed | any | `CORRUPT` |
| unknown schema version | current | unsupported (`CORRUPT`/unsupported) |
| valid legacy schema | current | explicit migration path only — never silent coerce |
| sidecar missing, Chroma meta present | — | Chroma alone ≠ authority; still `UNKNOWN_LEGACY` unless registry/sidecar v1 authority exists |
| sidecar v1 disagrees with registry | — | `CONFLICT` |
| sidecar v0 present | live field match | **Not** `KNOWN_COMPATIBLE` under this contract; evidence-only / `UNKNOWN_LEGACY` |

---

## 24. Current production classification under this contract

See `docs/PHASE6A_EMBEDDING_FINGERPRINT_AUDIT.md`.

Expected default without stronger historical proof:

**`UNKNOWN_LEGACY`**

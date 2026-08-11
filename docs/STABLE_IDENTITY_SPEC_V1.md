# STABLE IDENTITY SPECIFICATION V1

**Status:** FROZEN for Phase 1 (P0)
**identity_scheme_version:** `stable-id-v1`
**Date:** 2026-08-11
**Supersedes (naming only):** terminology in `docs/RAG_STABLE_IDENTIFIER_SPECIFICATION_V1.md` where it conflicts; see §4.1 synonym map.
**Does not implement:** production ID generation, registry population, or re-index.

---

## 1. Purpose

Define a deterministic, path-independent identity contract for:

- logical document lineage (`subject_id`)
- immutable document/revision objects (`document_id`)
- governed chunks (`chunk_id`)

sufficient to implement Phase 2 without inventing identity rules ad hoc, and without blocking Phase 5 lifecycle/lineage.

---

## 2. Scope

In scope:

- Identity rules, formats, hashing purposes, path normalization
- Duplicate / revision / collision semantics
- Chroma ↔ registry mapping rules
- Backward compatibility and preliminary migration/re-index decisions
- Authority boundaries

Out of scope (Phase 1):

- Writing IDs into production
- Schema application to production `.rag_state`
- Re-index / embedding rebuild
- Full lifecycle state machine implementation
- Confidence-gate changes

---

## 3. Non-goals

- Treating Chroma UUID4s as business identity
- Treating `source` path as sole identity
- Auto-merging manuals merely because they share maker/equipment folders
- Silent overwrite on identity conflict
- Replacing the existing registry schema wholesale in Phase 1

---

## 4. Terminology

| Term | Definition |
|------|------------|
| **subject_id** | Logical lineage identity for one technical document family (survives path moves, renames, and revisions). |
| **document_id** | Identity of **one immutable content/revision object** (byte-stable physical artifact under this scheme). |
| **chunk_id** | Deterministic identity of one chunk under a specific identity scheme + chunking fingerprint + ordinal. |
| **source_hash** | SHA-256 of raw file bytes. Content fingerprint, not lineage. |
| **content_hash** | SHA-256 of normalized extracted text for a document or chunk (diagnostic / reconciliation). |
| **chunking_fingerprint** | Deterministic digest of chunking/extraction contract parameters. |
| **source_path / relative_path** | Mutable locator under library root. |
| **chroma_embedding_id** | Physical Chroma vector ID (today: UUID4). |
| **identity_scheme_version** | Version tag for this ID algorithm family. Current: `stable-id-v1`. |

### 4.1 Synonym map to existing registry design

| Phase-1 term | Registry schema v1 term | Notes |
|--------------|-------------------------|-------|
| `subject_id` | `documents.document_id` | Logical row. Prefer storing Phase-1 `subject_id` value in that PK column going forward, **or** add `subject_id` column alias in a later migration — Phase 2 chooses additive compatibility (see Phase2 plan). |
| `document_id` | `document_versions.document_version_id` | Immutable revision row. |
| `source_hash` | `document_versions.source_hash` / `source_files.source_hash` | Same meaning. |
| `source_path` | `source_files.relative_path` | Locator. |

**Naming rationale:** Phase 1 adopts `subject_id` + `document_id` as required by the Production Evolution Roadmap. Prior docs used `document_id` for logical identity; that prior meaning is **retired** for new work and retained only via the synonym map.

**No separate `revision_id`:** `document_id` *is* the revision object.

---

## 5. subject_id rule

### 5.1 Format

```text
subject_id = "subj:" <body>
```

Body forms (priority order):

1. **Strong business key (deterministic)**
   `subj:key:<kind>:<normalized_key>`
   where `<kind>` ∈ {`imo_doc`, `sms`, `sire`, `reg`, `maker_doc`, `manual_family`} and `<normalized_key>` is a lowercase ASCII slug (see §9).

2. **Registry-assigned UUID (when key insufficient or ambiguous)**
   `subj:uuid:<uuid4>`
   Assigned once at first accepted registry establishment; never recomputed from path.

3. **Provisional (pre-review)**
   `subj:pending:<source_hash>`
   Temporary per physical artifact until human/registry review links lineage.
   **Must not** be treated as final lineage merge key across revisions.

### 5.2 Generation policy

| Signal | May auto-propose subject? | May auto-commit without review? |
|--------|---------------------------|----------------------------------|
| Exact same `source_hash` already linked to a subject | Yes (inherit) | Yes |
| Explicit document number + maker/issuer (high confidence extract) | Yes | **No** — proposal only |
| IMO + vessel doc number | Yes | **No** unless vessel_id already governed |
| Maker/model folder alone | **No** | **No** |
| Filename similarity alone | **No** | **No** |
| OCR vs original path pairing | Propose DERIVATIVE link | **No** auto-merge of subjects without review when hashes differ |

### 5.3 Survival / non-merge rules

Must survive: filename changes, path moves, new revisions (new `document_id` under same subject).
Must **not** merge: unrelated manuals that share maker/equipment directory.

### 5.4 Unknown representation

If no accepted subject exists: store `subject_id = subj:pending:<source_hash>` and `human_review_status = required`.

### 5.5 Collision behavior

If two distinct accepted subjects claim the same strong key → **STOP / REVIEW** (see §17).
Do not silently pick one.

---

## 6. document_id rule

### 6.1 Canonical rule (ONE)

```text
document_id = "docrev:" + source_hash
source_hash = sha256_hex(raw_file_bytes)   # lowercase 64 hex
```

### 6.2 Required properties

| Property | How satisfied |
|----------|---------------|
| Deterministic | Pure function of bytes |
| Immutable | Bytes change ⇒ new `document_id` |
| Collision-resistant | SHA-256 |
| Path-independent | Path excluded from inputs |
| Duplicate detection | Identical bytes ⇒ identical `document_id` |
| Does not merge different binaries | Different scans/OCR ⇒ different IDs |

### 6.3 Why not “document_id = subject + revision label” alone?

Revision labels in filenames are unreliable and non-unique. Byte identity is already proven in production via the tracker. Lifecycle/revision **labels** attach as metadata on the registry row; they do not form the primary key.

### 6.4 content_hash vs document_id

| Field | Input | Purpose |
|-------|-------|---------|
| `source_hash` / `document_id` body | Raw bytes | Physical artifact identity |
| `content_hash` | NFKC-normalized full extracted text (or approved extractor output) | Detect extract drift; near-duplicate research; **not** primary `document_id` |

Raw SHA-256 is **sufficient** for `document_id` under `stable-id-v1`.
It is **not** sufficient alone for *semantic* “same manual revision” when binaries differ (scans/OCR) — those are separate `document_id`s linked by `subject_id` + relationship type `DERIVATIVE` / review.

### 6.5 Relationship to prior `docver:` helper

Branch `deterministic_document_version_id(...)` mixes `document_id`+revision+status+hash.
For `stable-id-v1`, **do not use status in the ID**. Status is mutable lifecycle metadata. Prefer `docrev:<source_hash>`.

---

## 7. chunk_id rule

### 7.1 Chosen scheme: Option E (hybrid deterministic)

Canonical:

```text
token = identity_scheme_version
      + "|" + document_id
      + "|" + chunking_fingerprint
      + "|" + decimal_ordinal   # 0-based, no leading zeros required in token

chunk_id = "chunk:" + sha256_utf8(token).hexdigest()[:32]
```

Where:

- `ordinal` = index of the chunk in the **deterministic post-filter chunk list** for that document under the fingerprint (stable order = splitter output order after `normalize_text` + length filter, as in current ingest).
- `chunking_fingerprint` = `sha256_hex(canonical_json(chunking_contract))` with keys sorted:

```json
{
  "identity_scheme_version": "stable-id-v1",
  "chunk_size": 800,
  "chunk_overlap": 100,
  "separators": ["\n\n", "\n", ".", " "],
  "normalization": "nfkc",
  "min_chunk_chars": 51,
  "max_chunk_chars": 2999,
  "extractor": "<name>",
  "extractor_version": "<version_or_UNKNOWN>"
}
```

(Current production defaults shown; live values must be read from config/fingerprint at generation time.)

### 7.2 Companion fields (mandatory in registry; optional compact copy in Chroma later)

- `chunk_ordinal` (int)
- `page` (int or null)
- `chunking_fingerprint`
- `content_hash` (hash of chunk text after normalization)
- `document_id`
- `identity_scheme_version`

### 7.3 Requirement coverage

| Requirement | Mechanism |
|-------------|-----------|
| Same doc + same chunking ⇒ same IDs | token includes doc + fingerprint + ordinal |
| Changed boundaries ⇒ no silent reuse | fingerprint changes ⇒ all new chunk_ids |
| Same text twice in one doc | distinct ordinals |
| Same text in different docs | distinct `document_id` |
| Order-independent of filesystem walk | IDs derived per document, not global sequence |
| Chroma reconciliation | map table + optional Chroma id = chunk_id for new writes |
| Compact / inspectable | 32-hex body; ordinal stored alongside |

### 7.4 Explicitly rejected as sole rule

- **A** (doc+ordinal only): silent reuse across chunking changes
- **B** (doc+page+ordinal): page splits still change under re-chunk; insufficient alone
- **C** (content hash only): collisions on repeated text
- **D** without content safeguards: acceptable base but needs ordinal; E is D+hash packaging

---

## 8. identity_scheme_version

```text
identity_scheme_version = "stable-id-v1"
```

### Storage

| Store | Field |
|-------|-------|
| Registry document_version / chunk rows | `identity_scheme_version` TEXT NOT NULL |
| Generated chunk/document metadata | same |
| Chroma metadata (later governed copy) | `identity_scheme_version` allowed as compact scalar |
| Index / build fingerprint | may echo scheme version once IDs are emitted |

Any change to subject/document/chunk generation algorithms **requires** a new version string (e.g. `stable-id-v2`) and must not silently reinterpret old IDs.

---

## 9. Path normalization

### 9.1 Canonical relative path (`relative_path`)

1. Resolve under configured `library_root` (reject escape / `..`).
2. Convert to relative path from `library_root`.
3. Replace `\` with `/`.
4. Strip leading `./`.
5. Do **not** use path as `document_id` or sole `subject_id`.
6. Preserve path segment case as stored on disk (do not case-fold paths for identity).
7. Unicode: store paths as NFC for registry writes (Phase 2); if filesystem returns NFD (macOS), normalize to NFC for registry keys. **Do not** NFKC-normalize paths (would mutate filenames).

### 9.2 OCR locator policy

- Physical OCR file path may end with `_ocr.pdf` / `_OCR.pdf` (case-insensitive match on suffix).
- For **citation display / authority ranking**, continue using `canonical_source_path` (strip OCR suffix) as today.
- For **identity**, OCR file bytes ⇒ distinct `document_id`; classify relationship as `DERIVATIVE` of subject (review-assisted).

### 9.3 source_path role (explicit)

**Answer: C — mutable locator only.**

May be used as:
- inventory key for presence checks
- input to *proposals* for subject matching
- Chroma filter field `source`

Must not be:
- sole `subject_id` or `document_id`

---

## 10. Hash definitions

| Name | Algorithm | Exact inputs | Path? | Metadata? | Chunking params? | Extractor? | Purpose |
|------|-----------|--------------|-------|-----------|------------------|------------|---------|
| `source_hash` | SHA-256 | Raw file bytes | No | No | No | No | Physical content; `document_id` body |
| `content_hash` (document) | SHA-256 | NFKC-normalized full extracted text | No | No | No | Implicit via text | Extract drift / diagnostics |
| `content_hash` (chunk) | SHA-256 | NFKC-normalized chunk text | No | No | No | Implicit | Chunk integrity / collision checks |
| `chunking_fingerprint` | SHA-256 | Canonical JSON contract (§7) | No | No | **Yes** | **Yes** | Chunk ID stability boundary |
| `document_id` | prefixed `source_hash` | — | No | No | No | No | Immutable revision ID |
| Index fingerprint (existing) | JSON file | embed/chunk/normalization config | No | No | Partial | No | Global drift WARN/FAIL — **not** per-doc ID |

**Rule:** Do not reuse one hash field for multiple semantic purposes without renaming.

---

## 11. Duplicate semantics

| Case | Classification | subject_id | document_id | Notes |
|------|----------------|------------|-------------|-------|
| Same bytes, same path | SAME_PHYSICAL_CONTENT | same | same | Idempotent ingest |
| Same bytes, different filename/dir | DUPLICATE_COPY | same | **same** | Multiple `relative_path` locators |
| Different bytes, same technical meaning (rescan) | UNKNOWN / HUMAN_REVIEW_REQUIRED | maybe same | different | Do not auto-merge |
| Original vs OCR | DERIVATIVE | same (after review) | different | Link via relationship |
| Metadata-only packaging change, same bytes | SAME_PHYSICAL_CONTENT | same | same | |
| Rev.A vs Rev.B | NEW_REVISION | same | different | Lineage fields on registry |
| Same filename, different bytes/docs | DIFFERENT_DOCUMENT | different | different | |
| Career + vessel byte-identical copy | DUPLICATE_COPY | same | same | Applicability may differ later |

---

## 12. Revision semantics

- A new `document_id` under an existing `subject_id` represents a new revision object.
- Lifecycle state lives on registry rows, not inside IDs.
- Minimum future states to support (Phase 5):
  `ACTIVE`, `SUPERSEDED`, `ARCHIVED`, `REPLACED`, `WITHDRAWN`, `DUPLICATE`.
- Replacing content at a path with new bytes: new `document_id`; old row remains for lineage (no silent destroy).

---

## 13. Lineage expectations (anticipate, do not implement)

Registry should eventually support (names indicative):

| Field | Meaning |
|-------|---------|
| `replaces_document_id` | Prior revision this supersedes |
| `superseded_by` | Successor document_id |
| `duplicate_of` | Points to canonical document_id for DUPLICATE_COPY policy records if needed |
| `effective_date` | Operational effectiveness |
| `withdrawn_reason` | Withdrawal note |
| `source_path` / locators | Current and historical paths |
| `content_hash` | Extracted-text fingerprint |
| `relationship_type` | SUPERSEDES / DERIVATIVE / DUPLICATE_OF / … |

Historical rows are never hard-deleted by normal replacement.

---

## 14. Chroma mapping

### 14.1 Principles

- Business identity is generated **outside** Chroma.
- Chroma stores vectors + compact retrieval metadata.
- SQLite registry is authoritative for identity/lifecycle.

### 14.2 Mapping rule

| Layer | Key |
|-------|-----|
| Registry chunk PK | `chunk_id` |
| Business chunk identity | `chunk_id` |
| Chroma record ID | Prefer **equal to `chunk_id`** for all **new** writes under `stable-id-v1` |
| Legacy production vectors | Keep existing UUID4 as `chroma_embedding_id`; map via registry |

### 14.3 Should Chroma `id` equal `chunk_id`?

**Yes, for new governed writes**, because:

- eliminates join ambiguity
- IDs are already Chroma-safe (ASCII, no slashes)
- LangChain accepts explicit `ids=`

**Migration impact:** existing ~124.5k UUID4 IDs are **not** equal to future `chunk_id`s. Until an approved ID-rewrite migration, registry must store:

```text
chunk_id  ↔  chroma_embedding_id (legacy UUID4)
```

Rewriting Chroma IDs in place is a separate gated operation (`MIGRATION_ID_MAPPING` → optional later rewrite); **not** required to begin registry backfill.

---

## 15. SQLite mapping

### 15.1 Logical map

```text
subject (documents / subject_id)
   ↓
document revision (document_versions / document_id=docrev:sha)
   ↓
source_files (locators + source_hash)
   ↓
chunks (NEW table — required for Phase 2+)
   ↓
chunk_vector_map (NEW) → chroma_embedding_id
```

### 15.2 Minimal new tables (contract requirement; implement in Phase 2+)

**`chunks`**
- PK `chunk_id`
- FK `document_id`
- `chunk_ordinal`, `page`, `chunking_fingerprint`, `content_hash`
- `identity_scheme_version`
- timestamps / run ids

**`chunk_vector_map`**
- PK (`chunk_id`, `physical_collection_name`)
- `chroma_embedding_id` UNIQUE per collection
- `mapping_status` ∈ {`legacy_uuid`, `native_chunk_id`, `pending`}
- `identity_scheme_version`

**`document_locators`** (optional if `source_files` already covers)
- multiple paths per `source_hash` / `document_id`

### 15.3 Production registry path

`<LIBRARY_ROOT>/.rag_state/metadata_registry/metadata_registry_v1.sqlite3`
(per location policy; not inside `.rag_db`)

---

## 16. Authority boundaries

| Authority | Owns |
|-----------|------|
| **SQLite metadata registry** | subject identity, document identity, revision lineage, lifecycle state, ingest/build metadata, expected chunk inventory, migration/reconciliation state |
| **Chroma** | vector storage, embeddings, retrievable chunk payload, vector-search execution |
| **Hermes / `ce_rag_query`** | governed scoped retrieval interface — **not** identity lifecycle storage |
| **Source files** | original technical content bytes |

**Adjustment to prior F-01 embedding-identity design:** embedding compatibility may still use Chroma collection manifest + per-chunk embedding fields for *runtime embed safety*. That does **not** make Chroma authoritative for subject/document/chunk business identity.

**Current gap:** production ingest does not yet honor registry authority (registry absent). Phase 2 begins isolated implementation without flipping production authority until gated.

---

## 17. Collision handling

| Conflict | Policy |
|----------|--------|
| Same `document_id`, conflicting immutable metadata (e.g. disagreeing recorded `source_hash`) | **STOP / REVIEW** — impossible under rule unless data corruption |
| Same `subject_id` assigned to clearly different manuals | **STOP / REVIEW** — split subjects; never silent merge |
| Same `chunk_id`, different text/`content_hash` | **STOP / REVIEW** — treat as scheme bug or fingerprint mismatch; do not overwrite |
| Two candidate subjects indistinguishable automatically | Leave `subj:pending:*` or dual proposals; **human review** |
| Byte-identical file claims two active subjects | **STOP / REVIEW** |

Required posture: **fail closed**. No silent overwrite.

---

## 18. Backward compatibility

Production ~124,570 embeddings; tracker 1,736 digests.

| Question | Answer |
|----------|--------|
| Add stable IDs without rebuilding embeddings? | **Yes** — registry + mapping layer |
| Deterministically map existing chunks to new IDs? | **document_id**: yes from file bytes if path resolvable. **chunk_id**: yes by replaying chunking offline; binding to legacy UUID requires an ordered reconciliation pass (not guessable from UUID alone) |
| Available metadata today | `source`, `page`, `collection`; partial authority/OCR fields; tracker `paths`/`chunk_ids`/`extraction` |
| Old Chroma IDs immutable? | Treat as immutable handles unless gated rewrite |
| Metadata backfill without vectors? | **Yes** (Chroma metadata update / registry only) |
| Must Chroma IDs be replaced? | **Not required** for Phase 2–3 if mapping table exists |
| Registry mapping preserve old vector IDs? | **Yes — required** |

---

## 19. Migration strategy

### Modes

| Mode | When | Actions | Embeddings |
|------|------|---------|------------|
| **MIGRATION_METADATA_ONLY** | Can compute `document_id` / registry rows; chunk↔UUID map built without re-embed | Write registry; optional Chroma metadata copy fields | Preserved |
| **MIGRATION_ID_MAPPING** | Need durable `chunk_id`↔UUID map | Populate `chunk_vector_map`; optionally set Chroma metadata `chunk_id` without changing Chroma id | Preserved |
| **SELECTIVE_REINDEX** | Extract/chunk fingerprint mismatch for a subset; or corrupt/empty extraction | Re-embed affected `document_id`s only | Partial rebuild |
| **FULL_REINDEX_REQUIRED** | Embed model/dimension change; systemic chunk contract break; explicit approval | Rebuild collection | Full rebuild |

**Default for introducing `stable-id-v1`:**
`MIGRATION_METADATA_ONLY` + `MIGRATION_ID_MAPPING`.
No production migration in Phase 1.

---

## 20. Preliminary re-index decision matrix

| Case | Decision |
|------|----------|
| Registry IDs map to existing Chroma UUIDs | Preserve embeddings; metadata/registry backfill only |
| Chunk boundaries / fingerprint change | New `chunk_id`s; selective or full rebuild depending on scope; do not reuse old chunk_ids |
| Embedding model identity unknown / mixed | Flag provenance UNKNOWN / `legacy_unclassified`; **do not** auto-rebuild; follow F-01 gates |
| Extracted text differs from current deterministic extractor | Compare `content_hash`; evaluate SELECTIVE_REINDEX |
| Path rename only | No re-index; locator update + metadata rewrite |
| Byte-identical copy added | No re-index; add locator |
| New bytes at path | New `document_id`; re-embed that document; preserve old lineage row |
| OCR derivative added | New `document_id`; embed; link DERIVATIVE — no destroy of original |

Formal re-index gate remains a later phase.

---

## 21. Examples

### Example 1 — Same PDF, same path, re-indexed twice

- `source_hash` unchanged ⇒ **same `document_id`**
- `subject_id` unchanged
- If chunking fingerprint unchanged ⇒ **same `chunk_id`s**
- If legacy UUID path without explicit ids ⇒ **new chroma UUIDs** (current behavior) — under Spec, Phase 2+ must pass `ids=chunk_id` to keep stable
- Classification: SAME_PHYSICAL_CONTENT
- Re-index impact: avoid unless forced; if forced with explicit ids, upsert-safe

### Example 2 — Same PDF bytes, different path

- **same `document_id`**, **same `subject_id`**
- second `relative_path` locator
- **same `chunk_id`s** / shared vectors
- Classification: DUPLICATE_COPY
- Re-index impact: none

### Example 3 — Same manual moved/renamed

- **same `document_id` / `subject_id` / `chunk_id`s**
- locator updated; Chroma `source` metadata updated
- Classification: SAME_PHYSICAL_CONTENT (locator change)
- Re-index impact: none

### Example 4 — Original PDF + OCR derivative

- different bytes ⇒ **different `document_id`s**
- shared `subject_id` after review; relationship DERIVATIVE
- different `chunk_id` sets
- Classification: DERIVATIVE
- Re-index impact: embed OCR as new document only

### Example 5 — Rev.A vs Rev.B same maker manual

- different bytes ⇒ different `document_id`
- **same `subject_id`**
- lineage SUPERSEDES / REPLACED as approved
- Classification: NEW_REVISION
- Re-index impact: embed Rev.B; keep Rev.A vectors until lifecycle says otherwise

### Example 6 — Unrelated manuals, same filename

- different bytes/paths ⇒ different `document_id` **and** different `subject_id`
- Classification: DIFFERENT_DOCUMENT
- Re-index impact: independent

### Example 7 — Same chunk text twice in one document

- same `document_id`
- distinct ordinals ⇒ **distinct `chunk_id`s**
- Classification: n/a (intra-doc)
- Re-index impact: none special

### Example 8 — Same chunk text in different documents

- different `document_id` ⇒ different `chunk_id`s even if text/`content_hash` match
- Classification: n/a
- Re-index impact: none special

### Example 9 — Chunking config changed

- `chunking_fingerprint` changes ⇒ **all new `chunk_id`s**
- `document_id` unchanged
- Classification: contract change
- Re-index impact: SELECTIVE or FULL per matrix; do not reuse old chunk_ids

### Example 10 — Extractor/normalization version changed

- If extractor_version in fingerprint changes ⇒ new `chunk_id`s
- `content_hash` likely changes even when bytes unchanged
- `document_id` (byte-based) unchanged
- Re-index impact: evaluate SELECTIVE_REINDEX; compare hashes before rebuild

---

## 22. Acceptance criteria (Spec freeze)

This specification is accepted when:

1. `subject_id`, `document_id`, `chunk_id`, and `identity_scheme_version` rules are unambiguous.
2. Path is defined as locator-only.
3. Hash purposes are separated.
4. Duplicate/revision/collision policies are fail-closed.
5. Chroma and SQLite mapping rules are explicit.
6. Backward compatibility modes are defined without requiring immediate full re-index.
7. Examples cover the mandatory ten scenarios.
8. Synonym map preserves compatibility with registry schema v1 lineage work.

---

## 23. Open questions

Only non-blocking items remain (see Audit U-1…U-4): historical extractor version labeling, applicability policy for multi-path copies, near-duplicate auto-link thresholds, and scaffold merge sequencing.
**No material ambiguity remains in the identity generation rules themselves.**

---

## Appendix A — Cross-phase invariants

1. No silent fallback to legacy SQLite retrieval.
2. Audit phases do not mutate production.
3. No production re-index before explicit approval gate.
4. Stable identity is reproducible across independent reruns.
5. Storage path is not the sole business identity.
6. Chroma-generated UUIDs are not primary lifecycle identity.
7. Historical lineage must never be destroyed by replacement.
8. Reconciliation must be read-only before repair tooling exists.
9. Unknown or conflicting identity must stop for review.
10. Identity algorithm changes require `identity_scheme_version` change.

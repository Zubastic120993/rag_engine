# STABLE IDENTITY AUDIT V1

**Phase:** Phase 1 (P0) — Stable Document / Chunk Identity
**Date:** 2026-08-11
**Repository HEAD:** `84a178065a1f23ddf312f48fbe7775fd5e5f4711` (`main`)
**Mode:** READ-ONLY audit (no production mutation, no re-index, no implementation code changes)
**Companion artifacts:**
- `docs/STABLE_IDENTITY_SPEC_V1.md` (frozen contract)
- `docs/STABLE_IDENTITY_PHASE2_PLAN.md` (bounded implementation plan)

---

## 1. Executive findings

1. **Production has no governed stable identity contract in live code on `main`.**
   Business identity today is effectively: file-byte SHA-256 (tracker key) + LangChain-generated UUID4 Chroma IDs + path-derived metadata (`source` / `page` / `collection`).

2. **SQLite metadata registry is design + branch scaffold only.**
   Schema/docs exist under `docs/`. Python scaffold exists on branches such as `integration/rag-reliability-registry-v1` (`rag_engine/metadata_registry/*`) but is **not merged to `main`**. Production path `.rag_state/` does **not** exist.

3. **Chroma record IDs are random UUID4 strings**, assigned by LangChain when `ids` is omitted from `add_documents`. They are **not** reproducible across re-index.

4. **Source path is a locator used as de-facto identity for retrieval/citation**, not a durable business identity. Byte-identical copies share one tracker digest and one set of Chroma IDs (65 multi-path tracker entries observed).

5. **Prior design docs partially define IDs** (`docs/RAG_STABLE_IDENTIFIER_SPECIFICATION_V1.md`) using `document_id` (logical) + `document_version_id` (revision). Phase 1 freezes renamed terminology (`subject_id` / `document_id`) with an explicit synonym map so Phase 5 lineage remains possible.

---

## 2. Repository / production baseline

| Item | Value |
|------|-------|
| Branch | `main` |
| HEAD | `84a1780` — *fix: harden final confidence gate against cross-document topical borrowing* |
| Working tree tracked mods | none (only unrelated untracked audit/backup artifacts) |
| Production Chroma root | `/Users/vladymyrzub/CE_Library/.rag_db` |
| Physical Chroma collection | `langchain` |
| Live embedding count | **124,570** (was ~108,685 in earlier audits; growth confirmed) |
| Tracker digests | **1,736** |
| Tracker-referenced chunk IDs | **124,569** (all UUID-shaped) |
| Chroma-only ID (not in tracker) | **1** (`8d8300f9-…`, probe residue) |
| Registry DB | **NOT PRESENT** (`/Users/vladymyrzub/CE_Library/.rag_state` missing) |
| Index fingerprint | `mxbai-embed-large`, chunk 800/100, `nfkc` |

Production file mtimes after this audit (unchanged by audit reads):
- `chroma.sqlite3` — 2026-08-10 11:05:53
- `embedded.json` — 2026-08-10 11:05:53

---

## 3. PART A — Current identity model (evidence table)

| Identity field | Location | Generation rule | Persistence | Current use | Stable across re-index? | Production populated? | Notes |
|----------------|----------|-----------------|-------------|-------------|-------------------------|-----------------------|-------|
| Tracker digest / `source_hash` (unnamed) | `rag_engine/ingest.py` `_file_sha256` L69–74; tracker key L358–311 | SHA-256 of **raw file bytes** | `.rag_db/embedded.json` keys | Dedup / skip / rename attach | Yes (content-stable) | Yes (1736 digests) | Authoritative for current ingest skip logic; **not** named `source_hash` in tracker JSON |
| `paths[]` | tracker entry (`ingest.py` L299–311) | Relative path under library root | `embedded.json` | Locator(s) for a digest; multi-path = byte-identical copies | No (path moves) | Yes | Mutable locator; 65 digests have ≥2 paths |
| `chunk_ids[]` | tracker entry | Copied from Chroma add result | `embedded.json` | Join tracker↔Chroma; rename repair; replace delete | **No** (new UUID4s on re-embed) | Yes (most entries); **74** entries have empty `chunk_ids` | Opaque vector IDs, not business IDs |
| Chroma `embedding_id` | Chroma `embeddings.embedding_id` | LangChain `uuid.uuid4()` when `ids is None` (`langchain_chroma` `add_texts`) | Chroma | Vector primary key | **No** | Yes (124570) | **Not** business identity |
| `source` (metadata) | `ingest.py` L163–167; `authority.enrich_metadata` L191–205 | Library-relative path; OCR suffix stripped to canonical | Chroma metadata | Retrieval filter + citation | Path-dependent | Yes (124569) | Locator used as soft identity |
| `raw_source` | `enrich_metadata` | Original path when OCR-canonicalized | Chroma metadata | Preserve OCR path | Path-dependent | Partial (4505) | Present when OCR rewrite applied |
| `page` | loader + ingest | PDF page / MD=`1` | Chroma metadata (int) | Citation | Extraction-dependent | Yes | Multiple chunks share page |
| `collection` | `collection_from_relpath` | Path-derived scope | Chroma + tracker | Scope routing (**not** physical collection) | Path-dependent | Yes | Logical scope only; physical collection=`langchain` |
| Authority enrichment fields | `authority.py` | Path heuristics | Chroma metadata | Ranking | Path-dependent | Partial (~11k–15k newer chunks) | `authority_rank`, `document_type`, etc. |
| `machine_transcribed` | `enrich_metadata` | `_ocr.pdf` suffix | Chroma bool | Authority demotion | Path-dependent | Partial (4505 true / 11379 false) | Older chunks lack field |
| Index fingerprint | `fingerprint.py` | Config snapshot JSON | `.rag_db/index_fingerprint.json` | Drift detection | N/A (config) | Yes | Not per-document identity |
| `subject_id` | — | — | — | — | — | **No** | **NOT IMPLEMENTED** on `main` |
| Governed `document_id` / `document_version_id` | docs + branch `identifiers.py` | UUID / uuid5 | Registry (not live) | Design only | Design intent: yes | **No** live | Scaffold not on `main` |
| Governed `chunk_id` | docs only | Speculative deterministic | — | Design only | Design intent: yes | **No** | Branch identifiers lack `chunk_id` helper |
| `content_hash` / text hash | docs; registry schema optional | Normalized text | Registry design | Design only | — | **No** in production | **NOT IMPLEMENTED** in live ingest |
| `intake_hash_index.json` | `.rag_db` sidecar | File sha256 + size/mtime | Sidecar JSON | Intake tooling | Content-stable | Yes | **Not used by** `rag_engine/ingest.py` |
| Probe id | doctor persistence probe | Temporary | May leave residue | Probe only | N/A | 1 orphan embedding | Not business identity |

### Evidence map (code)

| Claim | Evidence |
|-------|----------|
| Ingest does not pass Chroma IDs | `rag_engine/ingest.py` `_embed_chunks` L173–179: `db.add_documents(chunk)` — no `ids=` |
| UUID4 generation | `langchain_chroma.vectorstores.Chroma.add_texts`: `if ids is None: ids = [str(uuid.uuid4()) for _ in texts]` |
| Tracker key = file SHA-256 | `ingest.py` L69–74, L358, L299 |
| Rename preserves chunk IDs | `ingest.py` `_attach_path` L229–238 |
| Re-embed deletes old IDs | `ingest.py` `_ingest_new_hash` L289–292 |
| OCR path canonicalization | `authority.py` `canonical_source_path` L32–42 |
| Registry not in main package | No `rag_engine/metadata_registry/` on `main`; present on `integration/rag-reliability-registry-v1` |

---

## 4. PART B — SQLite metadata registry findings

### Location (approved design)

Per `docs/RAG_METADATA_REGISTRY_LOCATION_POLICY_V1.md`:
- Approved DB: `<LIBRARY_ROOT>/.rag_state/metadata_registry/metadata_registry_v1.sqlite3`
- Must **not** live inside `.rag_db` or the git repo

**Production status:** `.rag_state` **does not exist**. Registry is **scaffold/design only**.

### Schema (design + branch)

Authoritative design doc: `docs/RAG_METADATA_REGISTRY_SCHEMA_V1.md`
Branch implementation: `rag_engine/metadata_registry/schema.py` (`CURRENT_SCHEMA_VERSION=1`)

Tables (14):
1. `registry_schema_version`
2. `ingestion_runs`
3. `human_reviews`
4. `authorities`
5. `controlled_vocabulary_values`
6. `source_files`
7. `vessels`
8. `equipment_entities`
9. `documents` — PK `document_id` (**logical** in registry naming)
10. `document_versions` — PK `document_version_id` (**revision** in registry naming)
11. `document_vessel_applicability`
12. `document_equipment_applicability`
13. `relationships`
14. `metadata_assertions`

Key constraints (design):
- `document_versions`: UNIQUE `(document_id, source_hash)`
- `source_files`: UNIQUE `(source_hash, relative_path)`
- `vessels`: UNIQUE `vessel_imo`
- Soft-delete / supersede preferred over hard delete

### Actual relationship (registry design)

```
documents (logical lineage)          ← Phase-1 synonym: subject_id
    ↓ 1:N
document_versions (immutable rev)    ← Phase-1 synonym: document_id
    ↓ N:1
source_files (physical observation)  ← path + source_hash
    ↓ (future)
chunk inventory / vector map         ← NOT in schema v1 tables today
    ↓
Chroma embeddings (vector store)
```

**Gap:** Schema v1 has **no chunk table** and **no chroma_id mapping table**. Phase 2 must add minimal mapping tables (see Spec §15 / Phase2 plan).

### Integration with production ingest

| Question | Answer |
|----------|--------|
| Does `main` ingest write registry? | **No** — `ingest.py` has zero registry references |
| Population CLI on main? | **No** |
| Branch population? | Dry-run planning only; production creation deferred |
| Status | **Scaffold / design only** |

---

## 5. PART C — Current ingest flow

```text
CLI: rag-engine sync / python -m rag_engine.ingest
  └─ run_ingest() → ingest_lock → _run_ingest_locked()
       │
       ├─ load tracker embedded.json
       ├─ open Chroma(persist_dir=.rag_db, OllamaEmbeddings)
       │
       ├─ _iter_docs()  [discover PDF + wiki MD under library_root]
       │     identity: relative path only
       │
       ├─ for each file:
       │     ├─ is_valid_pdf? (magic %PDF) — skip INVALID_PDF
       │     ├─ digest = _file_sha256(bytes)          [content identity]
       │     │
       │     ├─ if digest in tracker and not force:
       │     │     └─ _attach_path()                 [DEDUPE/RENAME/SKIP]
       │     │           may update Chroma metadata source/collection
       │     │           preserves existing chunk_ids
       │     │
       │     └─ else _ingest_new_hash():
       │           ├─ _embed_chunks()
       │           │     ├─ PyPDFLoader / TextLoader
       │           │     ├─ metadata: source, page, collection
       │           │     ├─ enrich_metadata() (authority fields)
       │           │     ├─ normalize_text NFKC + length filter
       │           │     ├─ RecursiveCharacterTextSplitter(size, overlap)
       │           │     └─ db.add_documents() → UUID4 ids
       │           ├─ optionally delete old sole-owned chunk_ids
       │           └─ write tracker[digest] = {paths, chunk_ids, …}
       │
       └─ write_fingerprint() if embeddings changed
```

### Persistence side effects (current)

| Store | Written by ingest? |
|-------|--------------------|
| Chroma vectors + metadata | Yes |
| `embedded.json` | Yes |
| `index_fingerprint.json` | Yes (conditional) |
| SQLite metadata registry | **No** |
| `ask_events.jsonl` | **No** (ask path only) |

---

## 6. PART D — Current Chroma ID behavior

| Question | Answer | Evidence |
|----------|--------|----------|
| What is passed as Chroma `ids`? | **Nothing** — library generates UUID4 | `ingest.py` L177; langchain_chroma `add_texts` |
| ID class | **random UUID** | Production: 124569/124569 tracker IDs match UUID regex |
| Same unchanged doc re-indexed? | **New IDs** (old deleted if sole owner) | `_ingest_new_hash` force/replace path |
| Stable across restart? | Yes for existing rows | IDs persist in Chroma |
| Stable across DB rebuild? | **No** | New UUID4s |
| Stable across path/filename change? | **IDs preserved** on rename attach; metadata `source` updated | `_attach_path` |
| Stable across chunking/extraction change? | **No** — re-embed yields new IDs | force / new hash path |
| Treated as business identity? | **No** — opaque handles only | tracker `chunk_ids` are join keys |
| Registry join today? | **Impossible** — no registry | — |
| Duplicate source/page identity? | Many chunks share `(source,page)` by design (multi-chunk pages). Embedding IDs are unique. Multi-path digests share one ID set across paths. | Census above |

---

## 7. PART E — Path normalization (current)

| Concern | Current behavior | Evidence |
|---------|------------------|----------|
| Absolute vs relative | Stored as library-relative with `/` | `ingest.py` L124 |
| CE_Library root stripping | `path.relative_to(library_root())` | same |
| Case | Preserves filesystem case; some comparisons `.lower()` | skip/authority |
| Unicode path NFC/NFKC | **Not normalized for paths** | UNKNOWN / NOT IMPLEMENTED |
| Slash normalization | `\` → `/` in several helpers | `authority._norm`, ingest |
| Symlinks | **Not specially handled** | NOT IMPLEMENTED |
| `.` / `..` | `reconcile_path._validate_rel_path` rejects `..` | reconcile only |
| `/private/tmp` vs `/tmp` | **Not handled** | NOT IMPLEMENTED |
| Rename/move | Tracker attach + Chroma metadata update | `_attach_path`, `reconcile_path` |
| OCR suffix | `_ocr.pdf` → `.pdf` for `source`; `raw_source` kept | `canonical_source_path` |
| Duplicate filenames | Allowed; distinguished by full relative path | 60 basename collisions in tracker |

**Does source_path act as identity today?**
De facto yes for retrieval/citation and scope, but ingest content identity is SHA-256.
**Recommendation (frozen in Spec):** source_path = **C. mutable locator only** (may be an *input* to provisional subject matching, never sole business identity).

---

## 8. PART F — Hashing inventory

| Hash / fingerprint | Algorithm | Inputs | Includes path? | Use case |
|--------------------|-----------|--------|----------------|----------|
| File content hash | SHA-256 | Raw file bytes | No | Tracker key; dedupe; reconcile expected SHA |
| JSON digest (`reconcile_path`) | SHA-256 | Canonical JSON of ids/metas/embeddings | N/A | Audit digests for reconcile receipts |
| Index fingerprint | JSON (not hash) | embed_model, chunk_size/overlap, normalization (+ llm informational) | No | Config drift detection |
| Text normalization | NFKC (not hash) | Chunk/query text | No | Retrieval matching |
| Branch `source_hash` validators | SHA-256 hex | Declared | No | Registry scaffold |
| Branch `docver` / `src` / `eq` / `rel` | UUID5 over token | Various | Path for `src` only | Scaffold IDs (not live) |
| Normalized text / chunk content hash | — | — | — | **NOT IMPLEMENTED** live |
| Build/chunking fingerprint (per-doc) | — | — | — | Index-global only today |

---

## 9. PART G — Duplicate semantics (current vs intended)

| Case | Current behavior | Intended classification (Spec) |
|------|------------------|--------------------------------|
| Same bytes, same path | SKIP | SAME_PHYSICAL_CONTENT |
| Same bytes, different filename | DEDUPE — shared digest + chunk_ids; one embedding set | DUPLICATE_COPY (same `document_id`) |
| Same bytes, different directory | Same as above (career vs vessel copies observed) | DUPLICATE_COPY — may still need distinct **applicability** rows later |
| Same technical doc, different binary scans | NEW digest → separate embeddings | UNKNOWN / HUMAN_REVIEW_REQUIRED (possible SAME_DOCUMENT / DERIVATIVE) |
| Original PDF vs OCR derivative | Different bytes → separate digest; OCR path canonicalized for authority | DERIVATIVE (linked via subject; distinct `document_id`) |
| Same revision, metadata-only differences | Same bytes → same digest | SAME_PHYSICAL_CONTENT |
| Different revisions, similar filename | Different digests if bytes differ | NEW_REVISION (shared `subject_id`) |
| Different docs, same filename | Separate digests/paths | DIFFERENT_DOCUMENT |
| Career + vessel byte-identical copy | Multi-path tracker entry | DUPLICATE_COPY of one `document_id`; locator multiplicity |

---

## 10. PART H — Revision / lineage readiness

Registry schema already anticipates:
- `supersedes_document_version_id` / `superseded_by_document_version_id`
- `status`, dates, approval/verification
- relationships table

**Missing for Phase 1 contract completeness (addressed in Spec, not implemented):**
- explicit `subject_id` naming
- chunk inventory + chroma mapping
- lifecycle enum alignment (ACTIVE / SUPERSEDED / ARCHIVED / REPLACED / WITHDRAWN / DUPLICATE)

Model choice (frozen in Spec): **`subject_id` + `document_id`** is sufficient; no separate `revision_id`.
`document_id` **is** the immutable revision object.

---

## 11. PART I–L — Gaps driving the frozen Spec

| Gap | Impact |
|-----|--------|
| No governed `subject_id` | Cannot express lineage across revisions |
| No governed immutable `document_id` separate from path | Path moves and copies confuse identity |
| Chroma IDs random | Re-index destroys joins unless mapping layer exists |
| No chunk table / vector map in registry schema v1 | Cannot inventory expected chunks |
| Partial metadata enrichment | Older chunks lack authority/OCR fields |
| Tracker≠Chroma slight drift | 1 probe orphan; 74 tracker rows without chunk_ids |
| Prior ID naming conflict | Old docs use `document_id`=logical; Phase 1 renames — synonym map required |

---

## 12. Migration constraints (from production observations)

1. ~124.5k vectors must not be rebuilt solely to introduce IDs.
2. Existing UUID4 Chroma IDs must remain valid vector handles until an approved ID-replacement migration.
3. Deterministic backfill of new `document_id` is possible from file bytes where the source file still exists at a tracker path.
4. Deterministic backfill of `chunk_id` requires replaying extraction+chunking with a frozen fingerprint; ordinal alignment to old UUID4s is **not** recoverable from Chroma alone without storing order — mapping must be created during a controlled reconciliation pass.
5. Multi-path digests must map to **one** `document_id` with multiple locators.
6. OCR derivatives must not silently merge with originals via path canonicalization alone.

---

## 13. Unresolved items (non-blocking for Spec freeze)

| ID | Item | Classification |
|----|------|----------------|
| U-1 | Exact historical extractor/PyPDF version for original corpus | UNKNOWN — fingerprint lacks extractor version |
| U-2 | Whether all multi-path copies should share vessel applicability automatically | HUMAN_REVIEW_REQUIRED (policy later) |
| U-3 | Near-duplicate non-byte-identical revisions auto-link thresholds | Deferred to lineage phase |
| U-4 | Whether branch scaffold merges before or with Phase 2 ID helpers | Process decision; Spec compatible either way |

These do **not** leave subject/document/chunk ID **rules** ambiguous; they affect later automation quality.

---

## 14. Cross-phase invariants (recorded)

1. No silent fallback to legacy SQLite retrieval.
2. Audit phases do not mutate production.
3. No production re-index before explicit approval gate.
4. Stable identity is reproducible across independent reruns.
5. Storage path is not the sole business identity.
6. Chroma UUID4s are not primary lifecycle identity.
7. Historical lineage must never be destroyed by replacement.
8. Reconciliation must be read-only before repair tooling exists.
9. Unknown/conflicting identity stops for review (no silent merge).
10. Identity algorithm changes require `identity_scheme_version` change.

---

## 15. Audit verdict input

Current-state identity behavior is **documented with evidence**.
Gaps are **sufficiently characterized** to freeze Phase 1 Spec V1.
Production was **not mutated** by this audit.

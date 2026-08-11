# Historical Chunking Fingerprint Recovery

**Status:** Evidence recovery complete (no production mutation; no Phase 5)
**Date:** 2026-08-11
**Baseline HEAD:** `bee1330250d149ca075f1cb77f0a8ac4b020064d` (`main`)
**Authority:** `docs/STABLE_IDENTITY_SPEC_V1.md` §7.1 (frozen)

---

## 1. Question answered

Can the existing **124,569** legacy Chroma UUID vectors receive trustworthy deterministic `stable-id-v1` `chunk_id` mappings **without** rebuilding embeddings?

**Answer from evidence:** **No — not yet.**

| Decision | Value |
|----------|-------|
| Fingerprint verdict | `HISTORICAL_FINGERPRINT_PARTIALLY_RECONSTRUCTABLE` |
| Legacy mapping readiness | `LEGACY_MAPPING_NOT_READY` |
| Recommended policy | `PRESERVE_LEGACY_UUID_MAPPING` |
| Phase 5 (this recovery task) | Not implemented; see §17 for architecture-gate separation |

---

## 2. Exact Spec §7.1 fingerprint requirements

Canonical contract keys (sorted JSON → SHA-256):

| Field | Required by Spec §7.1? | Historical value needed? |
|-------|------------------------|--------------------------|
| `identity_scheme_version` | Yes | Yes (scheme label for `stable-id-v1` mapping) |
| `chunk_size` | Yes | Yes |
| `chunk_overlap` | Yes | Yes |
| `separators` | Yes | Yes |
| `normalization` | Yes | Yes |
| `min_chunk_chars` | Yes | Yes |
| `max_chunk_chars` | Yes | Yes |
| `extractor` | Yes | Yes |
| `extractor_version` | Yes | Yes (`<version_or_UNKNOWN>` is a Spec placeholder, not a recovered fact) |

`chunk_id` additionally requires **proven ordinal** (0-based index in the post-filter chunk list).

**Not substituted:** current Phase-2 `default_chunking_contract()` values are **not** treated as historical proof.

---

## 3. Evidence inventory table

| field | required by Spec? | historical value needed? | historical evidence found? | source of evidence | confidence |
|-------|-------------------|--------------------------|----------------------------|--------------------|------------|
| `identity_scheme_version` | Yes | Yes (for new mapping scheme) | No historical record (scheme post-dates index) | Spec freeze only | UNKNOWN as historical artifact; would be applied as `stable-id-v1` only under new scheme |
| `chunk_size` | Yes | Yes | Yes — `800` in production-era ingest/config + `index_fingerprint.json` | `rag/config.py` / `scopes.yaml` / `index_fingerprint.json` / git since `3c12f6c` | STRONG |
| `chunk_overlap` | Yes | Yes | Yes — `100` same sources | same | STRONG |
| `separators` | Yes | Yes | Yes — `["\n\n","\n","."," "]` hardcoded in production-era `_clean_chunks` | `rag/ingest.py`, `rag_engine/ingest.py` since scoped RAG | STRONG |
| `normalization` | Yes | Yes | Partial — NFKC in code after `73ee9b0`; earlier code used ASCII strip; empirical replay of “pre-commit” docs matched **NFKC**, not ASCII | git chronology + golden replay | CONTRADICTORY (chronology vs replay) / STRONG that stored text matches NFKC for tested docs |
| `min_chunk_chars` | Yes | Yes | Yes — filter `50 < len(text)` ⇒ 51 | ingest `_clean_chunks` since early scoped path | STRONG |
| `max_chunk_chars` | Yes | Yes | Yes — filter `len(text) < 3000` ⇒ 2999 | same | STRONG |
| `extractor` | Yes | Yes | Partial — `PyPDFLoader` / `TextLoader` in ingest | ingest history | STRONG for class identity; not a frozen Spec name string |
| `extractor_version` | Yes | Yes | No durable historical label recorded per ingest | absent from tracker / index fingerprint | UNKNOWN |
| splitter class | (affects contract via separators/size) | Yes | `RecursiveCharacterTextSplitter` | ingest history | STRONG |
| `keep_separator` | Not a Spec §7.1 key | Behaviorally relevant | Not set in ingest; library default `True` today | current package only | WEAK as historical proof |
| page≠ordinal | Companion rule | N/A | Proven multi-chunk pages exist | Chroma `page` metadata + reproduction | PROVEN |

---

## 4. Production snapshot (read-only)

Paths under library root `/Users/vladymyrzub/CE_Library`:

| Artifact | Path | Before | After |
|----------|------|--------|-------|
| `chroma.sqlite3` | `.rag_db/chroma.sqlite3` | size `616194048`, mtime `2026-08-10T11:05:53+0200`, sha256 `c03dafe2b220db9d848fcfdf16ffec0f1e12e9ff6fb86e9bd2b6f8922437b7d5` | unchanged |
| `embedded.json` | `.rag_db/embedded.json` | size `6237744`, mtime `2026-08-10T11:05:53+0200`, sha256 `f711555df17d79e43af05c9f8a4c0f5d31582343dcd762bec1f285cc39cb6631` | unchanged |
| `index_fingerprint.json` | `.rag_db/index_fingerprint.json` | size `141`, mtime `2026-08-10T11:05:54+0200`, sha256 `47bc5ef03f6e230b680d88233f1141462e0792a3ff5c17bd7adbb7dbc8811e61` | unchanged |
| `.rag_state` | `.rag_state` | absent | absent |
| production registry | `.rag_state/metadata_registry/metadata_registry_v1.sqlite3` | absent | absent |

Tracker facts used:

- 1,736 digests
- 124,569 chunk UUIDs
- `ingested_at` present on all entries
- `extraction` absent on 1,673 entries; `ok` on 63 (2026-08-08 / 2026-08-10)

---

## 5. `index_fingerprint.json` assessment

Exact contents:

```json
{
  "embed_model": "mxbai-embed-large",
  "llm_model": "qwen2.5:3b",
  "chunk_size": 800,
  "chunk_overlap": 100,
  "normalization": "nfkc"
}
```

| Field | Evidentiary class |
|-------|-------------------|
| `embed_model` | SUPPORTING_EVIDENCE_ONLY (embedding identity, not Spec §7.1 chunking_fingerprint) |
| `llm_model` | CURRENT_STATE_ONLY / informational |
| `chunk_size` | SUPPORTING_EVIDENCE_ONLY |
| `chunk_overlap` | SUPPORTING_EVIDENCE_ONLY |
| `normalization` | SUPPORTING_EVIDENCE_ONLY |

**Not** Spec §7.1 `chunking_fingerprint`: missing `identity_scheme_version`, `separators`, `min_chunk_chars`, `max_chunk_chars`, `extractor`, `extractor_version`.

Written first on successful ingest after commit `d77082c` (2026-07-21 15:18 +0200). Bulk of day-1 indexing predates that write; the file is a **latest live snapshot**, not a per-document historical contract log.

Aligned with `rag_engine/reconciliation/fingerprint_evidence.py`: index fingerprint alone ⇒ UNKNOWN for stable `chunk_id`.

---

## 6. Git / history findings

### Pre-production scripts (2025) — not production tracker era

| Commit | Settings |
|--------|----------|
| `8692d28` `rag_app.py` | size 800, overlap **120**, separators `["\n\n","\n"," ",""]` |
| `e7d6bbf` `build_rag.py` | size **600**, overlap **80**, separators `["\n\n","\n","."," "]` |
| `6bf6c2b` `build_rag.py` | conditional 2000/150 or 600/80 |

These demonstrate **multiple historical algorithms existed**, but production `embedded.json` timestamps begin **2026-07-21**.

### Production-era ingest (tracker window)

| Commit (local) | Relevance |
|----------------|-----------|
| `3c12f6c` 10:34 | Scoped RAG; ASCII strip `re.sub(r"[^ -~\n]", "")`; size 800 / overlap 100; separators with `"."` |
| `73ee9b0` 11:06 | NFKC `normalize_text`; hash tracker |
| `08672f4` 13:21 | `rag_engine` package + dependency pins (`pypdf==6.0.0`, etc.) |
| `d77082c` 15:18 | `index_fingerprint.json` writer |
| `2b8ad7e` 2026-07-27 | `extraction` field |
| `8214374` 2026-08-01 | F-03 chunking fingerprint **on a feature branch**; not the frozen Spec §7.1 helper used here |
| `85928b9` | `enrich_metadata` (metadata only; not chunk text) |

Production tracker day histogram:

| Day (UTC) | Docs | Chunks |
|-----------|------|--------|
| 2026-07-21 | 1540 | 105079 |
| 2026-07-23..26 | 133 | 3606 |
| 2026-08-08 | 54 | 4505 |
| 2026-08-10 | 9 | 11379 |

---

## 7. Extractor evidence

| Question | Result |
|----------|--------|
| Implementation | `PyPDFLoader` for PDF; `TextLoader` for wiki markdown |
| Exact version known? | **No** as a recorded historical field |
| Pin available? | `pypdf==6.0.0` + langchain pins from `08672f4` onward; **pre-pin environment unknown** |
| Classification | **implementation known but version uncertain** / **UNKNOWN** for Spec `extractor_version` |

Do **not** call extractor “known” merely because current code resembles it.

---

## 8. Chunking algorithm evidence

| Aspect | Evidence | Confidence |
|--------|----------|------------|
| Splitter | `RecursiveCharacterTextSplitter` | STRONG |
| `chunk_size` / `chunk_overlap` | 800 / 100 in production-era code + fingerprint file | STRONG |
| Separators | `["\n\n","\n","."," "]` | STRONG |
| Normalization timing | Normalize **after** split; filter on normalized length | STRONG |
| Filter | `50 < len(text) < 3000` | STRONG |
| Page handling | One loader page → metadata `page`; splitter may emit **multiple chunks per page** | PROVEN |
| `keep_separator` / length function | Not explicitly set historically | WEAK |

---

## 9. Cohort analysis

**Result: multiple ambiguous cohorts**

Signals:

1. **Timestamp window before `73ee9b0`:** 145 docs / 7,080 chunks with `ingested_at < 2026-07-21T09:06:13+00:00`.
2. **Git chronology** implies those could have used ASCII-strip normalization.
3. **Golden replay** of documents in that window matched **NFKC** stored Chroma text, **not** ASCII-strip (e.g. 11/11 and 12/12 under NFKC; partial under ASCII).
4. **Dependency pin boundary** at `08672f4` may imply extractor environment change without a recorded cohort key.
5. Later Aug 8/10 rows add `extraction` but do not record chunking fingerprint.

Therefore production cannot be safely partitioned into exact fingerprint cohorts with deterministic assignment. A single global fingerprint is also not proven.

---

## 10. Candidate contracts (evidence-supported sketches only)

Fingerprints computed **only** via `rag_engine.stable_identity.chunking_fingerprint` (Phase 2).

### Candidate A — NFKC production-era sketch

```json
{
  "identity_scheme_version": "stable-id-v1",
  "chunk_size": 800,
  "chunk_overlap": 100,
  "separators": ["\n\n", "\n", ".", " "],
  "normalization": "nfkc",
  "min_chunk_chars": 51,
  "max_chunk_chars": 2999,
  "extractor": "langchain_community.document_loaders.PyPDFLoader|TextLoader",
  "extractor_version": "UNKNOWN"
}
```

- Fingerprint: `f340ad78b85f1771e0ff7bfd7d048065bcb910a0e6971888a166e5bb51b48849`
- Proven / strong fields: size, overlap, separators, min/max, normalization **for tested docs**
- Inferred / unknown: `extractor` string labeling, `extractor_version`, scheme version as historical artifact
- Cohort: hypothesized majority / possibly entire tested set — **not certified for all 124,569**
- **Not a proven historical fingerprint** (unknown required fields remain)

### Candidate B — ASCII early-window sketch (chronology-only)

Same as A but `normalization` set to a descriptive ASCII-strip label.

- Fingerprint: `1282f9deb004d3364903e036e6e032ced9886dfccf505126c06b7c6449f068f3`
- **Rejected as proven cohort contract:** replay contradicts ASCII for sampled “early” docs

### Candidate C — Phase-2 `default_chunking_contract()` (comparison only)

- Fingerprint: `4fd91e460209531da53e3ce09913c00638b8bbc83e1af17c1b5d96c185c8090b`
- Explicitly **not** historical evidence (`extractor: "unknown"` defaults)

---

## 11. Reproduction results

Method: read source file → in-memory `PyPDFLoader` + historical-equivalent splitter/filter → compare tracker chunk counts / Chroma `chroma:document` text. **No embed, no Chroma write.**

### Count agreement (mixed sizes; NFKC 800/100)

| Document (short) | Tracker | Reproduced (NFKC 800/100) | Notes |
|------------------|---------|---------------------------|-------|
| CE_Travel_02_01.pdf | 1 | 1 | exact |
| Tacho alarms 2.pdf | 1 | 1 | exact |
| 46 CFR § 56.50-103… | 6 | 6 | exact; multi-chunk pages |
| 33 CFR § 164.25… | 6 | 6 | exact |
| Day2_E.pdf | 41 | 41 | exact |
| 5510-0284-00.pdf | 41 | 41 | exact |
| A14-789798-3.3… | 153 | 153 | exact |
| X.MLC-SDE001.pdf | 503 | 503 | exact |
| News.pdf (early ts) | 1 | 1 | exact (ASCII also 1) |
| U.N.400… (early ts) | 33 | 33 | exact (ASCII also 33) |
| WEICON… (early ts) | 62 | 62 | exact (ASCII also 62) |

Alternate configs often diverge (600/80; overlap 120 with different separators).

### Ordinal + content agreement (NFKC)

| Set | Result |
|-----|--------|
| 8 post-NFKC docs × 5 chunks | 40/40 text match at tracker index |
| 3 multi-batch docs (120–127 chunks) | 120/120, 124/124, 127/127 |
| 3 early-timestamp docs | ASCII primary failed; **NFKC matched 11/11, 11/11, 12/12** |

Page metadata agrees with reproduced page for small samples; **multiple chunks share pages** ⇒ page is not ordinal.

---

## 12. Ordinal recovery

### Code path

`_embed_chunks`:

1. Build `valid` in splitter order after normalize+filter.
2. Batch `db.add_documents(chunk)` with `ids.extend(added)` when return is a list.
3. Persist `tracker[digest]["chunk_ids"] = ids`.

LangChain `Chroma.add_texts` generates UUID list in input order and `return ids` in that order.

### Fallback hazard

If `add_documents` does not return a list:

```python
got = db.get(where={"source": source_rel})
ids = list(got.get("ids") or [])
```

Order of `get()` is **not** proven to equal ordinal. No production log proves this path was unused.

### Classification

**`ORDINAL_PROVEN_BY_COHORT`** for documents where:

- tracker `chunk_ids` length matches reproduced post-filter count, and
- Chroma document text equals reproduced text at each index (empirically shown for NFKC samples including multi-batch),

**not** a blanket `ORDINAL_PROVEN` for all 124,569 without a full read-only census and fallback exclusion.

Page must never be used as ordinal.

---

## 13. Legacy UUID ↔ ordinal relationship

When the happy-path return list is used:

```text
tracker.chunk_ids[i]  ↔  ordinal i  ↔  valid[i]
```

This is **necessary but not sufficient** for stable `chunk_id` (fingerprint still required).

---

## 14. Final fingerprint verdict

### `HISTORICAL_FINGERPRINT_PARTIALLY_RECONSTRUCTABLE`

Justification:

- Substantial identity-affecting fields are STRONG (size, overlap, separators, min/max, splitter, and NFKC behavior on tested docs).
- At least one required Spec field remains unproven: **`extractor_version`** (and durable `extractor` contract labeling).
- Cohort assignment is **ambiguous** (chronology vs replay).
- `index_fingerprint.json` is insufficient for Spec §7.1.
- Candidates still contain inferred/unknown required fields ⇒ not proven fingerprints.

Not `PROVEN` / `PROVEN_BY_COHORT`: incomplete required fields and non-deterministic cohort labeling.

Not `NOT_RECOVERABLE`: recovery is partially possible and further read-only census could strengthen evidence — but must not invent defaults.

---

## 15. Legacy mapping readiness

### `LEGACY_MAPPING_NOT_READY`

Mapping requires **both**:

1. proven historical `chunking_fingerprint` — **missing**
2. proven ordinal per legacy UUID — **partially evidenced only**

Proven stable chunk mappings manufacturable today: **0**
Unresolved legacy UUIDs: **124,569**

---

## 16. Re-index policy recommendation

| Policy | Meaning |
|--------|---------|
| A Preserve legacy UUID identity | Keep UUID as `chroma_embedding_id`; registry maps only proven facts; stable `chunk_id` begins at future controlled ingest |
| B Controlled stable-ID re-index | Rebuild under frozen contract; new deterministic IDs; retain audit trail |

**Recommend: `PRESERVE_LEGACY_UUID_MAPPING` (Policy A).**

Why:

- Phase 4 showed tracker↔Chroma join is healthy (0 missing UUIDs, 0 duplicate owners, 0 hash/chunk-count mismatches in that gate).
- Lack of stable chunk mapping alone does **not** justify rebuild risk.
- Policy A is reversible and does not destroy embeddings.
- Policy B remains available later if a governed migration explicitly accepts rebuild cost.

---

## 17. Phase 5 readiness (architecture separation)

This fingerprint-recovery task **does not implement** Phase 5.

Independent architecture review separates two issues:

| Issue | Status |
|-------|--------|
| Legacy deterministic `chunk_id` mapping for existing UUID vectors | **Blocked** (`LEGACY_MAPPING_NOT_READY`) |
| Document revision lifecycle at `subject_id` → `document_id` | **Not blocked by legacy chunk_id** under Spec §§12–14 |

Frozen Spec facts:

- Lifecycle state lives on registry rows, not inside IDs (§12).
- Chroma UUID4s are **not** primary lifecycle identity (Appendix A / §14).
- Legacy vectors keep UUID as `chroma_embedding_id`; mapping is optional later (§14.2–14.3).
- `document_id = docrev:<source_hash>` is available from tracker digests without stable chunk IDs (§6, §18).

**Architecture-gate verdict (verification release):** `PHASE5_READY_WITH_BOUNDARY`

Phase 5 may proceed at subject/document revision level **provided** legacy UUID vector identity and deterministic stable legacy `chunk_id` remain explicitly out of scope. Retrieval filtering of lifecycle state can join `document_id` / `source_hash` → tracker `chunk_ids` (UUIDs) without rewriting Chroma IDs.

This recovery document still does **not** start Phase 5 implementation.

---

## 18. Safety confirmations

| Action | Done? |
|--------|-------|
| Re-index | NO |
| Backfill | NO |
| UUID rewrite | NO |
| Registry population | NO |
| Production mutation | NO |
| Commit / push | NO |

---

## 19. Tests / code

No new reusable fingerprint-recovery logic was required beyond existing Phase-4 `fingerprint_evidence.py`.
**No new test file created** (audit/evidence-only task).

---

## 20. Remaining issues

### Blocking (legacy stable `chunk_id` mapping only)

- Unproven Spec `extractor_version` / durable extractor contract labeling
- Ambiguous cohort certification for full 124,569 set
- No full ordinal census excluding `get()` fallback path

### Not blocking Phase 5 (subject/document lifecycle)

- Unresolved legacy stable `chunk_id` under the preserve-UUID policy
- Future retrieval lifecycle filters can use `document_id`/`source_hash` → tracker UUID join

### Non-blocking

- Early 2025 script settings irrelevant to tracker window but useful as caution
- 74 zero-chunk tracker rows (no UUIDs to map)
- 187 metadata mismatches from Phase 4 (path/collection) do not by themselves decide fingerprint recovery

---

## 21. Independent verification addendum (release gate)

**Date:** 2026-08-11
**Verifier role:** independent fingerprint-recovery release + Phase 5 architecture gate
**Baseline HEAD verified:** `bee1330250d149ca075f1cb77f0a8ac4b020064d`

Confirmed independently:

1. Spec §7.1 contract fields as listed in §2 — correct; ordinal required for `chunk_id` but outside fingerprint JSON.
2. Production `index_fingerprint.json` contents and insufficiency — correct.
3. Historical chunker (RecursiveCharacterTextSplitter, 800/100, separators, `50 < len < 3000`) — correct from git (`3c12f6c`, `73ee9b0`, `08672f4`).
4. Normalization chronology vs replay contradiction — reconfirmed (early-timestamp sample: ASCII ordinal text 2/15, NFKC 15/15).
5. Extractor class strong / `extractor_version` unproven — correct; blocks exact Spec fingerprint.
6. Cohort = multiple ambiguous — justified.
7. Bounded reproduction: NFKC 800/100 exact counts + ordinal text match on independent 20-chunk samples; 600/80 diverges.
8. Ordinal = `ORDINAL_PROVEN_BY_COHORT` (happy-path + samples; `db.get` fallback unproven globally).
9. Fingerprint = `HISTORICAL_FINGERPRINT_PARTIALLY_RECONSTRUCTABLE`.
10. Mapping = `LEGACY_MAPPING_NOT_READY` (0 proven / 124569 unresolved).
11. Policy = `PRESERVE_LEGACY_UUID_MAPPING`.
12. Phase 5 = `PHASE5_READY_WITH_BOUNDARY` (§17 correction separates lifecycle from legacy chunk_id).

Production artifacts unchanged during verification (read-only).

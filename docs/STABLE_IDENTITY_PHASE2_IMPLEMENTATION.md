# STABLE IDENTITY PHASE 2 IMPLEMENTATION

**Status:** Implemented (not committed; not wired into production ingest)
**Scheme:** `stable-id-v1`
**Date:** 2026-08-11
**Depends on:** `docs/STABLE_IDENTITY_SPEC_V1.md`, `docs/STABLE_IDENTITY_PHASE2_PLAN.md`

---

## 1. Implemented API

Package: `rag_engine.stable_identity` (stdlib only; no Chroma / LangChain).

| Symbol | Purpose |
|--------|---------|
| `IDENTITY_SCHEME_VERSION` | `"stable-id-v1"` |
| `sha256_bytes` / `sha256_utf8` | Low-level digests |
| `source_hash_from_bytes` / `source_hash_from_file` | Raw-byte SHA-256 (64 lowercase hex) |
| `document_id_from_bytes` / `document_id_from_file` / `document_id_from_source_hash` | `docrev:<source_hash>` |
| `content_hash` | SHA-256 of normalized extracted text |
| `canonicalize_chunking_contract` | Deterministic compact JSON |
| `chunking_fingerprint` | SHA-256 of canonical contract JSON |
| `default_chunking_contract` | Spec §7.1-shaped defaults (no prod config read) |
| `subject_id_from_key` / `subject_id_from_uuid` / `subject_id_pending` | Subject forms |
| `chunk_id` / `chunk_id_preimage` | Deterministic chunk IDs |
| `validate_*` | Fail-closed validators |
| `normalize_relative_path` | Spec §9 locator NFC helpers |
| `verify_chunk_id_matches_preimage` / `reject_conflicting_chunk_reuse` | Collision fail-safes |
| `registry_tables.apply_stable_identity_tables` | Temp-DB additive DDL |

Phase 2 plan aliases (`make_document_id`, `source_hash_file`, …) are also exported.

---

## 2. Exact hash preimages

### source_hash

```text
source_hash = SHA256(raw_file_bytes).hexdigest()   # lowercase 64 hex, no prefix
```

### document_id

```text
document_id = "docrev:" + source_hash
```

### content_hash

```text
normalized = NFKC(text)
             then drop Unicode category "C" chars except \n and \t
             then str.strip()
content_hash = SHA256(UTF-8(normalized)).hexdigest()
```

Documented decision: matches existing ingest `rag_engine.text.normalize_text`
(Spec requires NFKC; control-char stripping + strip align with live RAG behavior).
No extra CRLF→LF conversion.

### chunking_fingerprint (Spec §7.1)

```text
canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False)
chunking_fingerprint = SHA256(UTF-8(canonical)).hexdigest()
```

Contract should include `identity_scheme_version` (see `default_chunking_contract`).
Floats and non-JSON-safe objects are rejected.

### chunk_id (Spec §7.1)

```text
token = identity_scheme_version + "|" + document_id + "|"
        + chunking_fingerprint + "|" + str(int(ordinal))
chunk_id = "chunk:" + SHA256(UTF-8(token)).hexdigest()[:32]
```

Ordinal: non-negative `int` only (bool/float rejected).

### subject_id

```text
subj:key:<kind>:<lowercase-ascii-slug>
subj:uuid:<lowercase-rfc4122-uuid>
subj:pending:<source_hash>
```

Kinds: `imo_doc`, `sms`, `sire`, `reg`, `maker_doc`, `manual_family`.
No automatic path-based subject inference.

---

## 3. Validation rules

| ID | Accept | Reject |
|----|--------|--------|
| source/content/fingerprint | 64 lowercase hex | uppercase, wrong length, non-hex, whitespace |
| document_id | `docrev:` + 64 hex | missing/wrong prefix, uppercase, truncations |
| chunk_id | `chunk:` + 32 hex | wrong length/prefix |
| subject_id | three Spec forms | unknown kind, bad UUID case, bad pending hash |
| ordinal | `int >= 0` | bool, float, negative |

Validators do **not** silently canonicalize malformed IDs.
`validate_subject_id` requires key bodies already match the lowercase ASCII slug
form (rejects uppercase / spaces / non-slug characters without rewriting them).

---

## 4. Chunking canonicalization value model

Allowed: `null`, `bool`, `int` (not bool), `str`, `list`/`tuple`, mapping with `str` keys.
Rejected: `float` (incl. NaN/Inf), arbitrary objects, non-str mapping keys.

---

## 5. Collision / invariant handling

- `verify_chunk_id_matches_preimage` regenerates ID from constituents; mismatch → `IdentityCollisionError`.
- `reject_conflicting_chunk_reuse` allows identical idempotent reuse; divergent stored constituents → fail hard.
- Temp SQLite `insert_chunk` / `insert_chunk_vector_map` use the same policy (no silent overwrite).

---

## 6. Registry additions

Additive DDL only under `rag_engine/stable_identity/registry_tables.py`:

- `chunks`
- `chunk_vector_map` (legacy UUID4 remain as `chroma_embedding_id`)

Full `rag_engine.metadata_registry` scaffold is **not on `main`**; these tables are
standalone for temporary DBs. **No production `.rag_state` is created.**

Terminology comments preserve Spec §4.1 synonym map.

---

## 7. Explicit non-goals (Phase 2)

- Live ingest wiring (`ids=chunk_id`)
- Production Chroma / `embedded.json` mutation
- Production registry creation
- Legacy UUID rewrite / re-index
- Automatic subject matching from paths
- Confidence-gate changes

---

## 8. Production ingest wiring

**NOT enabled.** `rag_engine/ingest.py` unchanged.

---

## 9. Tests

Focused:

```text
env -u PYTHONPATH -u PYTHONHOME PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  venv/bin/python -m pytest tests/test_stable_identity.py -q
→ 35 passed in 0.28s
```

Broader (isolated, non-live):

```text
env -u PYTHONPATH -u PYTHONHOME PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  venv/bin/python -m pytest tests -q --ignore=tests/test_chroma_persistence.py \
  -k "not live and not integration and not openai"
→ 249 passed, 5 deselected, 1 warning in 13.59s
```

`python -m compileall rag_engine/stable_identity` — OK
`git diff --check` — clean

---

## 10. Remaining for Phase 3

- Dry-run backfill planner (tracker + files → proposed registry rows)
- Gated production wiring of `chunk_id` into ingest
- Merge/align with `metadata_registry` scaffold when available
- Controlled `chunk_id` ↔ legacy UUID reconciliation pass

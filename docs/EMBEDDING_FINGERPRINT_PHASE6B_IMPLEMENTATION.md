# EMBEDDING FINGERPRINT PHASE 6B IMPLEMENTATION

**Status:** Phase 6B runtime enforcement (code + tests)
**Date:** 2026-08-11
**Frozen contract:** Phase 6A commit `872d8c5` — `docs/EMBEDDING_FINGERPRINT_CONTRACT_V1.md`
**fingerprint_schema_version:** `embedding-fp-v1`

Phase 6A contract modified: **NO**

---

## 1. Implementation scope

Turn the Phase 6A contract into executable, tested, fail-closed behavior:

- deterministic `efp` / `cfp` / `ifp` construction
- authoritative state load with conflict detection
- compatibility classification
- ingest append gate (before `embedded.json` digest skip)
- retrieval policy gate
- doctor diagnostics (non-mutating)
- registry schema v3 `index_fingerprints` table (test/new DBs only)
- comprehensive unit/integration tests on temporary state only

Out of scope (explicitly not done):

- production re-index / migration / certification
- production registry population
- automatic rebuild on mismatch
- lifecycle retrieval activation

---

## 2. Frozen Phase 6A contract reference

| Artifact | Role |
|----------|------|
| `docs/EMBEDDING_FINGERPRINT_CONTRACT_V1.md` | Normative contract |
| `docs/PHASE6A_EMBEDDING_FINGERPRINT_AUDIT.md` | Evidence package |
| `docs/embedding_fingerprint_contract_v1.json` | Machine-readable summary |

---

## 3. Architecture

```text
rag_engine/index_compatibility/
  constants.py      # schema names, states, field lists
  exceptions.py     # typed fail-closed errors
  specs.py          # typed contracts + canonical JSON + digests
  builders.py       # runtime contract construction from config
  chroma_inspect.py # read-only vector counts (SQLite mode=ro)
  state.py          # sidecar v1 + registry authority load/write
  compatibility.py  # evaluate_compatibility()
  policy.py         # ingest / retrieval / doctor enforcement
```

Legacy v0 module `rag_engine/fingerprint.py` remains for informational
`index_fingerprint.json` compare (doctor check `index_fingerprint`).
It is **not** `embedding-fp-v1` authority.

---

## 4. Fingerprint module(s)

| Digest | Preimage |
|--------|----------|
| `efp` | canonical JSON of embedding contract |
| `cfp` | canonical JSON of corpus/chunk contract (Spec §7.1 fields + `embedded_text_composition_version`) |
| `ifp` | canonical JSON of index contract (`efp`, `cfp`, store fields) |

---

## 5. Authoritative state

**Interim Phase 6B authority (when registry has no row):**

- `index_embedding_fingerprint_v1.json` under the persist dir

**Target authority (when present):**

- metadata registry table `index_fingerprints` (schema v3)

**Mirrors:**

- sidecar v1 may mirror registry; disagreement → `CONFLICT`
- Chroma collection metadata is never sufficient alone
- sidecar v0 (`index_fingerprint.json`) is never authority

Read/check paths **never** rewrite missing fingerprints.

---

## 6. Compatibility classification

| State | Meaning |
|-------|---------|
| `KNOWN_COMPATIBLE` | stored `ifp` == runtime `ifp` |
| `KNOWN_INCOMPATIBLE` | stored authority disagrees with runtime |
| `UNKNOWN_LEGACY` | vectors exist; no trustworthy `embedding-fp-v1` authority |
| `EMPTY_UNINITIALIZED` | zero vectors; no authority; may initialize |
| `CORRUPT` | malformed payload |
| `CONFLICT` | registry vs sidecar disagree |
| `UNSUPPORTED_SCHEMA` | unknown `fingerprint_schema_version` |
| `CONFIGURATION_ERROR` | runtime contract cannot be built |

---

## 7. Ingest enforcement

Order inside `_run_ingest_locked`:

1. Build/evaluate runtime fingerprint compatibility
2. If `EMPTY_UNINITIALIZED` → initialize v1 authority (empty only)
3. If unsafe → raise typed error (**before** digest skip)
4. Only then consult `embedded.json` skip / embed

Blocked for append: `UNKNOWN_LEGACY`, `KNOWN_INCOMPATIBLE`, `CORRUPT`,
`CONFLICT`, `UNSUPPORTED_SCHEMA`, `CONFIGURATION_ERROR`.

No automatic re-index, delete, or fingerprint repair.

---

## 8. Retrieval behavior

| State | Retrieval |
|-------|-----------|
| `KNOWN_COMPATIBLE` | allowed |
| `UNKNOWN_LEGACY` | allowed **degraded** (explicit diagnostics; never labeled compatible) |
| `EMPTY_UNINITIALIZED` | allowed (empty index) |
| `KNOWN_INCOMPATIBLE` / `CORRUPT` / `CONFLICT` / unsupported / config error | blocked |

Diagnostics keys under retrieval diagnostics:

- `index_fingerprint_state`
- `index_fingerprint_degraded`
- `index_fingerprint_reason`

---

## 9. Doctor behavior

New check: `embedding_fp_v1` via `doctor_fingerprint_report()`.

- Reports PASS / WARNING / FAIL with state name
- Does **not** create missing fingerprints
- Does **not** reindex or repair
- Legacy v0 check retained for continuity

---

## 10. Legacy handling

Non-empty collection without v1 authority ⇒ `UNKNOWN_LEGACY`.

Current runtime config alone never upgrades this to `KNOWN_COMPATIBLE`.

---

## 11. New-index initialization

Proven empty (`vector_count == 0`) + no authority ⇒ `EMPTY_UNINITIALIZED`.

Explicit ingest path may write sidecar v1 **before** first vector write.

Absence of sidecar alone never proves “new” if vectors exist.

---

## 12. Corruption / conflict

Malformed JSON/schema/digest mismatch ⇒ `CORRUPT`.

Registry vs sidecar digest disagreement ⇒ `CONFLICT` (no newest-wins, no mtime winner, no auto-repair).

---

## 13. No-auto-reindex rule

Mismatch / unknown legacy surfaces as errors. No control flow triggers
rebuild, vector deletion, or silent fingerprint rewrite.

---

## 14. Test coverage

`tests/test_index_compatibility.py` covers determinism, sensitivity,
insensitivity, same-dimension/different-model, skip-gate ordering,
legacy, empty init, mismatch/corrupt/conflict/unsupported, retrieval
policy, doctor non-mutation, atomicity failure, registry schema v3.

Prior suites remain required: stable identity, reconciliation, metadata
registry, document lifecycle.

---

## 15. Production safety

Phase 6B verification must not:

- reindex production
- write production fingerprint authority
- populate production registry
- mutate production Chroma / `embedded.json`

---

## 16. Known limitations

- Ollama model tags remain mutable aliases when `embedding_model_revision` is `null` (accepted residual risk per Phase 6A).
- Dimension probe from Chroma SQLite may be unavailable; dimension remains a runtime guardrail field, not sufficiency.
- Metadata-only tools (`backfill_collections`, `reconcile_path`) do not append embedding vectors; they are not substitute certification paths. Vector-append paths are gated in ingest.

---

## 17. Activation boundary

Code enforcement is active when ingest/query/doctor run.

Production index remains **uncertified** until a future controlled migration
phase designs treatment of `UNKNOWN_LEGACY`.

---

## 18. Rollback / containment

Revert the Phase 6B commit to remove gates. Production vector data is
untouched by this phase; rollback does not require DB restore for fingerprint
state (no production v1 authority was written).

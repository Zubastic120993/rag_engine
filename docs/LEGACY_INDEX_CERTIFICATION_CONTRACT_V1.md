# LEGACY INDEX CERTIFICATION CONTRACT V1

**Status:** Phase 6C design + implementation contract (frozen for tooling)
**Date:** 2026-08-11
**Depends on:** Phase 6A `embedding-fp-v1`, Phase 6B commit `66aa07a`
**Production authority:** NOT activated by this document alone

---

## 1. Purpose

Define the fail-closed, operator-gated rules for deciding whether an
`UNKNOWN_LEGACY` vector index may be certified as `KNOWN_COMPATIBLE`
without rebuilding vectors — and when rebuild is required instead.

---

## 2. Scope

In scope:

- Evidence hierarchy and certification threshold
- Target binding / TOCTOU
- Dry-run vs apply semantics
- Audit / conflict / idempotency
- Rebuild-required decision boundary

Out of scope:

- Automatic production certification
- Production reindex / vector mutation
- Lifecycle activation

---

## 3. Definitions

| Term | Meaning |
|------|---------|
| Historical contract | Embedding + corpus + index parameters that produced existing vectors |
| Runtime contract | Current process configuration fingerprint |
| Certification | Operator acceptance that historical contract is proven for a bound target |
| Level A / B / C | Evidence strength tiers (below) |

---

## 4. UNKNOWN_LEGACY semantics

Vectors exist but no trustworthy `embedding-fp-v1` authority proves the
historical contract. Current runtime config alone never upgrades this state.

Phase 6B policies remain:

- ingest append: **BLOCKED**
- retrieval: **allowed degraded**

---

## 5. Evidence hierarchy

### Level A — Direct immutable proof

Examples: creation-time v1 fingerprint; immutable model digest + exact
config snapshot; signed index-build manifest bound to collection;
transactional registry record from index creation.

### Level B — Corroborated historical proof

Multiple independent sources jointly establishing all compatibility-critical
fields (git historical indexer + config snapshot + immutable model digest +
creation window + proof no later incompatible append).

### Level C — Circumstantial (insufficient alone)

Same model alias, same dimension, v0 advisory fingerprint, current defaults,
developer recollection, “retrieval seems fine”.

---

## 6. Certification threshold

Certification to `KNOWN_COMPATIBLE` requires:

1. Target is `UNKNOWN_LEGACY` (or already exactly matching certification), and
2. Every compatibility-critical embedding/corpus/index field covered by
   Level **A or B** evidence (not Level C alone), and
3. No evidence value conflicts, and
4. Mixed-history assessment is not `PROBABLE_MIXING` / `PROVEN_MIXING`, and
5. If assessment is `POSSIBLE_MIXING`, Level-A `mixed_history_exclusion` fact
   is present, and
6. Historical fingerprint matches runtime fingerprint when certifying
   “compatible with current runtime” (otherwise `NOT_CERTIFIABLE` for runtime
   use; rebuild/change runtime separately), and
7. Target binding still matches (TOCTOU).

---

## 7. Insufficient evidence rules

Level C alone ⇒ `INSUFFICIENT_EVIDENCE`.

Missing any required field at Level A/B ⇒ `INSUFFICIENT_EVIDENCE`.

Runtime-config assumption without Level A ⇒ `INSUFFICIENT_EVIDENCE`.

---

## 8. Mixed-history rules

| Assessment | Certification |
|------------|---------------|
| `NO_EVIDENCE_OF_MIXING` | Allowed if other thresholds met |
| `POSSIBLE_MIXING` | Requires Level-A exclusion; else `MIXED_HISTORY_SUSPECTED` |
| `PROBABLE_MIXING` / `PROVEN_MIXING` | `MIXED_HISTORY_SUSPECTED` / rebuild |

Lack of mixing evidence ≠ proof of no mixing.

---

## 9. Target binding

Certification binds to:

- persist path
- physical collection name
- collection id (when known)
- vector count
- chroma.sqlite3 SHA-256
- structural fingerprint over the above

Stale approvals against a changed index must refuse.

---

## 10. TOCTOU protection

Before apply writes:

1. re-measure binding
2. verify vector count / hashes
3. verify compatibility still expected state
4. verify no conflicting authority
5. only then write

Any change ⇒ `ABORT` / `CertificationTargetChangedError`.

---

## 11. Dry-run semantics

Default mode. Reports proposed writes. **Zero** mutations to fingerprint,
registry, Chroma, tracker, or mtimes of index state.

---

## 12. Apply semantics

Requires explicit `--apply` (or `apply=True`). Writes:

1. authoritative `embedding-fp-v1` envelope (historical contract, not assumed runtime)
2. certification audit record (`index_embedding_certification_v1.json`)
3. optional registry row when explicitly requested on a non-production test DB

Does **not** rewrite vectors or `embedded.json`.

---

## 13. Operator reason

Non-empty reason required for apply. Persisted in audit.

---

## 14. Audit requirements

Audit records:

- previous_state / new_state
- fingerprints
- evidence_manifest_hash
- reason / actor / certified_at
- target binding

Certification timestamp = when proof was accepted, **not** vector creation time.

---

## 15. Conflict behavior

Existing different fingerprint authority or audit ⇒ `CONFLICT`. No overwrite.

---

## 16. Idempotency

Exact same fingerprint + matching evidence hash already present ⇒ no-op.

---

## 17. Revocation / containment principle

Do not silently delete audit history. Prefer explicit invalidation in a later
operations phase while retaining audit. Do not casually rewrite certification
records.

---

## 18. Rebuild-required decision

When evidence is insufficient or mixed history cannot be excluded:

`REBUILD_REQUIRED` / `MIXED_HISTORY_SUSPECTED` operational outcome.

Rebuild is a separate phase (parallel candidate index, validation, cutover).

---

## 19. Non-goals

- Certifying because retrieval works
- Certifying from dimension / alias / v0 alone
- Auto-certify from doctor/ingest/query/startup
- In-place destructive rebuild in this contract

---

## 20. Safety invariants

1. Dry-run never mutates.
2. Apply never invents historical contract from runtime alone.
3. Apply never rewrites vectors.
4. UNKNOWN_LEGACY ingest stays blocked until real certification applies.
5. Production apply requires a later explicit approval phase beyond tooling.

# HRM_014 confidence gating implementation report — 20260808T105831Z

## 1. Scope

Implemented SPEC_010 confidence gating architecture only.

Not implemented:
- Proposal 5
- re-indexing
- embeddings changes
- Metadata Registry
- Stable IDs
- floor recalibration
- full 48-question benchmark

## 2. Baseline and preconditions

### rag_engine
- Branch: `main`
- HEAD before implementation: `345455a384d1421859096e344d88ae037e543294`
- `origin/main`: matched local HEAD before implementation
- Tracked modifications before implementation: none
- Staged changes before implementation: none
- Pre-existing untracked artefacts: left untouched
- `RAG_RETRIEVAL_SCORE_MAX`: not set
- Configured floor before implementation: `0.38`

### orchestrator
- Branch: `master`
- HEAD: `0f91ffb7c6d516a8507333cb17b09aa20d93c2ba`
- `origin/master`: matched local HEAD
- Read-only reference only; no orchestrator code changes made in this task

## 3. Files changed

- `rag_engine/query.py`
- `tests/test_retrieval_authority.py`
- `tests/test_coverage_states.py`
- `tests/test_f18_retrieval_evidence.py`
- `audit_reports/HRM_014_CONFIDENCE_GATING_IMPLEMENTATION_REPORT_20260808T105831Z.md`

## 4. Old vs new pipeline

### Old live pipeline

```text
question
-> raw retrieval
-> scope filtering
-> hard score floor (0.38)
-> reranking
-> dedupe
-> answer / no_coverage
```

### New live pipeline

```text
question
-> raw retrieval
-> broad admissibility gate
-> scope filtering
-> reranking
-> dedupe
-> final authority-aware confidence gate
-> no_coverage OR context
-> generation
-> refusal / weak-evidence handling
-> answer
```

## 5. Stage 1 logic — broad admissibility

Implemented in `rag_engine/query.py`.

Purpose:
- reject only clearly unusable retrieval output.

Current rule:
- candidate must have a finite numeric distance;
- candidate must resolve to a non-empty source path after metadata enrichment.

Important:
- this stage does **not** use `0.38` as a reject threshold;
- candidates above `0.38` now survive to scope/rerank/dedupe.

## 6. Stage 2 logic — final authority-aware confidence gate

Implemented in `rag_engine/query.py` as `_apply_final_confidence_gate(...)`.

Signals used:
- best raw distance
- top candidate distance
- `canonical_authority_rank`
- `authority_rank`
- `document_type`
- `authority_family`
- exact-source support count
- authority-family support count
- candidate concentration / coherence
- `0.38` as strong-evidence indicator only

Current pass rule:
- top candidate must be authoritative:
  - `canonical_authority_rank <= 2`, or
  - `canonical_authority_rank <= 3` with manual-like document type
- and must also satisfy at least one support condition:
  - strong distance (`best_raw_distance <= 0.38`), or
  - coherent support from repeated source/family evidence, or
  - company-rank authority (`canonical_authority_rank <= 2`)

Fail-closed behavior:
- mixed non-authority / reference / training results fail;
- weak unsupported maker-manual singletons above `0.38` fail;
- final gate returns `final_confidence_failed` before generation.

## 7. Diagnostics added

### New gate outcomes surfaced
- `ok`
- `no_retrieval`
- `broad_admissibility_failed`
- `final_confidence_failed`
- `refusal_or_weak_evidence`
- existing error gates preserved for error paths

### New retrieval diagnostics surfaced
Added `retrieval_diagnostics` to `AskResult` JSON with:
- `raw_count`
- `post_admissibility_count`
- `post_scope_count`
- `post_rerank_count`
- `post_dedupe_count`
- `final_retained_count`
- `best_raw_distance`
- `score_floor`
- final-gate feature values such as top source/family/type/support

## 8. 0.38 semantics after implementation

`0.38` is no longer a hard pre-rerank reject threshold.

Current role:
- strong-evidence indicator;
- retained as diagnostic / telemetry value;
- used as one support signal inside the final confidence gate.

It is **not** the sole final decision criterion.

## 9. Focused safety checks implemented in tests

### GQ-004 equivalent positive control
- strong, authoritative hit remains able to return `ok`
- covered through existing `answer()` / contract tests with authoritative source preserved

### GQ-009 / GQ-015 / GQ-031 class protection
- candidates above `0.38` now survive retrieval into reranking
- tested by:
  - prefloor-preservation test for reranking candidates
  - supported parallel-manual final-gate pass test

### GQ-006 negative-control behavior
- weak or mixed non-authority evidence fails the final gate
- covered by mixed non-authority final-gate rejection test
- no plausible answer path is created from weak evidence alone

### Set B safety intent
- implementation does not broaden negative cases by using a larger global floor
- negative-style weak/mixed evidence is blocked by final confidence logic, not admitted by a raised threshold

## 10. Tests run

### Focused RED/GREEN cycle
- `./run_tests.sh tests/test_retrieval_authority.py tests/test_coverage_states.py -q`
- final result: `47 passed`

### Full suite
- `./run_tests.sh`
- final result: `133 passed`

### Compile check
- `./venv/bin/python -m compileall rag_engine tests`
- result: passed

### Diff whitespace check
- `git diff --check`
- result: passed

## 11. Governance compliance

Complies with task constraints:
- no Proposal 5 work
- no re-indexing
- no embeddings changes
- no Metadata Registry / Stable-ID work
- no full benchmark run
- no floor recalibration
- preserved authority/document-type/family metadata semantics
- preserved Proposal 2 / 3 / 4 ranking behavior before the new final gate

## 12. Limitations

1. Final confidence logic is intentionally conservative and heuristic-based; it is not yet benchmark-tuned.
2. This task did not run the 48-question gold benchmark, by instruction.
3. Some representative live outcomes remain to be verified only in the next governed measurement step.
4. Company-rank (`canonical_authority_rank <= 2`) evidence is allowed through final gate without repeated-source support; this preserves existing company-procedure paths but should be monitored in later benchmark verification.

## 13. Rollback

Code-only rollback.

To revert this implementation:
- restore previous `rag_engine/query.py`
- restore updated tests to prior expectations if rolling back behavior intentionally
- no DB rollback required
- no re-index rollback required

## 14. Unified diff summary

```text
rag_engine/query.py                  | 218 ++++++++++++++++++++++++++++++-----
tests/test_coverage_states.py        |  60 +++++++++-
tests/test_f18_retrieval_evidence.py |  18 +--
tests/test_retrieval_authority.py    | 141 +++++++++++++++++++++-
4 files changed, 390 insertions(+), 47 deletions(-)
```

## 15. Final status

Implementation complete locally and verified by tests.

Next governed step:
- commit approved implementation files and report only;
- push to `origin/main`;
- follow with benchmark/measurement task, not tuning in this task.

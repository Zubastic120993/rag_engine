# HRM_027 — Unified manual_library implementation

- Timestamp: 20260808T222654Z
- Task: HRM_027
- Mode: implementation + focused live validation
- Scope: implement SPEC_012 only
- Classification target: implementation committed even if focused gate fails

## 1. Baseline

### rag_engine
- Branch: `main`
- HEAD: `9848859d1a43271c0c76f67deda3e461b04d28ab`
- `origin/main`: `9848859d1a43271c0c76f67deda3e461b04d28ab`
- Match: yes
- Tracked modifications before implementation: none
- Staged changes before implementation: none

### orchestrator
- Branch: `master`
- HEAD: `35ac7739a6660cdeae9288135e19c44ac3dc4bab`
- `origin/master`: `35ac7739a6660cdeae9288135e19c44ac3dc4bab`
- Match: yes
- Tracked modifications before implementation: none
- Staged changes before implementation: none

### Preserved constraints
- No DB writes performed by implementation or tests
- No tracker writes
- No reindex
- No embedding changes
- Pre-existing untracked artefacts left untouched
- Normal `ask_events` query logging occurred during focused live gate and is recorded separately below

## 2. Files changed

Implementation/test files:
- `rag_engine/query.py`
- `rag_engine/cli.py`
- `tests/test_retrieval_authority.py`
- `tests/test_cli_ask_args.py`

Report file:
- `audit_reports/HRM_027_UNIFIED_MANUAL_LIBRARY_IMPLEMENTATION_20260808T222654Z.md`

## 3. Implementation summary

Implemented the accepted `TWO_QUERY_MERGE` design for the user-facing logical scope `manual_library` only.

Behavior now implemented:
1. if retrieval scope is `manual_library`, run two independent internal retrieval paths:
   - `maker-manuals`
   - `me-c`
2. each internal scope keeps its own existing retrieval controls
3. merge retained candidates only after those controls finish
4. apply deterministic cross-collection dedupe on `(canonical source, page)`
5. preserve direct behavior of `maker-manuals`
6. preserve direct behavior of `me-c`
7. expose union diagnostics:
   - `internal_scopes_queried`
   - `internal_scope_counts`
   - `merged_candidate_count`
   - `union_deduped_count`
   - `union_duplicate_drops`
   - `union_candidates` with:
     - `requested_scope`
     - `internal_collection`
     - `canonical_source`
     - `page`
8. CLI event logging now records `result.resolved_scope`, so user-facing `manual_library` remains visible in logs even when requested via alias

## 4. Old vs new manual_library behavior

### Before
- `manual_library` resolved effectively to `maker-manuals`
- `manual_library` could not see `me-c` candidates
- `M 1.3.pdf` was unreachable through `manual_library`

### After
- `manual_library` performs two controlled retrieval passes:
  - `maker-manuals`
  - `me-c`
- merged diagnostics explicitly show both internal paths
- direct `maker-manuals` and `me-c` scopes remain unchanged

## 5. Merge and dedupe logic

### Internal retrieval
For `manual_library`, the implementation calls the existing single-scope retrieval path twice, once per internal authoritative collection.

### Merge
- merged candidate pool = retained candidates from both internal scopes
- ranking remains existing candidate ranking semantics
- no new scoring weights introduced

### Dedupe
- identity: `(canonical source, page)`
- collection is intentionally not part of dedupe identity
- duplicate preference stays deterministic through the existing rank order, then stable order
- diagnostic counters record duplicate drops

## 6. Diagnostics added

For `manual_library` retrieval diagnostics now include:
- `internal_scopes_queried`
- `internal_scope_counts`
- `merged_candidate_count`
- `union_deduped_count`
- `union_duplicate_drops`
- `union_candidates`

These fields preserve existing diagnostics and add union visibility without changing direct internal-scope semantics.

## 7. Tests run

### Focused regression tests first
Command:
```bash
./run_tests.sh tests/test_retrieval_authority.py tests/test_cli_ask_args.py tests/test_hardening.py
```
Result:
- first run found one ordering assertion issue in new test and was corrected
- final focused regression result: pass

### Required repository checks
Commands:
```bash
./run_tests.sh
./venv/bin/python -m compileall rag_engine tests
git diff --check
```
Results:
- full suite: `139 passed`
- compileall: passed
- `git diff --check`: passed

## 8. Focused live gate

### ask_events accounting
- ask_events path: `/Users/vladymyrzub/CE_Library/.rag_db/ask_events.jsonl`
- before count: `386`
- after count: `393`
- delta: `7`

The last 7 appended records match the 7 focused live queries below.

### Focused live result table

| Check | Scope | Query summary | Top candidate collection | Top candidate source / page | Status | Gate | Authority correctness | Duplicate state | Result summary |
|---|---|---|---|---|---|---|---|---|---|
| FC-01 | `manual_library` | generic main-engine tightening-torque query | `maker-manuals` | `OPERATION MANUAL_6EY22(A)LWS(H.F.O.／MET)...pdf` / 43 | `ok` | `ok` | **FAIL** — expected `M 1.3.pdf`, got Yanmar | `union_duplicate_drops=0` | answer generated from Yanmar, not ME-C |
| FC-02 | `maker-manuals` | Yanmar maintenance interval query | `maker-manuals` | `OPERATION MANUAL_6EY22(A)LWS(H.F.O.／MET)...pdf` / 48 | `ok` | `ok` | PASS | n/a | returned `2 years / 8000 working hours` |
| FC-03 | `maker-manuals` | GQ-004 positive control | `maker-manuals` | `Yanmar_6EY22ALW_Spare_Parts_List...pdf` / 89 | `ok` | `ok` | PASS | n/a | returned `The dial gauge [A]` |
| FC-04 | `maker-manuals` | GQ-006 negative control | weak maker-manuals evidence only | `PH_Filter ... Manual.pdf` / 1 | `no_coverage` | `final_confidence_failed` | PASS | n/a | safely blocked; no unsafe duration answer |
| FC-05 | `me-c` | M 1.3 page-40 table fact (GQ-041) | `me-c` | `M 1.3.pdf` / 38 | `ok` | `ok` | PASS (retrieval path) / answer value drift | n/a | answer gave `2600 Nm`; top source remained `M 1.3.pdf` |
| FC-06 | `me-c` | M 1.3 page-100 warning heading (GQ-049) | `me-c` | `FITTING and ACC.pdf` / 387 | `ok` | `ok` | **FAIL** — expected `M 1.3.pdf` page 100 | n/a | answer `WARNING!`, wrong authority |
| FC-07 | `maker-manuals` | non-ME-C maker-manual query | `maker-manuals` | `OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf` / 92 | `ok` | `ok` | MIXED / not targeted regression | n/a | resolved to Yanmar SCR manual, not 6EY22 fault-diagnosis target |

### Detailed observations

#### FC-01 — manual_library generic query
- union diagnostics confirmed both internal scopes were queried
- internal counts:
  - `maker-manuals.retained_count = 5`
  - `me-c.retained_count = 5`
- merged top candidates still favored Yanmar pages
- hard criterion 1 failed: `manual_library` did **not** retrieve `M 1.3.pdf` into the resulting candidate set for this generic query

#### FC-04 — GQ-006 negative control
- returned `no_coverage`
- gate: `final_confidence_failed`
- no unsafe fabricated duration was produced
- hard criterion 5 passed

#### FC-05 — me-c direct page-40 fact
- direct `me-c` retrieval still reaches `M 1.3.pdf`
- top source remained correct
- answer text showed numeric drift (`2600 Nm` instead of expected `2623`), so retrieval path is preserved but answer fidelity remains imperfect

#### FC-06 — me-c direct page-100 fact
- direct `me-c` retrieval for this question did **not** keep `M 1.3.pdf` at top
- top candidate became `FITTING and ACC.pdf`
- this is a wrong-authority P0 outcome in focused live validation

## 9. Hard success criteria verdict

| Criterion | Verdict | Notes |
|---|---|---|
| 1. `manual_library` retrieves M 1.3 candidates | **FAIL** | FC-01 top/result set did not surface `M 1.3.pdf` |
| 2. direct `maker-manuals` behavior unchanged | PASS | FC-02 / FC-03 / FC-04 remained acceptable |
| 3. direct `me-c` behavior unchanged | **FAIL** | FC-06 wrong-authority top result |
| 4. GQ-004 remains correct | PASS | FC-03 |
| 5. GQ-006 remains safely blocked | PASS | FC-04 |
| 6. no new wrong-authority P0 result | **FAIL** | FC-06 |
| 7. no duplicate canonical source/page after merge | PASS | union duplicate drops stayed `0` in focused manual_library check |
| 8. diagnostics expose supplying collection | PASS | union diagnostics include internal collection provenance |

### Overall verdict
**FAILED GATE**

Per task instruction and SPEC_012 hard-stop rule:
- stop after this gate result
- do not tune
- do not change weights
- do not change exclusions
- do not expand scopes
- do not run full benchmark beyond the already required fixed checks
- do not implement Proposal 5

## 10. Limitations

1. `manual_library` union is implemented, but ranking still prefers strong non-ME-C maker-manual evidence for some generic manual questions.
2. Focused live validation shows the implementation alone is not sufficient to guarantee the accepted user-facing contract for generic M 1.3 access.
3. The failure is in focused live behavior, not in test execution or code stability.
4. No additional tuning was attempted after the failed gate.

## 11. Rollback

Rollback is limited and straightforward:
- remove `manual_library` union path in `rag_engine/query.py`
- restore prior single-scope `manual_library` behavior
- remove associated diagnostics/tests if reverting feature entirely

No DB rollback, tracker rollback, or reindex rollback is required.

## 12. Commit scope

Only the following should be committed for HRM_027:
- implementation files
- test files
- this HRM_027 report

Do not stage or modify pre-existing unrelated untracked artefacts.

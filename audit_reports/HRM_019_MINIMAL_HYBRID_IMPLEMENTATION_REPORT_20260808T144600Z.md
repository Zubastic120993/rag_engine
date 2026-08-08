# HRM_019 minimal hybrid implementation report — 20260808T144600Z

## 1. Scope

Implementation of **SPEC_011 only**.

Done:
- minimal hybrid candidate generation in `rag_engine/query.py`
- process-local in-memory lexical retrieval over existing stored chunk text
- deterministic vector + lexical merge
- additive provenance diagnostics
- focused unit tests
- frozen 7-question live gate

Not done:
- no Proposal 4 redesign
- no Proposal 5
- no reindex
- no embedding change
- no config change outside implemented code path
- no full benchmark
- no remediation after gate failure

## 2. Baseline

### rag_engine
- branch: `main`
- required baseline: `7fa0483ae9b60f45f03d4b2aa2c189ef1246da6b`
- local `HEAD` at implementation start matched `origin/main`

### orchestrator
- branch: `master`
- live accepted SPEC_011 baseline used during implementation review: `97ce95656df4f10c65d2ebfa2501811c8350b511`
- local `HEAD` matched `origin/master`

## 3. Files changed

### Modified
- `rag_engine/query.py`

### Added
- `tests/test_hybrid_retrieval.py`
- `audit_reports/HRM_019_MINIMAL_HYBRID_IMPLEMENTATION_REPORT_20260808T144600Z.md`

## 4. Implementation summary

Implemented a minimal hybrid candidate-generation layer before the existing retrieval control path:

```text
question
-> normalize_text(question)
-> vector candidates (existing Chroma similarity_search_with_score)
+
-> lexical candidates (new in-memory inverted token path)
-> deterministic merge
-> existing _apply_retrieval_controls(...)
-> existing _apply_final_confidence_gate(...)
-> answer / no_coverage
```

The downstream semantics were preserved:
- `authority_rank`
- `canonical_authority_rank`
- `document_type`
- `document_type_rank`
- `authority_family`
- `machine_transcribed`
- `raw_source`
- Proposal 3 scope behaviour
- Proposal 4 ranking behaviour
- SPEC_010 confidence behaviour

## 5. Lexical mechanism

Implemented:
- process-local cache built from existing stored chunk text via `db.get(include=["documents", "metadatas"])`
- no durable second index
- no external service
- no schema migration
- no reindex

Query-time signal handling implemented:
- exact technical-token detection:
  - mixed alphanumeric identifiers
  - section-like tokens such as `4.5`
  - thread/dimension forms such as `M42`
  - class abbreviations such as `LR`, `DNV-GL`, `CCS`, `CR`, `RINA`
- phrase capture from non-generic token windows
- heading approximation from first short lines of stored chunk text
- generic weak-term list:
  - `alarm`
  - `temperature`
  - `tank`
  - `bearing`
  - `torque`
  - `turbocharger`

Lexical scoring remained deterministic and bounded:
- exact technical token hit
- exact phrase hit
- heading match
- residual non-generic token overlap

## 6. Merge logic / provenance

Implemented duplicate identity:
- `chunk_id` when present
- fallback: canonical source + page + collection + normalized chunk-text digest

Implemented candidate provenance:
- `candidate_origin`: `vector` | `lexical` | `both`
- `vector_distance`
- `lexical_score`
- `lexical_exact_hits`
- `lexical_phrase_hits`
- `heading_match`

Deterministic pre-scope merge ordering implemented:
1. `both`
2. lexical with exact technical-token hits
3. vector-only
4. lexical without exact-token hits
5. lexical score
6. distance
7. source
8. page

This preserved the existing downstream ranking/gating path while exposing additive diagnostics.

## 7. Diagnostics added

Additive `retrieval_diagnostics` fields:
- `vector_raw_count`
- `lexical_raw_count`
- `merged_raw_count`
- `candidate_origins`

Additive `sources` / `retrieval_evidence` fields when available:
- `candidate_origin`
- `vector_distance`
- `lexical_score`
- `lexical_exact_hits`
- `lexical_phrase_hits`
- `heading_match`

## 8. Tests

### New tests added
`tests/test_hybrid_retrieval.py`

Covered:
1. exact technical token brings lexical-only candidate into merged pool
2. generic token alone cannot dominate
3. vector + lexical duplicate becomes origin=`both`
4. deterministic merge ordering
5. provenance diagnostics visible

### Existing tests preserved
Focused existing retrieval/coverage tests remained green.

### Required commands
- `./run_tests.sh` → **PASS** (`138 passed`)
- `./venv/bin/python -m compileall rag_engine tests` → **PASS**
- `git diff --check` → **PASS**

## 9. Frozen 7-question live gate

Scope executed only:
- `GQ-009`
- `GQ-015`
- `GQ-031`
- `GQ-041`
- `GQ-045`
- `GQ-006`
- `GQ-004`

### ask_events accounting
- before: `386`
- after: `386`
- delta: `0`

### 7-question result table

| ID | Final status | Gate | Expected source retained vector | Expected source retained hybrid | Final authority correct | Answer match | Result summary |
|---|---|---|---:|---:|---:|---:|---|
| GQ-009 | `no_coverage` | `final_confidence_failed` | Yes | No | No | No | regressed recall; expected source dropped from hybrid retained set |
| GQ-015 | `ok` | `ok` | Yes | Yes | No | No | wrong-authority P0 answer remains |
| GQ-031 | `ok` | `ok` | Yes | Yes | No | Yes | wording correct but wrong-authority P0 answer remains |
| GQ-041 | `ok` | `ok` | No | No | No | No | new wrong-authority P0 answer (`200 Nm`, wrong manual) |
| GQ-045 | `no_coverage` | `final_confidence_failed` | No | No | No | No | no recovery |
| GQ-006 | `ok` | `ok` | No | No | No | Yes | unsafe negative-control failure; should have remained blocked |
| GQ-004 | `no_coverage` | `refusal_or_weak_evidence` | Yes | No | No | No | positive control regressed |

### Focused observations
- Hybrid provenance is visible on all seven questions.
- Hybrid lexical candidates were present on all seven questions (`lexical_raw_count=100` in each case).
- Expected-source recall **worsened** on the frozen set:
  - vector retained expected source on `4/7`
  - hybrid retained expected source on `2/7`
  - delta = `-2`
- Hybrid introduced clear generic-phrase pollution in the live path, for example:
  - `GQ-009` lexical hits dominated by generic phrases such as `in the same`
  - `GQ-041` wrong lexical promotion into unrelated manuals despite correct `M42` evidence existing elsewhere

## 10. Hard success criteria verdict

Binding criteria from SPEC_011 / VER_037:

1. At least 3 previously failing cases improve live → **FAIL** (`0`)
2. At least 2 become fully correct authority selections → **FAIL** (`0`)
3. GQ-004 remains correct → **FAIL**
4. GQ-006 remains safely blocked → **FAIL**
5. No new wrong-authority P0 result → **FAIL** (`GQ-015`, `GQ-031`, `GQ-041`)
6. Candidate provenance is visible in diagnostics → **PASS**
7. Expected-source recall materially improves versus vector-only → **FAIL** (`2` vs `4`, delta `-2`)

### Frozen verdict
**The hard gate failed.**

Per the frozen rule set:
- stop hybrid refinement
- do not tune
- do not add new heuristics in this task
- do not implement Proposal 5
- do not run the full benchmark

## 11. Limitations

1. The minimal lexical path is additive but too permissive in live conditions.
2. Generic phrase overlap still enters lexical promotion strongly enough to displace expected retained sources.
3. No heading metadata exists; heading approximation from chunk text alone was insufficient.
4. Wrong-authority sibling-manual behaviour was not corrected.
5. Figure/table-heavy MAN cases were not recovered.

## 12. Rollback

Rollback remains simple:
- disable/remove lexical candidate generation path in `query.py`
- return to vector-only retrieval path

No data rollback is required because:
- no DB writes
- no schema migration
- no reindex
- no persistent sidecar index

## 13. Classification

`IMPLEMENTATION_COMMITTED_WITH_FAILED_GATE`

# HRM_038 — Clarification-First Technical Query Flow Implementation

## 1. Files modified
- `rag_engine/query.py`
- `rag_engine/cli.py`
- `tests/test_clarification_first_flow.py`

## 2. Implementation structure
Implemented the accepted SPEC_015 flow as a minimal extension around the existing `answer()` path.

Main additions:
- technical-query classifier for the accepted minimal class;
- sufficiency check for explicit maker/model/manual/family wording;
- clarification branch returning `status="clarification_required"` before retrieval;
- confirmation branch using explicit current-turn confirmation text;
- fresh constrained retrieval after sufficient confirmation;
- CLI exit mapping so clarification returns the non-answer/no-coverage exit path.

Not changed:
- retrieval ranking;
- `scopes.yaml`;
- embeddings;
- index/DB state;
- standalone `context_router.py`;
- live automatic context-router integration.

## 3. Sufficiency classifier
Implemented minimal accepted sufficiency only:
- explicit main-engine / MAN / `M 1.3` / `G50ME` -> `me-c`
- explicit Yanmar / `6EY22` / `Y22SCR` / SCR / auxiliary-engine wording -> `maker-manuals`
- explicit component plus verified equipment context may also be sufficient

Underspecified technical-query class implemented:
- torque
- clearance
- setpoint
- limit
- temperature
- pressure
- dimension
- interval
- quantity
- setting
- `procedure step`

Important narrowing applied during implementation:
- generic `procedure` / `step` wording was **not** treated as underspecified technical-value class because it regressed existing stable source/coverage behavior.
- This kept SPEC_015 minimal while preserving current stable direct-answer semantics.

## 4. Clarification behavior
For underspecified technical-value questions, retrieval is skipped and `answer()` returns:
- `status="clarification_required"`
- user-facing short prompt in `answer`
- `gate="clarification_required"`
- empty retrieval evidence / no sources

Prompt behavior:
- default: `Which equipment/component do you mean?`
- alarm/setpoint case: `Which equipment or alarm system do you mean?`
- ambiguous confirmation `Main engine` -> `Which main engine component do you mean?`

## 5. Confirmation behavior
Implemented explicit confirmation inputs via optional `confirmation_text=`.

Behavior:
- confirmation is treated as current-turn explicit evidence;
- stale/inferred context is not allowed to override it;
- insufficient confirmation returns `clarification_required` again;
- sufficient confirmation constrains scope and rewrites the live retrieval query as:
  - `<confirmation>. <original question>`

## 6. Fresh-retrieval proof
Fresh retrieval is encoded and exercised as follows:
- pre-confirmation vague query path returns clarification before any retrieval call;
- confirmation path performs exactly one new constrained retrieval call;
- retrieval diagnostics record:
  - `technical_state`
  - `confirmation_text` where applicable
  - `fresh_retrieval_required=true`
  - `preconfirmation_reuse_allowed=false`

Focused proof from tests:
- `test_vague_query_then_confirmation_runs_fresh_retrieval_without_preconfirmation_reuse`
- first call: zero retrieval calls
- second call with `MAN G50ME-C`: one retrieval call to `me-c`

## 7. Focused fixture results
Accepted fixture source:
- `/Users/vladymyrzub/Projects/ai-engineering-orchestrator/eval/clarification_first_fixtures_v1.json`

Result:
- `tests/test_clarification_first_flow.py::test_clarification_first_fixture_pack_matches_expected_behavior`
- **14 / 14 passed**

Supplementary focused checks in same file:
- incomplete confirmation -> second clarification
- fresh retrieval / no pre-confirmation reuse proof
- CLI clarification exit-code behavior

Focused file result:
- `17 passed in 0.84s`

## 8. Focused live results
Accepted 14 focused scenarios were executed through the fixture-driven live-path test file.

| Fixture | Scenario | Result |
|---|---|---|
| CF-001 | vague torque | `clarification_required` |
| CF-002 | vague alarm setpoint | `clarification_required` |
| CF-003 | vague clearance | `clarification_required` |
| CF-004 | vague temperature | `clarification_required` |
| CF-005 | explicit M 1.3 torque | direct retrieval -> `ok` (`me-c`) |
| CF-006 | explicit Yanmar query | direct retrieval -> `ok` (`maker-manuals`) |
| CF-007 | vague query -> confirm MAN | fresh constrained retrieval -> `ok` (`me-c`) |
| CF-008 | vague query -> confirm Yanmar | fresh constrained retrieval -> `ok` (`maker-manuals`) |
| CF-009 | incomplete confirmation | second clarification |
| CF-010 | stale context + explicit Yanmar wording | direct retrieval -> `ok` (`maker-manuals`) |
| CF-011 | constrained retrieval failure after confirmation | `no_coverage` |
| CF-012 | GQ-006-style negative safety | `clarification_required` first |
| CF-013 | vague pressure | `clarification_required` |
| CF-014 | explicit component + verified equipment context | direct retrieval -> `ok` (`me-c`) |

## 9. Hard-gate verdict
1. underspecified technical-value query -> clarification = **PASS**
2. technical answer before required confirmation = **PASS**
3. explicit sufficiently identified query avoids clarification = **PASS**
4. confirmation respected = **PASS**
5. fresh constrained retrieval after confirmation = **PASS**
6. wrong-family answer = **PASS**
7. unresolved confirmation -> clarification, not answer = **PASS**
8. failed evidence -> `no_coverage` = **PASS**
9. pre-confirmation candidate reuse = **PASS**
10. current stable direct retrieval behavior not materially regressed = **PASS**

Evidence for gate 10:
- initial regression against `tests/test_coverage_states.py` was detected during suite run;
- classifier narrowed from generic `procedure`/`step` to `procedure step` only;
- full suite then passed cleanly.

## 10. Limitations
- Scope inference remains intentionally minimal and limited to the accepted SPEC_015 anchors.
- No broad pre-clarification discovery was added.
- No router-v2/router-v3 behavior was introduced.
- Standalone `context_router.py` remains preserved but is not used as the live automatic family selector.

## 11. Rollback plan
If rollback is required:
1. revert the HRM_038 implementation commit only;
2. preserve standalone `context_router.py` and its accepted tests;
3. return live behavior to the pre-HRM_038 stable path at `0ab348cdb7f9d4ff8216fd43b9d4d8db22c4ebce` plus any later unrelated commits not part of this task;
4. do not revive HRM_034 live context-router integration.

## 12. Validation commands run
- `./run_tests.sh tests/test_clarification_first_flow.py::test_clarification_first_fixture_pack_matches_expected_behavior -q`
- `./run_tests.sh tests/test_clarification_first_flow.py -q`
- `./run_tests.sh tests/test_coverage_states.py::test_not_in_context_with_single_source_consensus_preserves_sources tests/test_coverage_states.py::test_not_in_context_with_conflicting_sources_stays_no_coverage -q`
- `./run_tests.sh`
- `./venv/bin/python -m compileall rag_engine tests`
- `git diff --check`

## 13. Classification
`IMPLEMENTATION_COMMITTED` pending commit/push.

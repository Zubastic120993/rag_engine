# HRM_033 — Context router implementation

## 1. Files modified
- `rag_engine/context_router.py`
- `tests/test_context_router.py`
- `tests/test_context_router_confirmation.py`

## 2. Implementation structure
Standalone pure component only.

Primary functions:
- `parse_question_evidence(question_text)`
- `route_context(question_evidence, context_state, clarification_suppressed=False)`
- `route_with_discovery(question_evidence, context_state, discovery_state=None, prior_process_state=None, confirmation_object=None, constrained_retrieval_result=None, clarification_suppressed=False)`

Properties:
- no Chroma dependency
- no DB dependency
- no tracker dependency
- no embedding dependency
- no side effects inside decision functions
- no retrieval integration in this task

## 3. Layer A implementation
Implemented deterministic handling for:
- `NO_CONTEXT`
- `VERIFIED_CONTEXT`
- `PRIOR_TURN_EXPLICIT`
- `INFERRED_CONTEXT`
- `STALE_CONTEXT`
- `CONFLICTED_CONTEXT`
- `CURRENT_TURN_OVERRIDE`

Implemented behavior:
- immutable provenance precedence
- stale/invalidation handling
- equal-authority conflict abstention
- explicit current-turn override
- closed reason-code outputs
- decision object fields required by SPEC_014 fixtures

## 4. Layer B implementation
Implemented discovery / clarification / confirmation control for:
- materially plausible family ambiguity
- mandatory clarification before technical answer on ambiguous multi-family cases
- confirmation object outranking prior/stale context
- mandatory fresh constrained retrieval state after confirmation
- fail-closed behavior when constrained retrieval / authority verification fails
- explicit unambiguous query bypassing unnecessary clarification

Pre-confirmation ambiguous candidate reuse is explicitly blocked in decision outputs.

## 5. Decision schema
Layer A/Later B decision payload includes:
- `decision`
- `selected_scope`
- `routing_mode`
- `fields_used`
- `provenance_used`
- `explicit_override`
- `invalidated_fields`
- `stale_fields`
- `conflict_fields`
- `reason_code`
- `clarification_prompt_class`

Layer B additive fields:
- `process_state`
- `technical_answer_allowed`
- `clarification_required`
- `constrained_retrieval_required`
- `explicit_confirmation`
- `reuse_preconfirmation_candidates`

## 6. Fixture execution results
### Layer A fixture pack
Source:
- `/Users/vladymyrzub/Projects/ai-engineering-orchestrator/eval/context_router_fixtures_v1.json`

Result:
- fixtures: `240/240` matched
- wrong scope: `0/240`
- explicit override outputs: `57` total
  - required explicit-override variant success: `48/48`
  - stale-question explicit rescue cases: `9/9`
- unresolved conflict abstention: `48/48`
- stale silent-route error: `0`
- standalone correct routing: `9/48`
- correct-context routing: `48/48`

### Layer B fixture pack
Source:
- `/Users/vladymyrzub/Projects/ai-engineering-orchestrator/eval/context_router_confirmation_fixtures_v1.json`

Result:
- fixtures: `8/8` matched
- multi-family clarification: `100%`
- no technical answer before confirmation in ambiguous cases: `100%`
- explicit confirmation override: `100%`
- constrained retrieval required after confirmation: `100%`
- ambiguous pre-confirmation reuse: `0`
- wrong-family answer: `0`
- explicit unambiguous query avoids unnecessary clarification: `100%`

## 7. Hard safety gates
PASS:
- Layer A wrong-scope rate `<= 2%`
- Layer A explicit override `48/48`
- Layer A unresolved conflict abstention `48/48`
- Layer A stale silent-route error `0`
- Layer B multi-family clarification `100%`
- Layer B no technical answer before confirmation `100%`
- Layer B confirmation override `100%`
- Layer B fresh constrained retrieval required after confirmation `100%`
- Layer B ambiguous evidence reuse `0`
- Layer B wrong-family answer `0`
- Layer B explicit unambiguous bypass present

## 8. Limitations
- This task implemented only the standalone state-machine component and fixture validation.
- No live retrieval integration was added.
- No `query.py` / answer-path wiring was changed.
- No `scopes.yaml` change was made.
- Question explicit-anchor detection is intentionally narrow and bounded to accepted SPEC_014 behavior and fixture coverage.

## 9. Whether live integration is justified
Yes — **as a separate gated task only**.

This implementation proves the accepted SPEC_014 state machine and both fixture packs executable. It does **not** by itself authorize changing live retrieval/answer flow without a separate controlled integration step and focused gate.

## 10. Rollback plan
If later integration fails its focused hard gate:
1. revert the router integration commit(s), not the accepted fixture packs/specs;
2. preserve this standalone module/tests/report as implementation evidence;
3. record failed gate explicitly;
4. do not start Router v2 / v3 tuning loop without a new operator decision.

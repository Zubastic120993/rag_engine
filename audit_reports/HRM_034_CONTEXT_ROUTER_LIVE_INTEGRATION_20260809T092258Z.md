# HRM_034 — Context Router Live Integration

## 1. Files modified
- `rag_engine/query.py`
- `tests/test_context_router_live_integration.py`
- `audit_reports/HRM_034_CONTEXT_ROUTER_LIVE_INTEGRATION_20260809T092258Z.md`

No changes were made to:
- `rag_engine/context_router.py`
- `rag_engine/cli.py`
- retrieval ranking
- embeddings
- `scopes.yaml`
- indexes / reindex flow

## 2. Integration points
Integrated the accepted HRM_033 router into the live `answer()` path in `rag_engine/query.py`.

Added live-path behavior:
1. parse current question into router evidence
2. run accepted Layer A router before retrieval when `scope` is not explicitly supplied
3. run discovery retrieval when required by router state
4. convert discovery results into materially plausible family candidates
5. if multiple plausible families remain, return clarification and stop before answer generation
6. if confirmation object is supplied, force fresh constrained retrieval in the confirmed scope
7. preserve existing authority / confidence gates after constrained retrieval
8. attach router/discovery audit data under `retrieval_diagnostics["context_router"]`

Preserved existing behavior when external scope is already supplied:
- `answer(..., scope="...")` continues directly with constrained retrieval
- no scope registry changes
- no ranking changes

## 3. Explicit query flow
Implemented direct no-clarification flow for explicit / unambiguous current-turn questions.

Observed in focused validation:
- `In M 1.3, what is the M42 tightening torque?` -> router `ROUTE` -> scope `me-c` -> direct constrained retrieval -> answer
- `For Yanmar 6EY22, what is the tightening torque?` -> router `ROUTE` -> scope `maker-manuals` -> direct constrained retrieval -> answer

## 4. Ambiguous query flow
Implemented fail-closed ambiguity handling.

For vague technical-value questions with no unique family anchor:
1. run discovery retrieval without silent family selection
2. build plausible-family set from surviving authority-eligible candidates
3. if two or more materially plausible families remain, return clarification
4. do not call answer generation
5. do not emit final answer sources

Observed in focused validation:
- `What is the torque?` -> `CLARIFY_MULTI_FAMILY`
- `What is the alarm setpoint?` -> `CLARIFY_MULTI_FAMILY`
- stale prior MAN context + vague query still clarified after discovery ambiguity
- conflicting context clarified immediately without silent route

## 5. Confirmation flow
Implemented explicit confirmation handling through `answer(..., confirmation_object=...)`.

Behavior:
1. confirmation is treated as authoritative current-turn confirmation
2. alternatives are invalidated in router decision data
3. a fresh constrained retrieval is run in the confirmed scope
4. pre-confirmation ambiguous candidates are not reused as final answer evidence
5. normal authority / confidence gates still decide whether answer is allowed

Observed in focused validation:
- ambiguity -> confirm `MAN G50ME-C` -> constrained retrieval in `me-c` -> answer from MAN source
- ambiguity -> confirm `Yanmar 6EY22` -> constrained retrieval in `maker-manuals` -> answer from Yanmar source
- confirmation + no acceptable constrained evidence -> fail-closed `no_coverage`

## 6. Fresh-retrieval proof
Focused validation call traces:
- ambiguity -> confirm MAN: retrieval scopes `[null, "me-c"]`
- ambiguity -> confirm Yanmar: retrieval scopes `[null, "maker-manuals"]`

This proves:
- first call was discovery only
- second call was a new constrained retrieval
- final answer evidence came from the constrained pass, not from the ambiguous discovery pass

## 7. Candidate-leakage proof
Router audit data now records:
- discovery ran or not
- plausible families
- clarification issued or not
- confirmation applied or not
- fresh constrained retrieval ran or not
- `preconfirmation_candidate_reuse`

Focused validation result:
- `preconfirmation_candidate_reuse = false` in all 10/10 focused cases

## 8. Focused live results
| # | Initial question | Context state | Router decision | Discovery ran | Plausible families | Clarification | User confirmation | Fresh retrieval | Selected scope | Final source/page | Authority result | Final status | Pre-confirmation reuse |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | In M 1.3, what is the M42 tightening torque? | NO_CONTEXT | ROUTE | No | — | No | — | No | `me-c` | `M 1.3.pdf` p.42 | `ok` | `ok` | No |
| 2 | For Yanmar 6EY22, what is the tightening torque? | NO_CONTEXT | ROUTE | No | — | No | — | No | `maker-manuals` | `OPERATION MANUAL_6EY22.pdf` p.73 | `ok` | `ok` | No |
| 3 | What is the torque? | NO_CONTEXT | CLARIFY_MULTI_FAMILY | Yes | MAN_G50ME-C_LGIP, Yanmar_6EY22 | Yes | — | No | — | — | `clarification_required` | `clarification_required` | No |
| 4 | What is the alarm setpoint? | NO_CONTEXT | CLARIFY_MULTI_FAMILY | Yes | MAN_G50ME-C_LGIP, Yanmar_6EY22 | Yes | — | No | — | — | `clarification_required` | `clarification_required` | No |
| 5 | What is the torque? | NO_CONTEXT | CLARIFY_MULTI_FAMILY -> CONFIRMED_CONSTRAINED_RETRIEVAL | First call only | MAN + Yanmar on discovery pass | Yes on first call | MAN G50ME-C | Yes | `me-c` | `M 1.3.pdf` p.42 | `ok` | `ok` | No |
| 6 | What is the torque? | NO_CONTEXT | CLARIFY_MULTI_FAMILY -> CONFIRMED_CONSTRAINED_RETRIEVAL | First call only | MAN + Yanmar on discovery pass | Yes on first call | Yanmar 6EY22 | Yes | `maker-manuals` | `OPERATION MANUAL_6EY22.pdf` p.73 | `ok` | `ok` | No |
| 7 | What is the torque? | STALE_CONTEXT | CLARIFY_MULTI_FAMILY | Yes | MAN_G50ME-C_LGIP, Yanmar_6EY22 | Yes | — | No | — | — | `clarification_required` | `clarification_required` | No |
| 8 | What is the torque? | CONFLICTED_CONTEXT | ABSTAIN_CLARIFY | No | — | Yes | — | No | — | — | `clarification_required` | `clarification_required` | No |
| 9 | For Yanmar 6EY22, what is the tightening torque? | STALE_CONTEXT | ROUTE | No | — | No | — | No | `maker-manuals` | `OPERATION MANUAL_6EY22.pdf` p.73 | `ok` | `ok` | No |
| 10 | What is the torque? | NO_CONTEXT | CONFIRMED_CONSTRAINED_RETRIEVAL | No | — | No | MAN G50ME-C | Yes | `me-c` | — | `authority_verification_failed` | `no_coverage` | No |

### ask_events
Normal CLI-path check executed separately.

- Path: `/Users/vladymyrzub/CE_Library/.rag_db/ask_events.jsonl`
- Before: `393`
- After: `395`
- Delta: `2`

No extra logging mutations were introduced outside the standard path.

## 9. Hard-gate verdict
1. Multi-family ambiguity -> clarification = **PASS**
2. Technical answer before required confirmation = **PASS**
3. Explicit confirmation respected = **PASS**
4. Fresh constrained retrieval after confirmation = **PASS**
5. Pre-confirmation ambiguous candidate reuse = **PASS**
6. Wrong-family technical answers = **PASS**
7. Explicit unambiguous queries avoid unnecessary clarification = **PASS**
8. Stale/conflicting context does not silently route = **PASS**
9. GQ-006-style negative safety remains fail-closed = **PASS**
10. Existing direct retrieval behavior is not materially changed = **PASS**

Overall hard-gate verdict: **PASS**

## 10. Limitations
- Multi-turn confirmation is integrated at the query/answer layer via `confirmation_object`; CLI conversational state was not redesigned in this task.
- Focused validation was intentionally bounded to the required scenarios; the full 48-question benchmark was not run in this task.
- Discovery-family assessment uses existing surviving retrieval candidates and authority metadata only; no retrieval-ranking or scope semantics were changed.

## 11. Rollback plan
If rollback is required:
1. revert the HRM_034 commit in `rag_engine`
2. rerun `./run_tests.sh`
3. rerun `./venv/bin/python -m compileall rag_engine tests`
4. rerun `git diff --check`
5. confirm live path is back to pre-HRM_034 behavior

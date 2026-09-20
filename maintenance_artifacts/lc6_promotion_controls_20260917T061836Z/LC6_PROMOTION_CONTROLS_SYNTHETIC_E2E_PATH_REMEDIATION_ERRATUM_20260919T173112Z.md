# LC6 Promotion Controls Synthetic E2E Path Remediation Erratum

Timestamp UTC: 20260919T173112Z

Scope: additive package only.

Package:
`/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_promotion_controls_20260917T061836Z`

## Historical evidence preserved

Failed post-remediation synthetic E2E report remained unchanged:

- File: `LC6_PROMOTION_CONTROLS_POST_REMEDIATION_SYNTHETIC_E2E_20260917T074051Z.json`
- SHA-256: `0fdbb8e2ad8bbfe8220b994e6f03a5238125f25da65589d5e346ad796b7c8017`

Diagnosis report remained unchanged:

- File: `LC6_PROMOTION_CONTROLS_SYNTHETIC_E2E_FAILURE_DIAGNOSIS_20260917T074051Z.md`
- SHA-256: `2ded6661ea0558b3a3f85cf110d2dbd6d1008babc2d20deddb746dacbdbd21a6`

## Remediation applied

1. `atomic_switch()` now prepares the governed journal parent before opening `switch.journal.json.lock`.
2. Journal parent preparation creates and fsyncs the parent and parent directory.
3. Unsafe journal parents are refused when outside the synthetic fixture, symlinked, or protected.
4. Package-local synthetic E2E driver helpers were added for:
   - work-directory creation before invoking switch logic;
   - full traceback, stage, operation, and target path capture in failure reports;
   - failed-fixture preservation by default;
   - successful-fixture cleanup.
5. Regression tests were added for the diagnosed path and evidence-preservation defects.

## Validation result

Complete package test suite:

- Passed: 20
- Failed: 0
- Errors: 0
- Skipped: 0

Syntax validation:

- `promotion_controls.py`: PASS
- `synthetic_e2e_driver.py`: PASS
- `test_promotion_controls.py`: PASS

Package-local cache cleanup:

- Removed: `__pycache__`
- Remaining `__pycache__`: 0
- Remaining `.pyc`: 0

## Boundary statement

No complete synthetic E2E retry was run.
No real `.hermes/.env`, Hermes process, production, baseline, candidates, sealed packages, production lock, promotion, staging, commit, push, or sealing action was performed.

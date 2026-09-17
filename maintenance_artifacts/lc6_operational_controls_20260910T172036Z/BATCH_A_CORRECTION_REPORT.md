# Batch A Forward Correction Report

Package: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z`

Scope: additive package only. Earlier Batch A failure result is preserved separately and not rewritten.

## Correction summary

- Separated candidate validation lifecycle into pre-mutation and post-mutation stages.
- Pre-mutation validation keeps mandatory marker/source-hash/source-embedding guard and fsyncs validated pre-state to journal.
- Post-mutation validation checks marker provenance against the journaled pre-state instead of current whole-file source hashes.
- Added bounded post-mutation checks for file set, changed-file scope, SQLite counts, tracker metadata, ordered embedding IDs, vector count, dtype, dimensions, per-vector dimensions, WAL/SHM/lock state, and payload digest.
- Embedding payload digest mismatch is now specifically reported as `embedding_payload_digest_mismatch` when one float changes while IDs and count remain unchanged.
- Contract and runbook updated to document the lifecycle.

## Validation performed

1. Remaining embedding test only:
   - `test_wrong_source_hash_marker_and_embedding_float_change_fail`
   - Result: `1 passed in 0.33s`

2. Both original failing test IDs:
   - `test_failure_boundaries_preserve_journals[after_mutation-candidate_create-args2]`
   - `test_wrong_source_hash_marker_and_embedding_float_change_fail`
   - Result: `2 passed in 0.37s`

3. Complete additive-package pytest suite:
   - Passed: `18`
   - Failed: `0`
   - Errors: `0`
   - Skipped: `0`
   - Result: `18 passed in 0.97s`

4. Compile check:
   - `python3 -m py_compile lc6_operational_controls.py test_lc6_operational_controls.py`
   - Result: `PASS`

5. Artifact hygiene:
   - Removed package-local generated caches only:
     - `__pycache__/lc6_operational_controls.cpython-312.pyc`
     - `__pycache__/test_lc6_operational_controls.cpython-312.pyc`
     - `__pycache__/`
   - Remaining `__pycache__` / `.pyc`: none
   - Outside-package removal: `false`

## Complete-file SHA-256 values

Self-hash note: this report intentionally does not include its own hash inside the file.

| File | SHA-256 |
|---|---|
| `ARTIFACT_SHA256SUMS.txt` | `bf0cb4e0821b69eb86ac55dd675613d9b8070557f142cd3bc4cd77116fe4fb81` |
| `BATCH_A_FAILURE_ANALYSIS.md` | `a91a385953acd056990c1eaf3317ffe4b1ef4a65678ec505140c8e03968d0756` |
| `ERRATUM_PARTIAL_REPORT_FORWARD.md` | `9113bf3a007d45303f563f8dd09f038a209063a27942e60cb27767564f4a06f6` |
| `LC6_OPERATIONAL_CONTROLS_CONTRACT_V1.md` | `3783d8e7ff70819d10c4591a647b349c3307c53ad9106670877bc05da5a927ff` |
| `PRODUCTION_UNCHANGED_REPORT.md` | `1d227f90e2335fedb57819524e430ad4ae24250167b9f0e911db33b7152aa89a` |
| `README.md` | `6a713aff98d5f108d140c66a7f7cefbe6465940ed3737ca92b2dc11917c06111` |
| `RUNBOOK.md` | `4c7e2dabb9d47077ee07c50a01b41a69641764f8bd73557987f3aa1b89718c21` |
| `VALIDATION_REPORT.md` | `ed091e0add56652ab233707afdf52bd1c967c86bbfcace7efc5b9a187aa8aca3` |
| `lc6_operational_controls.py` | `f894881ad67b0025bc642837eeb92a16480ba0399ceb6aa32cd1bf7c743a44dd` |
| `test_lc6_operational_controls.py` | `58b3c55e75b8db9cca96d40fcac6147b9c26cbd9ff1227e48e80f94000157ad5` |

## Boundary confirmation

Not performed:
- sealed-executor integration;
- production access or modification;
- certified baseline access or modification;
- sealed LC6 package modification;
- real candidate creation;
- production lock acquisition;
- production selection switch;
- seal, commit, or push.

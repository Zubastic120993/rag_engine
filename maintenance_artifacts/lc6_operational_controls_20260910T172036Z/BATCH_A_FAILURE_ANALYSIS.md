# Batch A Failure Analysis — Pre-Remediation

Package: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z`

Scope: Batch A remediation only. No production, certified baseline, sealed LC6 package, lock, selection, seal, commit, or push.

## Failing test 1

Test ID:
`maintenance_artifacts/lc6_operational_controls_20260910T172036Z/test_lc6_operational_controls.py::test_failure_boundaries_preserve_journals[after_mutation-candidate_create-args2]`

Complete traceback root cause:
- `candidate_create()` receives the fourth positional argument from the parametrized test as a `Path` object: `/private/tmp/.../purpose`.
- The value is stored directly in the candidate marker field `purpose`.
- `write_json_atomic()` calls `json.dump()` and fails with:
  `TypeError: Object of type PosixPath is not JSON serializable`.

Contract impact:
- The `after_mutation` injection boundary is not reached.
- The failure is an unhandled serialization exception, not a governed `ControlRefusal`.
- The journal is not updated with the exact post-copy residual destination state, recovery owner, required action, failed-artifact preservation policy, or double-rollback status.
- A partial destination and temporary marker file may exist without the required failure evidence being recorded.

Required correction:
- Normalize `purpose` to string before journal/marker serialization.
- Wrap post-copy marker/report validation and `after_mutation` injection in a governed failure path.
- On failure after mutation, update the journal with exact residual destination state, recovery owner, required action, failed-artifact preservation, and `double_rollback_prevented`.
- Preserve the failed candidate evidence instead of silently deleting or rolling back.

## Failing test 2

Test ID:
`maintenance_artifacts/lc6_operational_controls_20260910T172036Z/test_lc6_operational_controls.py::test_wrong_source_hash_marker_and_embedding_float_change_fail`

Complete traceback root cause:
- The test first corrupts `source_file_hashes` and confirms the whole-file/source-hash guard fails.
- It then restores `source_file_hashes` from the backup report.
- It changes one actual float payload in SQLite table `embeddings.embedding` for `embedding_id='c1'`.
- `postcheck()` calls `validate_candidate_marker()` first.
- `validate_candidate_marker()` compares current whole-file hashes against marker source hashes, detects the changed `chroma.sqlite3`, and raises:
  `candidate marker source hashes do not match current unrepaired candidate`.
- Therefore the test never reaches the dedicated embedding-payload digest comparison.

Contract impact:
- The production guard is fail-closed, but this test does not prove the dedicated embedding-payload invariant.
- The required invariant is: IDs and vector count unchanged, one actual float payload changed, internally valid marker/source identity, postcheck reaches embedding digest comparison, and the failure is specifically attributed to payload-digest mismatch.

Required correction:
- Keep the whole-file/source-hash guard and test it separately.
- For the payload-digest test only, maintain internally valid source identity after intentional SQLite payload mutation so marker validation can proceed.
- Assert embedding IDs and row count are unchanged before/after mutation.
- Assert the embedding payload digest changes.
- Ensure `postcheck()` fails specifically with an embedding payload digest mismatch, not at whole-file/source-hash validation.

# LC6 Promotion Controls Synthetic E2E Failure Diagnosis

Report under diagnosis:

`/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_promotion_controls_20260917T061836Z/LC6_PROMOTION_CONTROLS_POST_REMEDIATION_SYNTHETIC_E2E_20260917T074051Z.json`

Expected SHA-256:

`0fdbb8e2ad8bbfe8220b994e6f03a5238125f25da65589d5e346ad796b7c8017`

Verified SHA-256:

`0fdbb8e2ad8bbfe8220b994e6f03a5238125f25da65589d5e346ad796b7c8017`

## Scope

Read-only diagnosis only. No patch, no E2E rerun, no real `.hermes/.env` access, no Hermes restart, no production / baseline / candidate / sealed-package mutation, no lock acquisition, no promotion, no commit, no push, no sealing.

## 1. Extracted failure evidence

The failure report preserves only a shortened exception representation, not a full traceback.

Report evidence:

- `LC6_PROMOTION_CONTROLS_POST_REMEDIATION_SYNTHETIC_E2E_20260917T074051Z.json:73`
  - `"error": "FileNotFoundError(2, 'No such file or directory')"`
- `LC6_PROMOTION_CONTROLS_POST_REMEDIATION_SYNTHETIC_E2E_20260917T074051Z.json:76-79`
  - synthetic fixture path:
    `/private/tmp/lc6_post_remediation_e2e_20260917T074051Z_2090rm_3`
  - `removed_after_report: true`
- `LC6_PROMOTION_CONTROLS_POST_REMEDIATION_SYNTHETIC_E2E_20260917T074051Z.json:80`
  - verdict:
    `LC6_PROMOTION_CONTROLS_SYNTHETIC_E2E_FAIL_PRESERVED`

Full traceback: not available in the preserved report. The E2E driver caught the exception and stored `repr(exc)` only. This is a report-evidence limitation.

## 2. Failing operation and requested path

Most probable failing operation:

`os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)` in `atomic_switch()`.

Line reference:

- `promotion_controls.py:333-334`
  - `lock_path = journal.with_suffix(journal.suffix + ".lock")`
  - `lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)`

The E2E driver used a journal under a synthetic work directory:

- `journal = fixture / 'work' / 'switch.journal.json'`

Therefore the requested lock path would have been:

`/private/tmp/lc6_post_remediation_e2e_20260917T074051Z_2090rm_3/work/switch.journal.json.lock`

The parent directory `/private/tmp/lc6_post_remediation_e2e_20260917T074051Z_2090rm_3/work` was not proven created before `atomic_switch()` attempted to open the lock file.

## 3. Immediately preceding completed stage

Based on the E2E driver sequence preserved in the session context, the immediately preceding completed stages were:

1. Fresh synthetic fixture root created under `/private/tmp`.
2. Synthetic old/new generation directories created.
3. Synthetic `.env` written.
4. `FakeHermesSupervisor` instantiated.
5. `validate_generation_target(str(new))` completed successfully.
6. Failure occurred at the start of `atomic_switch()` before PREPARED journal write.

The report confirms no synthetic workflow details were recorded, which is consistent with failure before the switch journal stage.

## 4. Missing path classification

Missing path classification:

`never created`

Reason:

- `promotion_controls.py:333-334` opens the lock file before any call that creates the journal parent directory.
- `_write_json_durable()` would create the journal parent directory at `promotion_controls.py:116-117`, but this function is reached after lock acquisition.
- In this failure path, the lock acquisition happens before `_write_json_durable()` can create `journal.parent`.
- The E2E driver expected a durable journal/work directory but did not create `fixture/work` before calling `atomic_switch()`.

Not supported by evidence:

- created under a different name: not supported;
- deleted too early before failure: not supported;
- referenced after cleanup: not the primary failure; cleanup occurred after failure capture;
- constructed incorrectly: partially applicable only if the driver contract required pre-creating `work/` and omitted it. The path shape itself was reasonable.

## 5. Trace through code and tests

### Production code

- `promotion_controls.py:333-334`
  - constructs and opens the advisory lock path.
  - this is the failing operation if `journal.parent` does not exist.

- `promotion_controls.py:116-117`
  - `_write_json_durable()` creates `path.parent`.
  - this would create the journal parent, but it is not reached before lock open.

- `promotion_controls.py:338-339`
  - `_assert_env_unchanged_before_replace()` and `_write_json_durable(journal, prepared)` occur after lock acquisition.

### Tests

The current tests do not cover a missing journal parent directory.

Examples:

- `test_promotion_controls.py:83-90`
  - uses `journal = self.tmp / "switch.journal.json"`; parent already exists.
- `test_promotion_controls.py:106-108`
  - same parent-existing pattern.
- `test_promotion_controls.py:141-146`
  - failure-injection journals are direct children of `self.tmp`; parent exists.
- `test_promotion_controls.py:208-229`
  - unresolved/finalized journal tests use direct `self.tmp` journal paths; parent exists.

Therefore the passing suite did not exercise the E2E driver condition where `journal = fixture / 'work' / 'switch.journal.json'` and `fixture/work` was absent.

### E2E driver / report writer

The E2E driver was executed from the prior Hermes `execute_code` call, not from a package file. It created the fixture and later wrote the JSON report. It recorded only `repr(exc)` and not a traceback.

Report writer evidence:

- `LC6_PROMOTION_CONTROLS_POST_REMEDIATION_SYNTHETIC_E2E_20260917T074051Z.json:73`
  - only short exception text was preserved.
- `LC6_PROMOTION_CONTROLS_POST_REMEDIATION_SYNTHETIC_E2E_20260917T074051Z.json:76-79`
  - fixture path and cleanup status were preserved.

## 6. Cleanup timing and evidence loss

Cleanup occurred after exception capture but before diagnosis could inspect the fixture.

Evidence:

- report line `76-78` records fixture path and `removed_after_report: true`.

Effect:

- the synthetic fixture and any partial work directory / lock path evidence were erased;
- this prevents direct inspection of whether `work/` existed at failure time;
- because no full traceback was stored, diagnosis relies on code-path reconstruction and the preserved `FileNotFoundError` summary.

Cleanup-order classification:

- cleanup after failure is normal for synthetic fixture hygiene;
- however, for FAIL_PRESERVED evidence, immediate deletion erased useful forensic evidence;
- this is a secondary cleanup-evidence preservation defect, not the primary runtime failure.

## 7. Root cause classification

Primary root cause:

`test-driver defect`

Reason:

- the E2E requirement said the fixture should contain a durable journal/work directory;
- the driver used `fixture/work/switch.journal.json` but did not prove/create `fixture/work` before calling `atomic_switch()`.

Secondary implementation robustness gap:

- `atomic_switch()` creates the lock path before ensuring `journal.parent` exists;
- `_write_json_durable()` can create the parent, but only after the lock is opened.

Secondary report-only defect:

- report preserved only `repr(exc)`, not full traceback or stage marker.

Secondary cleanup-order / evidence defect:

- failed fixture was removed, so the missing path could not be inspected after failure.

## 8. Smallest safe correction, no changes made

Smallest safe correction options:

1. E2E driver correction:
   - create the synthetic work directory before switch:
     `journal.parent.mkdir(parents=True, exist_ok=True)`
   - add stage markers before each E2E step;
   - on failure, store full traceback and current stage.

2. Implementation hardening correction:
   - inside `atomic_switch()`, ensure `journal.parent.mkdir(parents=True, exist_ok=True)` before constructing/opening `lock_path`.
   - this would align lock creation with `_write_json_durable()` parent-creation behavior.

3. Failure-evidence correction:
   - for FAIL_PRESERVED E2E, preserve the synthetic fixture or copy a minimal redacted failure evidence bundle before cleanup;
   - at minimum store full traceback, failing path, stage marker, and whether each expected fixture directory existed.

No correction was applied in this diagnosis.

## 9. Tests required before rerun

Required tests before another E2E attempt:

1. Unit test: `atomic_switch()` with `journal` inside a missing parent directory either:
   - creates the parent safely before lock acquisition, or
   - fails with a controlled governed error that identifies the missing journal parent.

2. E2E-driver test: fixture construction explicitly creates:
   - synthetic `.env`;
   - old/new generation paths;
   - durable `work/` journal directory;
   - fake supervisor.

3. Report-writer test: on exception, report contains:
   - full traceback;
   - failing stage;
   - failing operation;
   - requested path if available;
   - fixture preservation / cleanup status.

4. Failure-preservation test:
   - when synthetic E2E fails before switch completion, required forensic evidence remains available or is copied into the report.

## 10. Historical preservation

Historical reports were not modified:

- `LC6_PROMOTION_CONTROLS_POST_REMEDIATION_SYNTHETIC_E2E_20260917T074051Z.json` remains unchanged.
- This diagnosis is additive and forward-only.

## Final diagnosis status

`LC6_PROMOTION_CONTROLS_SYNTHETIC_E2E_DIAGNOSIS_COMPLETE_NO_PATCH`

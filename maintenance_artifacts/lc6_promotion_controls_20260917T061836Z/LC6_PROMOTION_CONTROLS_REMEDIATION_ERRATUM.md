# LC6 Promotion Controls Remediation Erratum

Package: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_promotion_controls_20260917T061836Z`

Status: forward erratum after failed validation result.

## Preserved failed validation result

The earlier validation run collected required safety-scenario tests and found four failed/error scenarios:

- concurrent `.env` modification was not detected before replacement;
- second switch using an existing committed journal was not refused;
- synthetic backend start failure was not implemented;
- rollback atomic restore failure propagated raw `OSError` and did not persist governed residual state.

That failed result is preserved as historical validation evidence. This erratum does not rewrite or remove the earlier failure.

## Remediation applied

Package-local implementation and tests were updated only inside this package.

Remediated controls:

1. Concurrent synthetic `.env` modification detection now compares captured bytes, hash, inode, size, mode, ownership, and timestamp metadata immediately before atomic replacement. Expected/observed differences are recorded in the durable journal. A synthetic advisory switch lock serializes cooperating switch operations, but ungoverned external editors remain controlled by the mandatory final pre-replace comparison.
2. Second-switch refusal now blocks unresolved journals for the same `.env` in `PREPARED`, `COMMITTED`, `RECOVERY_REQUIRED`, `RESIDUAL`, and unknown states. Finalized states `ROLLED_BACK` and `FINALIZED_ARCHIVED` are accepted.
3. `FakeHermesSupervisor` now has deterministic start-failure simulation with old PID, attempted new PID, failure phase, and rollback-request state.
4. Rollback atomic restore failure is caught and converted to governed `RollbackError`; journal state is persisted as `RECOVERY_REQUIRED` with current selection, intended restored selection, backend-state note, and manual recovery action.

## Validation result after remediation

Four previously failed/error test IDs: `4 passed / 0 failed / 0 errors / 0 skipped`.

Complete promotion-controls suite: `15 passed / 0 failed / 0 errors / 0 skipped`.

Syntax/static parse check: PASS.

## Boundaries preserved

No real `.env` edit, production mutation, baseline mutation, candidate mutation, sealed-package mutation, Hermes restart/reload, current environment change, selection change, promotion, commit, or push was performed.

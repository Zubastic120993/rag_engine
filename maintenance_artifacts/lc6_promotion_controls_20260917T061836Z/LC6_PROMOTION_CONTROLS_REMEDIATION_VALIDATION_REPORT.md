# LC6 Promotion Controls Remediation Validation Report

Package: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_promotion_controls_20260917T061836Z`

## Scope

Validation-only completion after remediation of four LC6 Promotion Controls safety scenarios.

No final package reports, checksum sealing, commit, or push were performed.

## Files validated

- `promotion_controls.py`
- `test_promotion_controls.py`
- `SYNTHETIC_E2E_SWITCH_RESTART_VERIFY_ROLLBACK_REPORT.json`

## Syntax / compile check

Method: Python AST parse of implementation and test files, without retained bytecode.

Result: PASS.

## Existing synthetic E2E report hash

File: `SYNTHETIC_E2E_SWITCH_RESTART_VERIFY_ROLLBACK_REPORT.json`

SHA-256:
`11bb1da0fe8231bb0a36bbeec05ebb235c90432bd62316c483c9216b1b49eeed`

## Test result

Previously failed/error test IDs were run first.

Result:
`4 passed / 0 failed / 0 errors / 0 skipped`

Complete promotion-controls suite result:
`15 passed / 0 failed / 0 errors / 0 skipped`

## Real environment / production unchanged evidence

Real `.env` checked read-only. Only the `RAG_DB_PATH` line and metadata were reported; unrelated `.env` values were not printed.

- Real `.env` mode: `0o600`
- Real `.env` uid/gid: `501/20`
- Active `RAG_DB_PATH` assignment count: `1`
- Active `RAG_DB_PATH` line: `483`
- Active `RAG_DB_PATH` value:
  `/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_20260814T182037Z_698e0df44604`

Active production selection checked read-only:

- Active selection:
  `/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_20260814T182037Z_698e0df44604`
- Production file count: `19`
- Production inventory SHA-256:
  `0728d4f6a7d4a7a996ee66a0a1ef225afa42c7fd9428835a5a21131b187b27b9`
- Production lock exists: `false`

Hermes backend process identity checked read-only:

- Hermes app process:
  `PID 700`, parent `1`, start `Sun Sep 13 22:09:24 2026`
- Hermes backend process:
  `PID 1285`, parent `700`, start `Sun Sep 13 22:09:27 2026`

No Hermes restart/reload command was executed by this remediation. Current Hermes process identity is consistent with the earlier observed process chain and predates this remediation.

## Package-local cleanup

Package-local cache search result:

- `*.pyc`: none found
- `__pycache__`: none found

No outside-package removal was performed.

## Complete-file SHA-256 values

- `promotion_controls.py`:
  `aeaf64846542751b763db8121eed271f8f36d8aba73d8b40d6069ac8c9ae5c39`
- `test_promotion_controls.py`:
  `43ce3587ec085ba31b872f36099df267ee120908ac80ed110488cf19bf9c0db5`
- `SYNTHETIC_E2E_SWITCH_RESTART_VERIFY_ROLLBACK_REPORT.json`:
  `11bb1da0fe8231bb0a36bbeec05ebb235c90432bd62316c483c9216b1b49eeed`

Erratum and this report hashes were computed after writing and are reported in the final chat response.

## Boundary confirmation

Not performed:

- real `.env` edit;
- production mutation;
- baseline mutation;
- candidate mutation;
- sealed package mutation;
- Hermes restart/reload;
- current environment mutation;
- selection change;
- production lock acquisition;
- promotion;
- final package reports;
- checksum sealing;
- commit;
- push.

Validation status: PASS for the approved remediation-validation scope.

# LC6 Promotion Controls Versioned Baseline Binding Validation Report

Created UTC: 2026-09-19T22:42:42Z

## Verdict
`LC6_PROMOTION_CONTROLS_VERSIONED_BASELINE_BINDING_VALIDATED_STOP_BEFORE_E2E_RETRY`

## Prerequisites verified
- Baseline certification: `09b69ba17c559e0ee535f4b724ed7482a71578608e7699f2eff260e21ef86b70`
- Production-unchanged report: `11aab5550a773c4dd57d0c9565060b649390ac0ac35260b70dbd2ec239232c8e`
- Path-remediation validation report: `6becb7a0ffdf3e7b3d261cedbce36286e1ab2b5faaf3ee173523fee345bda25b`

## Targeted versioned-baseline tests
Command: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v <10 targeted versioned-baseline tests>`

Result:
- Ran: `10`
- Passed: `10`
- Failed: `0`
- Errors: `0`
- Skipped: `0`

Included missing/failed `certification_result` regression test.

## Complete promotion-controls suite
Command: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_promotion_controls.py`

Result:
- Ran: `30`
- Passed: `30`
- Failed: `0`
- Errors: `0`
- Skipped: `0`

## Syntax / compile validation
AST parse passed for:
- `promotion_controls.py`
- `synthetic_e2e_driver.py`
- `test_promotion_controls.py`

## Cache cleanup
Package-local cache cleanup verified:
- `__pycache__` remaining: `0`
- `.pyc` remaining: `0`

## Binding verification
Code binding confirmed for:
- source-content rows `11`
- source-content checksum `004737754f216a38494eeca739590e2535560ed21ed0159580bed48e43ed56e3`
- provenance rows `20`
- provenance checksum `b98465e70b9a8226cca41ab40882e15463cd4591ede67ccdb16aa8f688f8d9df`
- exact authoritative 55-orphan identity set
- certified selection mechanism and active generation
- semantic counts and tracker/SQLite equality contract
- WAL/SHM/lock clear state and writer-clear state
- historical append-chain disclosure
- certified baseline status `CERTIFIED`

## Complete-file SHA-256 values
- `promotion_controls.py`: `293df14d3e8918c8875aa56165b3ae492cb106094659b616959e2cb1db598caf`
- `synthetic_e2e_driver.py`: `352f3c707eea44e63c80ca4b4e8d352667947038956f06a8a9f12aec5ab335cd`
- `test_promotion_controls.py`: `ea1a512b2555a93c81ae8199dcf8e9232efe38c4de1952305cd91186cf606182`
- Erratum: `7969cdd5a8305bf8ff282f98102f47b8fcce7b60bfd092d100651a87fac4bf0c`
- This validation report: recorded externally after file close; no self-hash stored inside this report.

## Stop rule
Stopped before synthetic E2E retry, sealing, staging, commit, or push.

## Production safety
No `.hermes/.env` edit, Hermes restart, production Chroma open, production modification, baseline/candidate copy, lock acquisition, repair, or promotion was performed.

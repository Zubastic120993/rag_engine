# LC6 Promotion Controls Versioned Baseline Binding Erratum

Created UTC: 2026-09-19T22:42:42Z

## Status
Forward erratum only. Historical reports are preserved unchanged.

## Scope
This erratum records the package-local versioned-baseline binding update for LC6 promotion controls.

## Historical disclosure preserved
- The old certified append continuity chain remains incomplete.
- The rolled-back append is not retrospectively proven clean.
- The versioned baseline certification is independent current-state certification, not repair of historical evidence.

## Binding correction
The promotion controls are bound to the certified evolved pre-repair baseline artifact:

- Baseline certification SHA-256: `09b69ba17c559e0ee535f4b724ed7482a71578608e7699f2eff260e21ef86b70`
- Source-content rows: `11`
- Source-content checksum: `004737754f216a38494eeca739590e2535560ed21ed0159580bed48e43ed56e3`
- Provenance rows: `20`
- Provenance checksum: `b98465e70b9a8226cca41ab40882e15463cd4591ede67ccdb16aa8f688f8d9df`
- Required status: `CERTIFIED`

## Added fail-closed guard
The validator now rejects missing or failed `certification_result` instead of accepting only schema/hash/count evidence.

## Files changed
- `promotion_controls.py` SHA-256: `293df14d3e8918c8875aa56165b3ae492cb106094659b616959e2cb1db598caf`
- `test_promotion_controls.py` SHA-256: `ea1a512b2555a93c81ae8199dcf8e9232efe38c4de1952305cd91186cf606182`

## Explicit non-authorization
This erratum does not authorize production repair, candidate creation, selection change, restart, promotion, sealing, staging, commit, push, or synthetic E2E retry.

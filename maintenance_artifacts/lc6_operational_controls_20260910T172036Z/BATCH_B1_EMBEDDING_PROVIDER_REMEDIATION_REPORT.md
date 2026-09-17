# Batch B1 Embedding Provider Remediation Report

Package: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z`

Scope: additive package only. Batch B1 prerequisite workflow was not rerun. Batch B2 was not started.

## Historical failure preserved

Failed report preserved unchanged:
`BATCH_B1_PREREQUISITE_REPORT.json`

Preserved failed-report SHA-256:
`5bb53735d62fdcca70a7f316820837191daaf26ef24a31b50a617b8ce72d6167`

Forward erratum written:
`BATCH_B1_EMBEDDING_PROVIDER_ERRATUM.md`

## Remediation summary

- Removed the invalid production assumption that raw vectors are available in SQLite column `embeddings.embedding`.
- Added Chroma public collection API embedding digest provider for disposable `/private/tmp` generations only.
- Protected production, certified external baseline, CE Library `.rag_db`, and generations root paths are refused for Chroma public API embedding reads.
- Added deterministic bounded-page digesting with:
  - expected single collection;
  - bounded page retrieval;
  - deterministic sort by vector/embedding ID;
  - ID, dimension, dtype, and actual vector values bound into the digest;
  - canonical byte format `float32-le`;
  - payload digest, vector count, ordered-ID digest, dimensions, provider/version metadata, and pagination evidence.
- Failure conditions added for duplicate IDs, missing vectors, incomplete pagination, inconsistent dimensions, non-finite floats, unexpected collections, and protected-path Chroma open attempts.
- Batch B1 lifecycle updated so protected baseline/production snapshots defer embedding payload digest to `DEFERRED_TO_BATCH_B2_DISPOSABLE_COPY`.

## Validation performed

Targeted embedding-provider tests:
- `test_embedding_provider_deterministic_pagination_and_page_order_independence`
- `test_embedding_provider_one_float_changes_digest_with_ids_and_count_fixed`
- `test_embedding_provider_duplicate_missing_and_nonfinite_fail`
- `test_embedding_provider_refuses_production_and_certified_baseline_chroma`

Targeted result:
- Passed: `4`
- Failed: `0`
- Errors: `0`
- Skipped: `0`
- Summary: `4 passed in 0.20s`

Complete additive-package suite:
- Passed: `22`
- Failed: `0`
- Errors: `0`
- Skipped: `0`
- Summary: `22 passed in 0.93s`

Compile check:
- Result: `PASS`

Artifact hygiene:
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
| `BATCH_A_CORRECTION_REPORT.md` | `1cc070d301bed01d0acd7312677ad9a60b641cd3dfa898c00945b91382171d31` |
| `BATCH_A_FAILURE_ANALYSIS.md` | `a91a385953acd056990c1eaf3317ffe4b1ef4a65678ec505140c8e03968d0756` |
| `BATCH_B1_EMBEDDING_PROVIDER_ERRATUM.md` | `ead6601d87374665cd7f54724e8004113fcc722fcd0cf66c70b85a3659c6abc1` |
| `BATCH_B1_PREREQUISITE_REPORT.json` | `5bb53735d62fdcca70a7f316820837191daaf26ef24a31b50a617b8ce72d6167` |
| `ERRATUM_PARTIAL_REPORT_FORWARD.md` | `9113bf3a007d45303f563f8dd09f038a209063a27942e60cb27767564f4a06f6` |
| `LC6_OPERATIONAL_CONTROLS_CONTRACT_V1.md` | `77c922659aae9ce536c876c389cceac8748c1c530d53c539ed827a261b60f52c` |
| `PRODUCTION_UNCHANGED_REPORT.md` | `1d227f90e2335fedb57819524e430ad4ae24250167b9f0e911db33b7152aa89a` |
| `README.md` | `6a713aff98d5f108d140c66a7f7cefbe6465940ed3737ca92b2dc11917c06111` |
| `RUNBOOK.md` | `5be68f882eb6bf69b6c10df5a4047b238109afa96d083f086f1df66a42f5528a` |
| `VALIDATION_REPORT.md` | `ed091e0add56652ab233707afdf52bd1c967c86bbfcace7efc5b9a187aa8aca3` |
| `lc6_operational_controls.py` | `86d22875c49a58e0801838e09585c98217823c01cb4058e6252cd3bf45681501` |
| `test_lc6_operational_controls.py` | `94108115fe474ba0b702f1660c72674979f685bcd5afe088f467b283762a7d9b` |

## Boundary confirmation

Not performed:
- Batch B1 prerequisite workflow rerun;
- Batch B2;
- Chroma open on production or certified baseline;
- sealed executor invocation;
- real candidate creation;
- production lock acquisition;
- production selection change;
- sealed package, certified baseline, production, or source-document modification;
- seal, commit, push, or promotion.

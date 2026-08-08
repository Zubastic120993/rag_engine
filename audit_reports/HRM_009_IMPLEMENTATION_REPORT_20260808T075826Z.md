# HRM_009 implementation report — 20260808T075826Z

## Status
- Task: `HERMES_SESSION_E_PROPOSAL2_DOCUMENT_TYPE_AND_AUTHORITY_FAMILY_RANKING`
- Scope executed: `SPEC_009` Proposal 2 only
- Branch: `main`
- Baseline HEAD before change: `8ac6775c6d39add8f6eaaf14492ca898ff07b1c1`
- Production benchmark: not run
- Production replay: not run
- Session A / Session B rerun: not run
- Production database / `embedded.json` / `ask_events.jsonl` / tracker: unchanged
- Retrieval floor: unchanged

## Motivation
Session C and Session D established:
- Proposal 1 is governance-compliant after HRM_008 but **NOT_EFFECTIVE** on the preserved failed-P0 replay.
- Remaining retrieval failures include service letters, training material, and reference/unrelated documentation outranking or surviving alongside maker-manual sources where Proposal 2 is intended to help.

Proposal 2 therefore introduces explicit document-type metadata and authority-family metadata for ranking, without changing:
- `authority_rank`
- `canonical_authority_rank`
- OCR semantics from `DECISIONS.md` Decision 2
- score-floor behaviour

## Implementation summary
### 1. Added document-type metadata
New metadata derived from source path:
- `document_type`
- `document_type_rank`

Current document-type classes:
- `operation_manual`
- `spare_parts_catalogue`
- `maker_manual`
- `service_letter`
- `training`
- `reference`
- `drawing_set`
- `note`

Ranking intent:
- maker-manual classes ahead of service letters
- service letters ahead of training
- training ahead of generic reference/drawing/note material

### 2. Added authority-family metadata
New metadata derived from source path:
- `authority_family`

This preserves the equipment/manual family identity in ranking metadata without changing any existing governance fields.

### 3. Updated retrieval ranking model
Before this change, ranking order was effectively:
1. distance band
2. `canonical_authority_rank`
3. `authority_rank`
4. distance
5. source path
6. page

After this change, ranking order is:
1. distance band
2. `canonical_authority_rank`
3. `document_type_rank`
4. authority-family support count within the gated candidate set
5. `authority_rank`
6. distance
7. source path
8. page

This keeps Proposal 2 separate from Proposal 1 and preserves Decision 2 text-native preference because `authority_rank` still breaks ties between native and OCR variants of the same document.

## Files changed
- `rag_engine/authority.py`
- `rag_engine/query.py`
- `tests/test_retrieval_authority.py`
- `audit_reports/HRM_009_IMPLEMENTATION_REPORT_20260808T075826Z.md`

## Governance compliance
Confirmed preserved:
- `authority_rank` semantics unchanged from HRM_008 governance remediation
- `canonical_authority_rank` semantics unchanged
- OCR-derived chunks remain `authority_rank = 7`
- OCR-derived chunks remain `machine_transcribed`
- `raw_source` remains preserved
- citations remain normalized to canonical original PDF path
- text-native preference remains enforced by retrieval ranking for same-document OCR/native pairs

Proposal 2 metadata is additive and separate:
- `document_type`
- `document_type_rank`
- `authority_family`

## Tests
### Focused
- `./run_tests.sh tests/test_retrieval_authority.py`
- result: `12 passed`

### Full unit suite
- `./run_tests.sh`
- result: `121 passed in 8.82s`

### Additional checks
- `./venv/bin/python -m compileall rag_engine tests` → passed
- `git diff --check` → clean

## New test coverage
Added/updated unit tests for:
- maker manual vs service letter
- maker manual vs training
- maker manual vs reference
- same-family support preference within same document type
- negative regression: document-type ranking does not override a much closer hit
- preserved OCR/raw/canonical/text-native behaviour from HRM_008

## Limitations
1. No benchmark was run.
2. No production replay was run.
3. No effectiveness claim is made in this report.
4. Proposal 3, Proposal 4, Proposal 5, score-floor changes, chunking changes, embedding changes, migration work, Metadata Registry work, and Stable-ID work were not implemented.
5. This implementation improves ranking metadata and ordering only; it does not address collection-selection, figure-text recovery, or benchmark calibration gates.

## Classification
`IMPLEMENTATION_READY_TO_COMMIT`

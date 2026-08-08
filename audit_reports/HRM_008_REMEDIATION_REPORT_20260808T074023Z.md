# HRM_008 remediation report — 20260808T074023Z

## Status
- Task: `HRM_008`
- Branch: `main`
- Starting HEAD verified: `317356d3882a97d70bf83154a19cbe6a47e1451b`
- Goal: remediate HRM_007 to restore compliance with `DECISIONS.md` Decision 2 while preserving valid Proposal 1 work
- Production benchmark: not run
- Retrieval floor: unchanged
- Production database / tracker / `embedded.json` / `ask_events.jsonl`: unchanged

## Governance issue
ORCH_064 identified that HRM_007 violated Decision 2 by redefining OCR-derived authority behaviour inside `authority_rank`.

Specifically, HRM_007:
1. changed OCR-derived chunks from `authority_rank = 7` to inherited canonical authority rank;
2. removed the retrieval-path penalty that preserved text-native preference for the same document.

That conflicted with the governing requirement in `DECISIONS.md` Decision 2 and `SPEC_006`:
- OCR-derived chunks remain `authority_rank = 7`;
- OCR-derived chunks remain `machine_transcribed`;
- raw OCR provenance must be retained;
- citations must continue to name the canonical original PDF and page;
- text-native versions must remain preferred over OCR versions for the same document.

## Remediation
### 1. Restored Decision 2 authority rank semantics
- `authority_rank_for_source()` now again returns `7` for `_OCR.pdf` sources.
- OCR-derived chunks do **not** inherit the maker/manual/company/regulatory rank of the canonical original.

### 2. Preserved valid Proposal 1 work through a separate ranking signal
- Added `canonical_authority_rank` metadata derived from the canonical source class.
- Ranking now uses:
  1. distance band
  2. `canonical_authority_rank`
  3. `authority_rank`
  4. distance
  5. canonical source path
  6. page
- This keeps canonical document class distinguishable from OCR provenance **without** redefining `authority_rank`.

### 3. Restored text-native preference
- Because text-native and OCR versions of the same document share the same `canonical_authority_rank`, the lower `authority_rank` of the native version (`3/4/5/etc.`) now beats OCR rank `7`.
- This enforces Decision 2 in the retrieval path.

### 4. Preserved provenance and citation normalization
- `source` remains canonicalized to the original PDF path for citation.
- `raw_source` is retained for OCR-derived chunks.
- `machine_transcribed` remains preserved in metadata and returned source details.
- repeated metadata enrichment still preserves the original OCR provenance.

## Before / after behaviour
### Before remediation (HRM_007)
- OCR-derived chunk could surface with inherited canonical maker-manual `authority_rank = 3`
- canonical-vs-OCR distinction was embedded into `authority_rank`, violating Decision 2
- text-native preference for the same document was no longer enforced by rank semantics

### After remediation (HRM_008)
- OCR-derived chunk remains `authority_rank = 7`
- OCR-derived chunk also carries `canonical_authority_rank` for ranking-only use
- canonical citation still points to original PDF
- `raw_source` still points to `_OCR.pdf`
- text-native version is preferred over OCR version when both exist for the same document

## Preserved-evidence behaviour check
Read-only replay against preserved Session A retained candidates was used again to confirm the narrow effect of the remediation.

Compared with the pre-HRM_007 baseline ordering, the remediated ranking changed only:
- `GQ-050`

Result:
- before: `00_Career/03_Engine_Knowledge/Training/MAN_Academy/Aditional/Vol 2+3 K98ME.pdf`
- after: `00_Career/03_Engine_Knowledge/Yanmar_6EY22/Yanmar_6EY22ALW_Spare_Parts_List_YZJ2021-1391_Eng_03895-03898.pdf`
- expected: `00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf`

This confirms the remediation did not broaden into Proposal 2, collection work, chunking work, or benchmark work.

## Files changed
- `rag_engine/authority.py`
- `rag_engine/query.py`
- `tests/test_retrieval_authority.py`
- `audit_reports/HRM_008_REMEDIATION_REPORT_20260808T074023Z.md`

## Verification
### Focused
- `./run_tests.sh tests/test_retrieval_authority.py`
- result: `8 passed`

### Full unit suite
- `./run_tests.sh`
- result: `117 passed in 8.93s`

### Additional checks
- `./venv/bin/python -m compileall rag_engine tests` → passed
- `git diff --check` → clean

## Test coverage added/updated
The updated tests now verify:
- OCR chunks remain `authority_rank = 7`
- canonical citation path remains the original PDF
- `raw_source` is retained for OCR-derived chunks
- `machine_transcribed` remains preserved
- text-native version is preferred over OCR for the same document
- the new `canonical_authority_rank` signal drives ranking without redefining `authority_rank`

## Remaining limitations
1. This remediation does **not** resolve the broader retrieval issues identified in Session C.
2. No Proposal 2 work was implemented.
3. No collection-selection, metadata redesign, chunking, OCR-content recovery, or benchmark work was performed.
4. Read-only preserved-evidence replay is not a production benchmark and was used only to confirm narrow ranking behaviour.

## Classification
`IMPLEMENTATION_READY_TO_COMMIT`

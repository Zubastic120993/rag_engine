# HRM_007 implementation report — 20260808T072957Z

## Status
- Task: `HRM_007`
- Scope executed: Proposal 1 only from `SPEC_009_RETRIEVAL_RANKING_IMPROVEMENT.md`
- Branch: `main`
- Baseline HEAD before change: `8c17adc73b3f7e74cf0ebbf4e95c8b0e9451b646`
- Production changes: none
- Benchmark rerun: none
- Retrieval floor change: none
- Database / tracker / `embedded.json` / `ask_events.jsonl` changes: none

## Files changed
- `rag_engine/authority.py`
- `rag_engine/query.py`
- `tests/test_retrieval_authority.py`
- `audit_reports/HRM_007_IMPLEMENTATION_REPORT_20260808T072957Z.md`

## Rationale
Session C identified one approved first-step implementation:

> Decouple canonical authority from OCR penalty.

The pre-change behaviour penalized OCR-derived chunks twice:

1. `authority_rank_for_source()` assigned `_OCR.pdf` sources `RANK_MACHINE=7`, even when the canonical document was a maker manual.
2. `_candidate_sort_key()` further penalized `machine_transcribed=True` during ranking.

This meant OCR provenance could directly lower ranking even when canonical authority should have been evaluated as the original authority document.

## Implementation summary
1. **Authority rank is now derived from canonical authority path, not raw OCR provenance.**
   - OCR provenance remains preserved through `raw_source` and `machine_transcribed`.
   - Canonical source class now determines `authority_rank`.
2. **Repeated metadata enrichment now preserves OCR provenance.**
   - Existing `raw_source` is retained instead of being lost on a second enrichment pass.
3. **Ranking no longer penalizes candidates merely for `machine_transcribed=True`.**
   - OCR provenance remains visible in metadata and source reporting, but it is no longer a ranking penalty.

## Before/after retrieval examples
Focused verification was performed by replaying the preserved Session A retained-candidate evidence for the previously failed P0 set. This is a read-only replay over preserved evidence, not a live benchmark and not a production query.

### Example 1 — GQ-015
- Legacy replay top candidate:
  - `00_Career/03_Engine_Knowledge/Separator_AlfaLaval_S926/70_200006173_02_V1 instruction manual.pdf`
- New replay top candidate:
  - `00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf`
- Expected authority:
  - `00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22(A)LWS(H.F.O.／MET)_0AE22-EN0100 May.2023-10.pdf`
- Result:
  - improved away from unrelated non-OCR material, but still not resolved to expected authority

### Example 2 — GQ-050
- Legacy replay top candidate:
  - `00_Career/03_Engine_Knowledge/Training/MAN_Academy/Aditional/Vol 2+3 K98ME.pdf`
- New replay top candidate:
  - `00_Career/03_Engine_Knowledge/Yanmar_6EY22/Yanmar_6EY22ALW_Spare_Parts_List_YZJ2021-1391_Eng_03895-03898.pdf`
- Expected authority:
  - `00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf`
- Result:
  - ranking changed, but not toward the expected MAN authority source

## Affected P0 questions
Focused replay over the 16 previously failed P0 questions showed ranking-order changes for:
- `GQ-015`
- `GQ-050`

All other failed P0 preserved-candidate rankings remained unchanged under Proposal 1 alone.

This is consistent with the approved narrow scope: Proposal 1 removes OCR-derived ranking penalty only. It does **not** implement document-type ranking, collection selection fixes, family-aware reranking, or figure/OCR-content improvements.

## Tests
### Focused
- `./run_tests.sh tests/test_retrieval_authority.py`
- Result: `6 passed`

### Full unit suite
- `./run_tests.sh`
- Result: `115 passed in 8.46s`

### Additional checks
- `./venv/bin/python -m compileall rag_engine tests`
- Result: passed
- `git diff --check`
- Result: clean

## Unified diff summary
- `rag_engine/authority.py`: derive authority rank from canonical source and preserve raw OCR provenance across repeated enrichment
- `rag_engine/query.py`: remove machine-transcribed ranking penalty from candidate sort key
- `tests/test_retrieval_authority.py`: update OCR-authority expectations and add regression coverage for OCR manual preference within the authority band

## Limitations
1. Proposal 1 alone does **not** resolve all failed P0 questions.
2. No full benchmark was run, by instruction.
3. Focused verification used preserved Session A candidate evidence replay, not live production queries, in order to avoid modifying `ask_events.jsonl` or touching production state.
4. Remaining unresolved failures still require later proposals from `SPEC_009`, especially document-type/authority-family ranking, scope tightening, and figure-text recovery work.

## Production-safety confirmation
- No production database write performed
- No `embedded.json` change
- No `ask_events.jsonl` change
- No tracker change
- No retrieval floor change

## Classification
`IMPLEMENTATION_READY_TO_COMMIT`

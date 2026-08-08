# HRM_011 implementation report — 20260808T091417Z

## Status
- Task: `HRM_011 — Implement SPEC_009 Proposal 4 only`
- Scope executed: `SPEC_009` Proposal 4 only
- Branch: `main`
- Baseline HEAD at start: `df8af34761b22a30a796e8d1e690ac9724179c90`
- `origin/main` matched local `HEAD` at start
- Tracked changes at start: none
- Staged changes at start: none
- Pre-existing untracked files: left untouched

## Motivation
Proposal 3 removed wrong-material contamination but left parallel-manual failures where retrieval already reached the correct equipment family and still selected the wrong sibling manual.

Primary target examples from preserved replay evidence:
- `GQ-015`
- `GQ-021`
- `GQ-031`
- `GQ-032`

The common pattern is near-tied Yanmar-family candidates where exact-source repetition inside the same family is a better coherence signal than raw document-type preference alone.

## Implementation summary
Implemented the smallest Proposal 4 change in the retrieval ranking layer only:

1. preserved Proposal 2 metadata semantics and Proposal 3 scope filtering unchanged;
2. added exact-source support counting across gated candidates;
3. inserted source-coherence reranking ahead of document-type preference;
4. kept distance band first, so materially closer hits still stay ahead across bands;
5. kept existing family-support, authority, floor, dedupe, and citation behaviour unchanged.

## Files changed
- `rag_engine/query.py`
- `tests/test_retrieval_authority.py`

## Diff summary
### `rag_engine/query.py`
- extended `_candidate_sort_key()` to accept `source_support`
- added `source_coherence` tie-break from exact canonical source repetition count
- updated `_apply_retrieval_controls()` to count exact-source support across gated candidates
- ranking order is now:
  1. distance band
  2. `canonical_authority_rank`
  3. exact-source support count
  4. `document_type_rank`
  5. authority-family support count
  6. `authority_rank`
  7. distance
  8. source path
  9. page

### `tests/test_retrieval_authority.py`
Added focused Proposal 4 coverage:
- better-supported parallel manual inside the same family can outrank a sibling manual in the same distance band;
- materially closer parallel manual still stays ahead across different bands;
- existing Proposal 2/3 ranking and scope tests remain intact.

## Focused preserved-evidence spot check
Read-only preserved-candidate replay against the four Proposal 4 target examples showed:

- `GQ-015`
  - before Proposal 4 winner: `OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf`
  - after Proposal 4 winner: `OPERATION MANUAL_6EY22(A)LWS(H.F.O.／MET)_0AE22-EN0100 May.2023-10.pdf`
  - expected authority: `OPERATION MANUAL_6EY22(A)LWS(H.F.O.／MET)_0AE22-EN0100 May.2023-10.pdf`
  - result: corrected

- `GQ-021`
  - before Proposal 4 winner: `SCR/0ASCR-EN0054 Sep.2024-0.pdf`
  - after Proposal 4 winner: `SCR/0ASCR-EN0054 Sep.2024-0.pdf`
  - expected authority: `OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf`
  - result: unchanged

- `GQ-031`
  - before Proposal 4 winner: `OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf`
  - after Proposal 4 winner: `SCR/0ASCR-EN0054 Sep.2024-0.pdf`
  - expected authority: `SCR/0ASCR-EN0054 Sep.2024-0.pdf`
  - result: corrected

- `GQ-032`
  - before Proposal 4 winner: `OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf`
  - after Proposal 4 winner: `OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf`
  - expected authority: `SCR/0ASCR-EN0054 Sep.2024-0.pdf`
  - result: unchanged

Observed effect:
- `2/4` target sibling-manual examples corrected in preserved-candidate spot check;
- no cross-band override was introduced for materially closer sibling hits.

## Governance confirmation
Confirmed unchanged:
- retrieval floor
- embeddings
- chunking
- Metadata Registry
- Stable IDs
- Proposal 5
- benchmark logic
- Session replay logic
- Proposal 2 metadata fields:
  - `authority_rank`
  - `canonical_authority_rank`
  - `document_type`
  - `document_type_rank`
  - `authority_family`
  - `machine_transcribed`
  - `raw_source`
- Proposal 3 scope-selection behaviour

Proposal 4 changed only retrieval reranking within the existing gated candidate set.

## Verification
### Focused suite
Command:
- `./run_tests.sh tests/test_retrieval_authority.py`

Result:
- `19 passed in 0.94s`

### Full suite
Command:
- `./run_tests.sh`

Result:
- `128 passed in 8.77s`

### Compile check
Command:
- `./venv/bin/python -m compileall rag_engine tests`

Result:
- passed

### Diff check
Command:
- `git diff --check`

Result:
- clean

## Remaining limitations
- Proposal 4 does not change retrieval floor or candidate recall.
- Proposal 4 does not solve cross-band sibling failures where the expected sibling remains materially farther, such as the preserved `GQ-021` and `GQ-032` examples.
- Proposal 4 does not address figure/OCR authority absence failures.
- No benchmark or full replay measurement was run in this implementation session.

## Classification
`IMPLEMENTATION_COMMITTED`

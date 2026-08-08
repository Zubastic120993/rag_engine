# HRM_010 implementation report — 20260808T084756Z

## Status
- Task: `HERMES_SESSION_G_PROPOSAL3_MAKER_MANUAL_SCOPE_REFINEMENT`
- Scope executed: `SPEC_009` Proposal 3 only
- Branch: `main`
- Baseline HEAD before change: `0cdfe137cf3c5bd3cba2870e60f94d878017b9c0`
- Production benchmark: not run
- Production replay: not run
- Session A / Session B rerun: not run
- Production database / `embedded.json` / `ask_events.jsonl` / tracker: unchanged
- Retrieval floor: unchanged

## Motivation
Session C identified direct maker-manual scope contamination, especially `GQ-022`, where `maker-manuals` admitted `00_Career/07_SDS_Datasheets/` material into an equipment-manual question.

Session F showed Proposal 2 was only **PARTIALLY_EFFECTIVE**. Proposal 3 is therefore limited to scope-selection refinement so unrelated families are no longer treated as equivalent candidates when the requested scope is `maker-manuals`.

## Implementation summary
### 1. Added retrieval-time scope constraints for `maker-manuals`
The collection definition remains unchanged for ingest/classification history, but retrieval now applies an additional scope-selection filter for `maker-manuals` candidates.

Configured in `rag_engine/scopes.yaml`:
- `retrieval_allowed_path_prefixes`
- `retrieval_excluded_document_types`

For `maker-manuals`, retrieval now:
- allows only `00_Career/03_Engine_Knowledge/` paths at query time
- excludes these document types:
  - `service_letter`
  - `training`
  - `reference`
  - `note`

### 2. Added a dedicated scope-selection helper
`rag_engine/scope_rules.py` now provides:
- `scope_allows_candidate(scope, metadata)`

This is query-time selection only. It does not alter:
- chunk metadata
- collection assignment already stored in production
- ranking metadata semantics

### 3. Applied scope filtering before ranking/gating
`rag_engine/query.py` now filters raw retrieval candidates through the scope-selection helper before:
- score-floor gating
- family-support counting
- ranking
- dedupe

This keeps Proposal 3 limited to the scope-selection layer.

## Files changed
- `rag_engine/scopes.yaml`
- `rag_engine/scope_rules.py`
- `rag_engine/query.py`
- `tests/test_retrieval_authority.py`
- `audit_reports/HRM_010_IMPLEMENTATION_REPORT_20260808T084756Z.md`

## Scope-selection model
### Before Proposal 3
For `maker-manuals`, query-time retrieval effectively accepted any indexed chunk already labeled with collection `maker-manuals`, including:
- SDS/manual content under `00_Career/07_SDS_Datasheets/`
- training material
- service-letter literature
- reference-style material inside the broad maker-manual corpus

### After Proposal 3
For `maker-manuals`, query-time retrieval now requires:
1. candidate source path starts with `00_Career/03_Engine_Knowledge/`
2. candidate `document_type` is not one of the excluded out-of-scope classes

Only after this scope-selection pass does normal Proposal 2 ranking proceed.

## Governance compliance
Confirmed unchanged:
- `authority_rank`
- `canonical_authority_rank`
- `document_type`
- `document_type_rank`
- `authority_family`
- `machine_transcribed`
- `raw_source`
- canonical citation normalization
- text-native preference behavior
- score-floor behavior

Proposal 3 changed only the scope-selection layer.

## Tests
### Focused
- `./run_tests.sh tests/test_retrieval_authority.py`
- result: `17 passed`

### Full unit suite
- `./run_tests.sh`
- result: `126 passed in 7.60s`

### Additional checks
- `./venv/bin/python -m compileall rag_engine tests` → passed
- `git diff --check` → clean

## New test coverage
Added query-time scope-selection tests for:
- maker-manual scope excludes service literature
- maker-manual scope excludes training
- maker-manual scope excludes SDS/reference content
- maker-manual scope excludes unrelated reference-family material
- negative regression: unscoped retrieval still keeps those candidates

## Limitations
1. No benchmark was run.
2. No effectiveness measurement was run.
3. No preserved-P0 replay was run.
4. Proposal 4, Proposal 5, score-floor changes, chunking, embedding changes, migration work, Metadata Registry work, Stable-ID work, and production indexing were not implemented.
5. This change removes known out-of-scope candidate classes from `maker-manuals`, but it does not solve same-family parallel-manual ambiguity or figure-text extraction weakness.

## Classification
`IMPLEMENTATION_READY_TO_COMMIT`

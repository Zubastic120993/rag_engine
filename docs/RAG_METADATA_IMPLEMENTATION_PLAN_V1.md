# RAG Metadata Implementation Plan v1

Revision date: 2026-07-31
Status: Approved implementation-phase plan

This plan is design-only. It does not authorize execution in Step 6.

## Phase 1 — Registry schema and read-only discovery
### Objective
Define and prepare the SQLite registry boundary and schema versioning without changing Chroma or production CE_Library records.

### Files likely affected
- future schema files and migration templates
- registry configuration docs
- validation/test specs

### Database changes
- schema definition only in isolated development work
- no production registry creation in Step 6

### Re-indexing requirement
No

### Backup requirement
Normal design-document backups only; no production RAG change

### Tests
- schema validation
- naming and ID format validation
- vocabulary consistency review

### Rollback
Discard design artifacts or revert repo changes

### Acceptance criteria
- schema approved
- stable-ID spec approved
- vocabulary spec approved
- human-review policy approved

### Approval gate
Chief Engineer / design authority approval of schema boundary

## Phase 2 — Registry population from existing evidence
### Objective
Populate the registry using deterministic existing evidence while preserving inference status.

### Files likely affected
- future population scripts
- mapping rules
- review queue logic

### Database changes
- registry rows created
- no Chroma change

### Re-indexing requirement
No embedding rebuild

### Backup requirement
Registry rollback point before first production population

### Tests
- deterministic import repeatability
- provenance completeness
- review-required routing
- vessel and authority ambiguity capture

### Rollback
Restore registry snapshot or recreate registry from clean schema

### Acceptance criteria
- deterministic population works
- ambiguous fields preserved as ambiguous/review_required
- no production Chroma mutation

### Approval gate
Approval of deterministic import rules and review queue handling

## Phase 3 — Retrieval joins and registry-aware filters
### Objective
Allow retrieval workflows to consult registry metadata for vessel and authority filters without changing chunk identity if avoidable.

### Files likely affected
- query and lookup logic
- diagnostics
- validation routines

### Database changes
- read/query use of registry
- possible supporting indexes

### Re-indexing requirement
No, if Chroma fields can remain unchanged initially

### Backup requirement
Registry backup before production query-path deployment

### Tests
- vessel separation
- authority-aware filtering
- no cross-vessel leakage
- citation preservation

### Rollback
Disable registry-aware lookup path and revert code/config changes

### Acceptance criteria
- registry lookups safely constrain retrieval output
- citations still resolve correctly
- no Chroma rebuild required in this phase

### Approval gate
Approval of query-path integration design

## Phase 4 — Deterministic chunk IDs and governed Chroma metadata
### Objective
Introduce governed chunk metadata and deterministic chunk IDs through a controlled re-indexing phase.

### Files likely affected
- ingest logic
- chunk-ID generation logic
- Chroma metadata mapping
- validation harnesses

### Database changes
- Chroma metadata change
- possible registry update of chunk mappings

### Re-indexing requirement
Yes

### Backup requirement
Verified backup and restore-tested backup required before execution

### Tests
- deterministic chunk ID generation
- file-move stability
- registry/Chroma consistency
- rollback rehearsal

### Rollback
Restore verified backup and revert ingestion changes if validation fails

### Acceptance criteria
- deterministic chunk IDs proven
- compact governed Chroma metadata consistent with registry
- restore-tested rollback remains available

### Approval gate
Explicit approval required before any re-indexing or Chroma rebuild

## Phase 5 — Obsidian relationship projection
### Objective
Project approved registry metadata into derived Obsidian structures without circular ingestion.

### Files likely affected
- note generators
- frontmatter templates
- stale-link checks
- exclusion rules

### Database changes
- none required to core registry schema beyond projection bookkeeping

### Re-indexing requirement
No for registry projection itself

### Backup requirement
Backup generated note outputs if later approved

### Tests
- stable-ID link integrity
- circular-ingestion prevention
- stale-link detection
- approved/generated note separation

### Rollback
Remove derived projection outputs and revert projection tooling

### Acceptance criteria
- Obsidian remains derived only
- generated folders remain excluded from production ingest
- source links remain stable

### Approval gate
Separate approval required before automatic note generation

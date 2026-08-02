# RAG Metadata Registry Population Policy v1

Revision date: 2026-07-31
Status: Approved planning policy for Step 8A

## Purpose
Define the controlled production-population workflow for the SQLite metadata registry without authorizing any immediate production write.

## Core rule
Population must be:
- proposal-first;
- deterministic where evidence is deterministic;
- review-gated where policy requires human approval;
- reversible at the approved batch boundary;
- isolated from Chroma mutation.

## Source-discovery boundary
Population must not perform broad uncontrolled discovery.

Approved source-discovery boundary:
- only approved scope roots already defined by governed configuration;
- or an explicitly provided manifest of source files;
- or an explicitly approved single subtree under one governed source root.

Not approved:
- scanning the whole home directory;
- scanning arbitrary mounted volumes;
- heuristic cross-library filesystem discovery;
- importing generated Obsidian outputs;
- importing project-source files as registry content.

## Deterministic population fields
The following fields may be populated automatically when the evidence is direct and unambiguous:
- `source_hash`
- `relative_path`
- `filename`
- `file_extension`
- `file_size_bytes`
- `first_seen_at`
- `last_seen_at`
- `ingestion_timestamp`
- `ingestion_version`
- `extractor`
- `extractor_version`
- `page_number` when produced directly by the approved extraction pipeline
- `scope` when the configured scope root mapping is unambiguous and not contradicted
- `document_version_id` when derived deterministically from approved stable-ID rules and deterministic version inputs
- `source_file_id` when derived from approved stable-ID rules

These fields may be auto-accepted only as deterministic extraction/provenance output, not as human approval.

## Fields requiring human review
Human review remains mandatory for the approved sensitive fields, including:
- vessel applicability
- sister-vessel applicability
- `authority_class`
- `approval_status`
- supersession relationships
- ambiguous revision
- ambiguous document type
- confidentiality classification for correspondence/defect/requisition/photo related relationships
- operational-record relationships
- any `scope` assignment contradicted by reviewed evidence
- any value with conflicting evidence

## Ambiguous and conflicting handling
When automatic population finds multiple plausible values:
- write proposal/assertion records as `ambiguous` or `conflicting`;
- preserve evidence provenance;
- do not auto-promote to governed approved metadata.

Silent overwrite is not approved.

## Dry-run and proposal-first workflow
Before any production write, population must support a dry-run mode.

Dry-run outputs must contain:
- source manifest or approved source-root reference;
- planned inserts/updates by table;
- deterministic values;
- review-required values;
- ambiguous/conflicting cases;
- duplicate candidates;
- move-path candidates;
- counts by document/scope/review state.

Dry-run must not write production registry rows.

Dry-run evidence should be written to an isolated artifact path, not into `.rag_db`.

## Population execution workflow
Approved future production workflow:
1. validate configured production roots;
2. verify no ingest/re-index/migration active;
3. verify coordinated backup boundary exists and is current;
4. run dry-run and produce proposal evidence;
5. obtain operator approval of the proposal;
6. open a single approved population batch;
7. insert deterministic rows and review-required rows with correct status;
8. run registry validation;
9. record batch receipt and hashes;
10. stop before any Chroma change.

## Population batch size
Initial approved operational limit:
- one approved manifest or one approved scope-subtree batch at a time;
- recommended maximum `200` source files per production batch until production behavior is proven;
- no concurrent production population batches.

This limit may be revised later after validated production experience.

## Rollback boundary
Rollback boundary is per approved production batch.

Each production batch must have:
- a pre-batch coordinated backup;
- one transaction boundary where feasible;
- one batch receipt/log;
- one validation result.

If validation fails:
- abort the batch;
- roll back the open transaction if still active;
- if a committed but invalid state exists, restore from the pre-batch approved snapshot.

## Duplicate handling
Duplicate handling must be non-destructive by default.

Rules:
- same `source_hash` across multiple paths becomes a duplicate candidate, not an auto-delete;
- registry may preserve multiple `source_files` observations for the same content hash/path history;
- logical `document_id` must not be regenerated from path alone;
- duplicate family resolution that affects governed identity requires review when business meaning is not certain.

## File-move handling
File moves must not create a new logical document identity automatically.

Rules:
- path changes update physical-file observation/history;
- source identity follows hash/stable rules, not path-only identity;
- if moved file content is unchanged, document identity should remain stable;
- if move coincides with revision/approval ambiguity, route to review.

## Human-review gates
Mandatory production gates:
1. proposal approval before write
2. review-required queue generated, not bypassed
3. no bulk auto-approval for restricted fields
4. correction/revocation must append history, not erase it
5. review records must preserve role, time, status, and affected record/assertion

## Registry/Chroma consistency boundary during population
Step 8A population policy does not authorize Chroma writes.

Population must therefore:
- update SQLite only;
- leave Chroma untouched;
- record any future Chroma-copy fields as deferred;
- produce consistency candidates for later validation, not execute Chroma migration.

## Integrity checks after each batch
Required post-batch checks:
- SQLite integrity check = `ok`
- foreign-key check clean
- schema version correct
- controlled vocabulary counts correct
- expected inserted/updated row counts match proposal receipt
- review-required counts match dry-run proposal
- no unexpected production-path writes outside approved registry root

## Deferred items
This policy explicitly defers:
- Chroma metadata copying
- chunk-ID rollout
- re-indexing
- retrieval code changes
- Obsidian projection
- review UI/workbench implementation

## Design restriction
This Step approves planning policy only. It does not approve immediate production population.

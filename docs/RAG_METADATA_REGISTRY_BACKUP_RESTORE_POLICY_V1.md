# RAG Metadata Registry Backup Restore Policy v1

Revision date: 2026-07-31
Status: Approved planning policy for Step 8A

## Purpose
Define the backup boundary, backup frequency, restore procedure, and integrity-validation requirements for the future production SQLite metadata registry.

## Core rule
A production registry backup is not accepted until a restore validation succeeds in an isolated environment.

## Backup boundary
The future governed backup boundary must protect:
1. the production Chroma/RAG runtime state under `.rag_db`
2. the production registry state under `.rag_state/metadata_registry`
3. the manifests and validation receipts required to prove the two stores are consistent

The registry must not rely on `.rag_db` backup alone once production registry use begins.

## Coordinated backup package structure
Approved logical package structure:
- `data/rag_db/...`
- `data/metadata_registry/...`
- `manifests/...`
- `hashes/...`
- `logs/...`
- `validation/...`

Minimum protected registry contents:
- `metadata_registry_v1.sqlite3`
- any approved sidecar manifest/receipt files in the registry directory
- package-level hash manifest

## Backup frequency
### Event-driven mandatory backups
A coordinated backup is mandatory before:
- first production registry creation
- first production population batch
- any schema migration
- any retrieval integration deployment that depends on the registry
- any Chroma metadata migration
- any re-indexing
- any destructive cleanup affecting registry-linked records

### Routine backups
Initial approved routine policy:
- daily registry snapshot
- weekly coordinated `.rag_db` + registry verified backup
- quarterly restore test
- additional backup immediately after any approved population batch that materially changes production registry state

This frequency may be tightened or relaxed only after operational review.

## Immutability
After successful verification:
- backup package becomes read-only;
- manifests and validation receipts are preserved;
- backup packages must not be overwritten;
- deletion requires explicit maintenance approval.

## Required manifest contents
Each coordinated package must record:
- backup ID
- creation UTC
- source library root
- source `.rag_db` path
- source registry path
- backup paths
- file counts and directory counts
- total sizes
- per-file SHA-256
- SQLite integrity result for registry copy
- SQLite integrity result for Chroma SQLite where applicable
- visible collection names and chunk counts for `.rag_db`
- registry schema version
- registry table counts
- controlled vocabulary row count
- consistency-check result between registry and `.rag_db`
- known limitations
- command log and validation record

## Restore procedure
Approved restore approach:
1. create an isolated restore root
2. restore `.rag_db` copy into isolated path
3. restore registry copy into isolated path
4. validate hashes, counts, and sizes against manifests
5. validate both stores in read-only mode
6. run registry integrity/foreign-key/schema/version checks
7. run `.rag_db` visibility/chunk-count checks
8. run registry/Chroma consistency checks
9. confirm production remained unchanged

## Registry integrity validation
Minimum restore validation for the registry:
- `PRAGMA integrity_check` = `ok`
- `PRAGMA foreign_key_check` empty
- schema version = expected version
- required tables present
- required indexes present
- controlled vocabulary count = expected count
- restore copy readable in read-only mode

## Registry/Chroma consistency validation
Once production population exists, restore validation must compare:
- registry source hashes vs Chroma evidence references where applicable
- registry scope expectations vs current Chroma collection/scope evidence
- document-version counts vs mapped retrieval evidence receipts where approved
- deferred mismatch list for fields not yet copied into Chroma

SQLite remains authoritative if mismatch is found.

## Current limitation carried forward
Until Chroma migration is separately approved, consistency validation is limited by the current Chroma boundary:
- Chroma authoritative-now fields remain retrieval/state only;
- registry is authoritative for governed metadata;
- mismatch detection must not trigger automatic repair.

## Abort criteria
Abort backup/restore work if any of the following occurs:
- active production writes detected
- restore path resolves to production
- hash mismatch
- registry integrity check failure
- `.rag_db` restore validation failure
- unexpected production file change
- unexplained registry/Chroma divergence

## Retention policy
Approved initial retention policy:
- retain latest 3 verified coordinated backups
- retain at least 1 monthly coordinated backup for 12 months
- never delete the last restore-tested coordinated backup
- retain one backup before every schema change, population rollout, retrieval integration change, Chroma migration, or re-indexing event

## Design restriction
This Step approves the future backup/restore policy only. It does not create a new production backup package by itself.

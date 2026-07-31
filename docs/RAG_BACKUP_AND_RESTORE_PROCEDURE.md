# RAG Backup and Restore Procedure

## 1. Purpose
Define the controlled method to back up, verify, restore, and validate the production RAG database for `rag_engine`.

**Rule:** A backup does not exist until a restore has succeeded.

## 2. Scope
This procedure applies to the production RAG data store used by the CE Assistant project at:
- Production RAG root: `/Users/vladymyrzub/CE_Library/.rag_db`
- Project root: `/Users/vladymyrzub/CE_Library/Tools/rag_engine`

## 3. Definitions
- **Production RAG**: the live `.rag_db` directory used by the project.
- **Verified backup**: a backup package with manifest, hashes, SQLite validation, and readable Chroma collection.
- **Restore-tested backup**: a verified backup that has also been restored successfully into an isolated environment and passed retrieval validation.
- **Isolated restore**: a restore performed outside production, using explicit restore-root paths and a clean runtime environment.
- **Recovery validation**: integrity, metadata, and retrieval checks proving the restored copy is independently readable.

## 4. Responsibilities
- **Operator / maintainer**: perform backup, verification, restore drill, documentation, and register updates.
- **Reviewer / approver**: confirm evidence completeness before accepting backup or deleting any older backup.
- **Project owner**: ensure quarterly restore testing and annual disaster-recovery review are completed.

## 5. Production RAG components
The protected production RAG components are the top-level contents of `.rag_db`, including as applicable:
- `chroma.sqlite3`
- vector segment directory/directories
- `embedded.json`
- `index_fingerprint.json`
- `ask_events.jsonl`
- `intake_hash_index.json`
- related read-only manifest evidence when copied into a backup package

## 6. Backup boundary
The backup protects `.rag_db` only.

It protects the RAG database state and related local operational files contained inside `.rag_db`.

## 7. Excluded components
The backup does **not** independently protect:
- the CE_Library source-document corpus;
- project source code;
- `scopes.yaml` governance changes outside the backup package;
- virtual environment;
- installed models;
- non-RAG project logs or external caches.

## 8. Preconditions
Before backup or restore validation:
- confirm approved paths;
- confirm enough free space for backup or restore copy;
- confirm no production ingestion, re-indexing, or migration is in progress;
- confirm environment contamination risks are controlled;
- confirm no action will silently resolve to production paths during restore validation.

## 9. Active-write checks
Before backup and before restore comparison:
- check for SQLite WAL presence;
- check for SQLite SHM presence;
- check for `ingest.lock` presence;
- sample file size and modification time stability over a short interval;
- confirm no indexing or ingestion process is actively writing.

If active writes are detected, abort and record **BLOCKED**.

## 10. Backup creation procedure
1. Confirm production active-write checks pass.
2. Create a timestamped backup package under the approved backup location.
3. Copy only the production `.rag_db` contents into backup `data/`.
4. Preserve structure, filenames, hidden files, timestamps where practical, and vector segment files.
5. Record command log and environment record.
6. Produce manifest, inventory, and hash files.

## 11. Backup manifest requirements
Each backup package must contain at minimum:
- backup ID;
- creation UTC;
- source path;
- backup path;
- file count;
- directory count;
- total size;
- per-file SHA-256 hashes;
- package SHA-256 hashes;
- SQLite integrity result;
- visible collection name(s);
- chunk count;
- known limitations;
- command log;
- verification record.

## 12. Hash verification
Required checks:
- source hashes before vs after copy;
- source hashes vs backup hashes;
- backup package SHA256SUMS;
- relative path list match;
- file count match;
- directory count match;
- total size match.

Any unexplained mismatch is a failure.

## 13. SQLite validation
Run read-only validation against the copied SQLite database:
- `PRAGMA integrity_check` must return `ok`;
- required tables must be readable;
- relevant table counts must be recordable;
- known baseline anomalies must be documented but not repaired during backup/restore validation unless separately approved.

## 14. Chroma validation
Run read-only validation against the restored or backed-up persistent directory:
- collection must be visible;
- expected collection name must be `langchain`;
- chunk count must match the verified baseline;
- sample IDs, metadata, and documents must be readable;
- vector segment accessibility must be confirmed.

## 15. Backup immutability
After successful verification:
- the backup package must be made read-only;
- backup evidence files must be preserved;
- backup files must not be overwritten;
- deletion must occur only through explicitly approved maintenance action.

## 16. Restore preparation
Before restore drill:
- use only a verified backup package;
- select an approved isolated restore root;
- confirm sufficient free space with margin;
- prepare folders for logs, scripts, temp home, temp cache, temp config, results, and restored data;
- confirm no command will fall back to production `.rag_db`.

## 17. Isolated restore procedure
1. Create the approved restore root.
2. Create isolated subdirectories for restored DB, logs, scripts, temp home, temp cache, temp config, and results.
3. Copy only backup `data/` contents into the isolated restore DB path.
4. Do not copy from production during restore validation.
5. Compare backup and restored copy for path, count, size, and hash equality.
6. Validate restored SQLite and Chroma in read-only mode.
7. Run deterministic metadata sampling.
8. Run controlled retrieval comparison against production read-only evidence.
9. Confirm production remains unchanged.

## 18. Environment isolation
The currently approved isolated restore method uses:
- `env -i`
- explicit `HOME` under the restore root
- explicit `TMPDIR` under the restore root
- explicit `XDG_CACHE_HOME` under the restore root
- explicit `XDG_CONFIG_HOME` under the restore root
- explicit empty `PYTHONPATH`
- explicit restore-root path opening only

## 19. PYTHONPATH handling
For every isolated Python/runtime command used in restore validation:
- inherited `PYTHONPATH` must be unset or explicitly empty;
- the resolved Python executable must be recorded;
- no inherited Hermes Python state may be relied on.

## 20. Current scopes.yaml limitation
Normal CLI restore isolation is not yet fully supported.

Current limitation:
- normal CLI-based isolation cannot yet be fully proven because `scopes.yaml` has no supported environment or CLI override for selecting an isolated database/config runtime path.

Approved current method:
- explicit restore-root paths and `env -i` using a dedicated isolated validation script.

## 21. Retrieval validation
Minimum restore validation must include controlled retrieval checks such as:
- exact identifier query;
- exact numeric query;
- maker/model query;
- vessel-specific query;
- service-letter query;
- page/citation query;
- no-answer query;
- ambiguous cross-vessel query;
- multi-term technical query;
- repeated query determinism check.

Acceptance expectation:
- identical result set for deterministic retrieval;
- or fully explained insignificant ordering difference;
- no missing records;
- no production writes.

## 22. Production non-modification checks
After restore validation:
- recalculate and compare production hashes where approved;
- compare file count, total size, and timestamps;
- confirm SQLite integrity remains unchanged;
- confirm tracker/document counts remain unchanged;
- confirm no new WAL/SHM leftovers were caused by the test;
- confirm no protected project files changed unexpectedly.

## 23. Recovery acceptance criteria
Recovery is accepted only if all apply:
- backup hashes validate;
- restored copy matches backup;
- restored SQLite integrity is `ok`;
- collection `langchain` is visible;
- expected chunk count is confirmed;
- metadata sampling matches;
- retrieval validation passes;
- no production path resolves as runtime database during restore validation;
- production remains unchanged.

## 24. Abort criteria
Abort immediately if any of the following occurs:
- production path resolves as the active database under test;
- active writes are detected in production;
- backup hash mismatch;
- restore hash mismatch;
- SQLite integrity is not `ok`;
- collection is missing;
- retrieval evidence diverges without explanation;
- production files change unexpectedly.

## 25. Cleanup procedure
After evidence capture:
- preserve reports, command log, retrieval comparison, and script hash;
- preserve register updates and corrective-action records;
- remove temporary restore only after reports are written and retention decision is confirmed;
- do not delete the temporary restore as part of a documentation-only phase unless separately approved.

## 26. Backup retention
Initial conservative retention policy:
- retain the latest 3 verified backups;
- retain at least 1 monthly backup for 12 months;
- retain a backup before every schema change, metadata migration, embedding-model change, Chroma upgrade, re-indexing operation, or major retrieval architecture change;
- never delete the last restore-tested backup;
- delete backups only through explicitly approved maintenance action.

This policy may be revised after storage-growth review.

## 27. Quarterly restore-test requirement
A restore test must be performed at least quarterly.

The quarterly restore must:
- use a verified backup;
- use the approved isolated method;
- confirm integrity, metadata sample match, and retrieval validation;
- update the backup register and roadmap evidence.

## 28. Annual full disaster-recovery review
At least annually, perform a full review covering:
- backup coverage adequacy;
- restore timing;
- documentation accuracy;
- retention policy suitability;
- known limitations and corrective actions;
- source-document corpus protection gap.

## 29. Backup register requirements
The backup register must record for each backup:
- backup ID;
- creation UTC;
- source path;
- backup path;
- size;
- file count;
- hash verification result;
- SQLite verification result;
- Chroma verification result;
- restore-tested status;
- restore date;
- restore classification;
- recovery validation time;
- retention status;
- remarks;
- evidence links.

## 30. Known limitations
- The backup protects `.rag_db` only.
- It does not independently protect the CE_Library source-document corpus.
- Normal CLI restore isolation is not yet fully supported.
- The approved isolated method currently uses explicit restore-root paths and `env -i`.
- One orphan SQLite embeddings row remains a documented baseline until separately investigated and safely remediated.

## 31. Evidence references
- Step 2 backup report: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_BACKUP_CREATION_20260728T112214Z.md`
- Step 3 restore report: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_RESTORE_VALIDATION_20260728T114008Z.md`
- Retrieval comparison: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_RETRIEVAL_COMPARISON_20260728T114008Z.json`
- Backup package: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/backups/rag_db_backup_20260728T112214Z`

## 32. Revision history
| Revision date | Version | Change |
|---|---:|---|
| 2026-07-28 | 1.0 | Initial controlled backup and isolated restore procedure issued after successful recovery validation. |

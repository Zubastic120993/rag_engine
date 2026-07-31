# RAG Backup Register

This register records verified and restore-tested backups for the `rag_engine` production RAG.

| Backup ID | Creation UTC | Source path | Backup path | Size | File count | Hash verification | SQLite verification | Chroma verification | Restore tested | Restore date | Restore classification | Recovery time | Retention status | Remarks | Evidence links |
|---|---|---|---|---:|---:|---|---|---|---|---|---|---:|---|---|---|
| `rag_db_backup_20260728T112214Z` | `2026-07-28T11:22:14Z` | `/Users/vladymyrzub/CE_Library/.rag_db` | `/Users/vladymyrzub/CE_Library/Tools/rag_engine/backups/rag_db_backup_20260728T112214Z` | `1071292889 bytes` | `46` | Source and backup hashes matched; package SHA256SUMS verified | `ok` | Collection `langchain` visible; `108685` chunks verified | yes | `20260728T114008Z` | `RESTORE PROVEN WITH RESTRICTIONS` | `32.904 s` | Keep; latest restore-tested backup. Do not delete without explicit approval. | Protects `.rag_db` only; source-document corpus excluded. Normal CLI isolation not yet supported; approved isolated method used explicit restore-root paths and `env -i`. | Step 2: `audit_reports/RAG_BACKUP_CREATION_20260728T112214Z.md`<br>Step 3: `audit_reports/RAG_RESTORE_VALIDATION_20260728T114008Z.md`<br>Retrieval: `audit_reports/RAG_RETRIEVAL_COMPARISON_20260728T114008Z.json` |

## Retention policy
- Retain the latest 3 verified backups.
- Retain at least 1 monthly backup for 12 months.
- Retain a backup before every schema change, metadata migration, embedding-model change, Chroma upgrade, re-indexing operation, or major retrieval architecture change.
- Perform a restore test quarterly.
- Perform a restore test before any major migration.
- Never delete the last restore-tested backup.
- Delete backups only through an explicitly approved maintenance action.

This policy may be revised after storage-growth review.

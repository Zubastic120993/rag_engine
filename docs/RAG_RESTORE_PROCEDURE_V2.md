# Restore Procedure V2

This document is an addition to, not a replacement for, `backups/rag_db_backup_20260728T112214Z/RESTORE_README.md`. That file's directory (`backups/rag_db_backup_20260728T112214Z/`) is read-only (`dr-xr-xr-x`) and was left untouched, unchanged, and un-chmod'd. This file reproduces its content verbatim below and appends the three closures VER_006 (AI_Orchestrator register, 2026-08-02) found missing. Original wording is unchanged; corrections are marked as such immediately after the section they correct.

## Original content, verbatim (backups/rag_db_backup_20260728T112214Z/RESTORE_README.md)

> # Restore README
>
> This document describes a future isolated restore only. Restore has not been executed in Step 2.
>
> ## Required free space
> - Minimum recommended for core DB-only restore validation: about `5 GiB` free
>
> ## Proposed isolated restore path
> - `/private/tmp/rag_engine_restore_test/`
>
> ## Unset inherited Python environment
> ```bash
> env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV ...
> ```
>
> ## Runtime isolation
> ```bash
> CE_LIBRARY_ROOT="/Users/vladymyrzub/CE_Library"
> RAG_DB_PATH="/private/tmp/rag_engine_restore_test/.rag_db"
> RAG_EMBED_MODEL="mxbai-embed-large"
> RAG_LLM_MODEL="qwen2.5:3b"
> OLLAMA_HOST="http://127.0.0.1:11434"
> ```
>
> ## Current scopes.yaml limitation
> - `scopes.yaml` has no env/CLI override in the current code.
>
> ## Commands that must not be run
> - no ingest
> - no sync
> - no re-indexing
> - no migrations
> - no eval commands that modify project files
>
> ## Validation sequence
> 1. verify production hashes unchanged
> 2. copy backup to isolated temp path
> 3. run SQLite `PRAGMA integrity_check` read-only
> 4. open Chroma store non-mutating
> 5. verify collection visibility and chunk count
> 6. run doctor with temp `RAG_DB_PATH` and unset inherited Python env
> 7. confirm production unchanged
>
> ## Abort conditions
> - any hash mismatch
> - any count mismatch
> - SQLite integrity failure
> - Chroma fails to open
> - command points back to production `.rag_db`
>
> ## Important note
> This backup protects the RAG database and associated state only. It does not independently back up the complete CE_Library source-document corpus.
>
> Restore-tested status: false

## CORRECTION 1 (DOC_001, 2026-08-02) — restore path

The original "Proposed isolated restore path" (`/private/tmp/rag_engine_restore_test/`) is corrected to a requirement, not a suggestion:

**The restore target MUST be a durable path outside `~/CE_Library` and outside `/tmp` and `/private/tmp`.** A path under `/tmp` or `/private/tmp` is volatile — it can be cleared on reboot or by OS temp-cleanup — which defeats the purpose of a restore-verification artifact: retention for later inspection. VER_006 used `~/rag_db_restore_verification_20260802/` for exactly this reason and left the restored copy in place afterward.

## CORRECTION 2 (DOC_001, 2026-08-02) — Validation sequence step 1, authoritative hash file

The original step 1 ("verify production hashes unchanged") does not say which hash file to check against. Corrected: three hash files exist in each backup directory — `SOURCE_HASHES_BEFORE.txt`, `SOURCE_HASHES_AFTER.txt`, and `BACKUP_HASHES.txt`. Per VER_006's finding, `SOURCE_HASHES_AFTER.txt` and `BACKUP_HASHES.txt` are byte-identical.

**The authoritative file for step 1 is `SOURCE_HASHES_AFTER.txt`, compared against the current contents of `~/CE_Library/.rag_db` (the file scope is the full recursive file listing of `.rag_db`, matching the paths recorded in that file).** Do not leave the choice to operator judgment.

## ADDITION 1 (DOC_001, 2026-08-02) — validation step 8: verify SHA256SUMS.txt against the restored copy

The original validation sequence never checks `SHA256SUMS.txt` against the restored copy. Add as step 8, after step 7 ("confirm production unchanged"):

8. Verify `SHA256SUMS.txt` against the restored copy: extract the `data/...` entries from `SHA256SUMS.txt` (46 entries as of the 2026-07-28 backup), compute SHA-256 of every corresponding file in the restored copy, and compare. Report matched / mismatched / missing counts. A mismatch or missing file is an abort condition, same as the original hash-mismatch abort condition.

## ADDITION 2 (DOC_001, 2026-08-02) — validation step 9: retrieval test

The original validation sequence is explicitly scoped as "DB-only restore validation" and never runs a functional query. Add as step 9:

9. Run one retrieval against the restored copy (e.g. `db.similarity_search_with_score(query, k=3)` using the repo's own `chroma_client_settings()`/`persist_dir()` construction, pointed at the restored copy) and confirm it returns results. **A store that opens with the correct chunk count but cannot answer a query has not been verified.** This step requires Ollama and the embedding model recorded in the backup's `SUMMARY.json` (`mxbai-embed-large` as of the 2026-07-28 backup) to be available. If Ollama or the model is unavailable, record that as a restore dependency, not a restore failure — do not treat it as an abort condition on its own.

## ADDITION 3 (DOC_001, 2026-08-02) — verification record

Verified 2026-08-02 by VER_006: 3m18s wall-clock duration, 46/46 hashes matched, 108,685 chunks (equal to production), production untouched (`.rag_db/chroma.sqlite3` mtime `2026-07-27 23:26:05` identical before and after). Restored copy retained at `~/rag_db_restore_verification_20260802/` for inspection.

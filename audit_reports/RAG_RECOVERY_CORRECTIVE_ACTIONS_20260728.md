# RAG Recovery Corrective Actions — 2026-07-28

## CA-001
**Title:** Add supported runtime override for scope/config/database path

- **Problem:** Normal CLI isolation cannot be fully proven because `scopes.yaml` has no environment or CLI override.
- **Evidence:**
  - `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_RESTORE_VALIDATION_20260728T114008Z.md`
  - `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_BACKUP_CREATION_20260728T112214Z.md`
- **Risk:** Restore and diagnostic workflows depend on a dedicated isolated script instead of a supported standard CLI path. Operational inconsistency remains if future maintainers assume normal CLI isolation is available.
- **Affected components:** CLI entrypoints, config path resolution, scope/config selection logic, doctor/scope-stats/retrieval/evaluation runtime path selection.
- **Proposed investigation:**
  1. Define supported override inputs for database path and config path.
  2. Confirm precedence rules between defaults, environment, and explicit CLI options.
  3. Extend diagnostics and retrieval tools to report resolved runtime paths clearly.
  4. Create tests proving doctor, scope-stats, retrieval, and evaluation can run against an explicitly selected isolated database.
- **Required outcome:** A supported, tested mechanism to run doctor, scope-stats, retrieval and evaluation against an explicitly selected isolated database and configuration.
- **Acceptance criteria:**
  - explicit override mechanism documented;
  - automated tests pass;
  - resolved runtime database/config path shown in diagnostics;
  - isolated CLI run can be proven without production fallback.
- **Re-indexing impact:** none expected if limited to runtime path/config selection.
- **Required backup:** required before implementation and before any release affecting path resolution.
- **Priority:** P1 / High
- **Status:** open
- **Implementation now:** not approved in this phase.

## CA-002
**Title:** Investigate orphan SQLite embeddings row

- **Known baseline:**
  - `segment_id`: `5df5781f-6ede-4906-9546-f9418c3fcfc5`
  - row count: `1`
- **Evidence:**
  - `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_BACKUP_CREATION_20260728T112214Z.md`
  - `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_RESTORE_VALIDATION_20260728T114008Z.md`
- **Risk:** Current evidence shows no integrity or retrieval failure, but undocumented orphan state may complicate future maintenance, migration, or cleanup actions.
- **Affected components:** `chroma.sqlite3`, maintenance workflow, diagnostics, future cleanup logic.
- **Proposed investigation:**
  1. Trace likely origin of the orphan row from prior maintenance activity or interrupted lifecycle events.
  2. Confirm whether Chroma/API-level reads ignore it safely.
  3. Determine whether any foreign-key or segment cleanup workflow should remove it.
  4. Define a safe remediation method and rollback path.
- **Required outcome:** Determine origin, impact and safe remediation method.
- **Acceptance criteria:**
  - origin assessed;
  - retrieval and integrity impact explicitly documented;
  - safe remediation plan approved before any data change;
  - rollback/backup requirement documented.
- **Re-indexing impact:** not required unless investigation proves broader index inconsistency.
- **Required backup:** required before any repair, cleanup, or schema-level remediation.
- **Priority:** P2 / Medium unless evidence shows retrieval or integrity impact.
- **Status:** open
- **Implementation now:** do not delete or repair in this phase.

# RAG Metadata Registry Location Policy v1

Revision date: 2026-07-31
Status: Approved planning policy for Step 8A

## Purpose
Define the approved persistent production location for the authoritative SQLite metadata registry, plus the portable path-safety policy for production, temporary, and restore use.

## Architecture rule
- SQLite registry is the authoritative governed metadata store.
- Chroma remains the embedding and chunk-retrieval store.
- The registry must not be created inside the project repository.
- The registry must not be created inside `.rag_db`.

## Approved persistent production location
Approved production location class:
- **separate governed state directory beside `.rag_db`**

Approved path pattern:
- `<LIBRARY_ROOT>/.rag_state/metadata_registry/metadata_registry_v1.sqlite3`

Where:
- `<LIBRARY_ROOT>` is the approved CE library root that already contains `.rag_db` as a child directory.

For the current deployment this resolves conceptually to a sibling of:
- `<LIBRARY_ROOT>/.rag_db`

but the policy is portable and must not hardcode a user-specific home path.

## Why this location is approved
### Not inside `.rag_db`
Rejected for production because:
- `.rag_db` is the Chroma/runtime state directory;
- registry and Chroma have different governance roles;
- separate backup/restore validation is clearer when the registry is outside the Chroma data root;
- future registry migrations should not appear as silent Chroma-state changes.

### Not inside the project source tree
Rejected because:
- repository state and governed operational state must remain separate;
- repo cleanup, checkout, or worktree changes must not affect production metadata state.

### Separate governed state directory
Approved because:
- it preserves a clear authority boundary;
- it supports coordinated but separable backup/restore;
- it avoids coupling registry lifecycle to source-code checkout state.

## Required production directory layout
Approved governed state root:
- `<LIBRARY_ROOT>/.rag_state/`

Approved registry subdirectory:
- `<LIBRARY_ROOT>/.rag_state/metadata_registry/`

Approved production DB filename:
- `metadata_registry_v1.sqlite3`

Optional future sidecar files in the same registry directory may include:
- schema/migration receipt files;
- snapshot manifests;
- validation receipts;
- lock files only if later explicitly approved.

## Portable approved-root policy
Implementation must derive roots from approved configuration, not from a hardcoded user path.

### Required configured roots
Future production creation must require explicit configured values for:
- `library_root`
- `rag_db_root`
- `registry_root`
- `project_root`

### Required root relationships
- `rag_db_root` must equal `<library_root>/.rag_db`
- `registry_root` must equal `<library_root>/.rag_state/metadata_registry`
- `registry_root` must not be inside `project_root`
- `registry_root` must not be inside `rag_db_root`
- `rag_db_root` and `registry_root` must share the same `library_root`

## Approved temporary roots
Temporary registry creation is approved only under resolved OS temporary roots.

Approved temporary-root classes:
- `/private/tmp`
- resolved `tempfile.gettempdir()` roots, including macOS `/private/var/...`

Requirements:
- the path must be absolute;
- the path must be resolved before policy checks;
- the resolved path must be under an approved temporary root;
- temporary roots are for isolated testing, validation, restore rehearsal, and dry-run artifacts only.

## Production-root creation policy
Production creation is not approved by filename alone.

Production registry creation requires:
1. explicit `--db` path;
2. resolved absolute path;
3. resolved path exactly under approved `registry_root`;
4. parent directory exists;
5. target file does not already exist unless the command is an approved non-create open;
6. production backup precheck passed;
7. no ingest/re-index/migration active;
8. explicit operator approval for the production action.

## Path-safety requirements
### Mandatory
- explicit `--db` path required
- absolute paths only
- resolve symlinks before safety evaluation
- reject path traversal by checking resolved path, not raw text
- reject creation under `.rag_db`
- reject creation under project root
- reject creation under home-directory defaults when not explicitly approved
- reject create if target file already exists and create-mode is requested
- reject if parent directory does not exist

### Symlink handling
All safety checks must use:
- `Path.expanduser().resolve()`

The resolved path is authoritative for:
- temporary-root approval
- production-root approval
- project-root exclusion
- `.rag_db` exclusion

## Existing-file protection
Create-mode must fail if the target DB already exists.

Overwrite-in-place is not approved for:
- first production creation;
- restore drills;
- validation rehearsals;
- dry-run artifacts.

Use a new file or an approved snapshot/replace workflow only after a separate policy is approved.

## Restore-root policy
Restored copies must never resolve to production roots.

Approved restore-root class:
- explicit isolated root under `/private/tmp` or another separately approved isolated restore root

Restore copy layout should mirror:
- isolated `.rag_db` copy
- isolated registry copy
- manifests/logs/results outside the restored DB roots

## Design restrictions preserved
This policy does not approve:
- production registry creation by this Step;
- population;
- Chroma mutation;
- retrieval integration code;
- re-indexing.

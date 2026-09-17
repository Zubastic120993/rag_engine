# LC6 Operational Handoff Controls Contract v2

Status: additive remediation package, synthetic/disposable validation only until separate real-operation gate.

Package path:
`/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z`

Immutable sealed evidence package:
`/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z`

Sealed `SHA256SUMS` SHA-256 authority:
`b9307864fc2dd9eed6778d6028fc95ca8c19f1576d5e5dacee1a772d24e5cd5d`

## Inventory contract

Canonical inventory schema: `inventory-row-v1`.

Each inventory row has exactly:

- `path`
- `type`
- `bytes`
- `sha256`

Rows are sorted by relative path and hashed as canonical JSON array with sorted keys and compact separators. Schema value must be present, non-empty string, and exactly `inventory-row-v1`. Unknown, null, empty, or non-string schema values fail closed except explicitly labelled legacy interpretation.

The unchanged 19-file production snapshot must produce canonical inventory SHA-256:
`b189d622935faf6a0aa14830a80fa73af85c49f2d965bb83cdfb23acdb4f992a`

Certified external baseline inventory rule: `.orphan_repair_disposable_clone.json` is marker evidence and is not part of the canonical source-content inventory. The source-content inventory must contain exactly 19 `inventory-row-v1` rows with SHA-256 `b189d622935faf6a0aa14830a80fa73af85c49f2d965bb83cdfb23acdb4f992a`. The marker is verified separately with SHA-256 `e148d9b83bbcff31d24f85ad461387457efc994138ff62003aafc2733284d2f1`. No other file may be omitted.

## Controls

1. Backup creation requires explicit `--source`, `--dest`, and `--journal`; source must equal active `RAG_DB_PATH`; mutation destinations must be under `/private/tmp` during validation and outside production, legacy `.rag_db`, `.rag_db_generations`, CE Library territory, sealed package, and certified external baseline.
2. Restore verification copies to a fresh target and verifies byte/hash inventory identity, SQLite tables, tracker/orphan count, collection identity, and embedding-payload digest.
3. Candidate creation copies from a verified disposable source and writes `.lc6_operational_candidate_v1.json` plus the compatibility marker required by the immutable sealed executor. It refuses the certified external baseline itself as source/target.
4. Candidate repair wrapper validates candidate marker, sealed package checksum, and sealed manifest before invoking immutable `orphan_repair_executor.py`; it records pre/post hashes, embedding digest, command, output, failed-artifact preservation, and recovery ownership.
5. Independent postcheck verifies inventory, tracker, SQLite/Chroma structure, chunk/orphan counts, collection identity, embedding-payload digest, WAL/SHM/lock state, and process state.
6. Embedding-payload digest uses Chroma public collection API on disposable `/private/tmp` generation copies only. It uses stable ordering by `embedding_id`, canonical byte header, dtype `float32-le`, vector dimensions, and actual vector payload values. Protected production and certified baseline paths must not be opened through Chroma.
7. Real production-selection switch remains blocked unless a persistent owner of `RAG_DB_PATH` is proven. Synthetic selection-file switch exists only for validation.
8. Rollback switch prevents double rollback and is synthetic-only until persistent production selection owner is governed.

## Candidate validation lifecycle

Pre-mutation validation is mandatory before any candidate mutation. It validates candidate marker structure and identity, source inventory schema, source file hashes against the untouched candidate, and source embedding-payload digest. The validated pre-state is written and fsynced to the durable operation journal. Wrong/stale marker or source-hash mismatch fails before mutation.

Post-mutation validation uses the journaled pre-state as authority for marker identity/provenance. It does not reapply the pre-mutation whole-file source-hash guard to current mutable candidate files. Before mutation the journal must persist the complete ordered vector-ID set, per-vector float payload SHA-256 keyed by vector ID, per-vector dtype and dimensions, embedding metadata keyed by vector ID, tracker records for the exact sealed-manifest affected set, and unaffected content/store digests.

Batch B2 postcheck is set-aware. It is bound to the exact authorized retirement set from the sealed LC6 contract: retired digest `247b28a9ff07169473ddb7ac5f54dc61bfac046796bd7d0b36249fa36e166c90` and vector IDs `chunk:7cb9d07e312b2e6080aa112f5a165428` and `chunk:9c1743c71b1b7c2e9d9aea88d3fbc247`. The only valid vector-set change is `125291 -> 125289`, removed IDs exactly equal to those two IDs, no added IDs, and retained IDs exactly equal to pre IDs minus the authorized set. Every retained vector must keep identical float payload digest, dtype, and dimensions. Retired IDs must be absent from vectors and associated metadata.

Embedding metadata and tracker changes must match exact manifest operations by digest, chunk ID, metadata key, path, and collection transition. Record counts or broad patterns are not sufficient. Changed files are limited to `chroma.sqlite3`, `embedded.json`, the named HNSW segment files under `abda5535-6e53-4124-b725-55046ffb0347/`, and separately validated marker/control artifacts. Every unknown changed file or unexpected marker mutation fails closed. Certified-baseline, production, and selection after-snapshots must be written in a finally safety stage on success or failure; a postcheck failure must not skip these snapshots.

## Embedding payload digest return contract

`embedding_payload_digest()` returns a stable contract from a Chroma public collection provider:

- `sha256`: digest binding ordered vector/chunk ID, dimension, and actual float payload.
- `vector_count`: number of vectors included.
- `ordered_vector_ids`: vector/chunk IDs sorted by `embedding_id`.
- `ordered_id_digest`: digest of the sorted ID stream.
- `dtype`: `float32-le`.
- `dimensions`: sorted unique dimensions observed.
- `per_vector_dimensions`: dimension by ordered vector/chunk ID.
- `per_vector_dtype`: dtype by ordered vector/chunk ID.
- `per_vector_payload_sha256`: deterministic float32-le payload digest by ordered vector/chunk ID.
- `provider`: provider/version metadata.
- `pagination`: bounded page evidence.
- `canonical_byte_format_version`: `lc6-embedding-payload-digest-v2`.

Digest input order and format:
1. fixed header `lc6-embedding-payload-digest-v2`;
2. fixed collection label;
3. fixed ordering label `embedding_id`;
4. fixed dtype label `float32-le`;
5. for each vector ordered by `embedding_id`: UTF-8 vector/chunk ID, dimension, and actual vector values re-encoded as deterministic little-endian float32 bytes.

Failure conditions: missing vectors, duplicate IDs, incomplete pagination, multiple unexpected collections, inconsistent dimensions, NaN/Infinity, and protected-path Chroma open attempts all fail closed.

Batch B1 baseline/production rule: protected production and certified baseline identity checks use marker, canonical inventory, file hashes, SQLite structural/count evidence, collection metadata, WAL/SHM/lock state, and process state only. Embedding payload digest is recorded as `DEFERRED_TO_BATCH_B2_DISPOSABLE_COPY`. Batch B2 must compute pre/post payload digests only after creating a fresh disposable candidate copy and must verify certified baseline byte identity before and after.

Batch B1 process-state rule: process detection excludes the current inspection process and known wrapper/ancestor chain by PID identity. A command-line path mention alone is not a writer. Relevant activity is determined from executable/action identity and, where available, open file descriptors or database/lock ownership. Independent ingest, repair, executor, Chroma writer, write-FD holder, or ambiguous path match without reader/writer proof fails closed.

Batch B2 runtime rule: before creating any backup, restore, or candidate directory, the selected runtime Python must be resolved and validated by subprocess import checks for required runtime modules, including `chromadb`. The authoritative governed runtime is `/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/python`, proven by sealed LC6 execution evidence. The selected interpreter path, Python version, authoritative-runtime path, and dependency versions must be journaled. `/Users/vladymyrzub/CE_Library/Tools/rag_engine/.venv/bin/python` is an unauthorized/uncommitted `uv run` side-effect runtime and must be refused. Chroma public-API calls and sealed-executor subprocesses must use the same validated runtime unless a separately versioned contract explicitly permits, documents, and tests a separation. Missing `chromadb`, wrong interpreter, `.venv`, or runtime mismatch fails before copy/candidate creation.

Batch B2 candidate source-content rule: source-content identity uses versioned contract `source-content-hash-v1`. Source-content hashes exclude only the explicitly named control artifacts `.orphan_repair_disposable_clone.json` and `.lc6_operational_candidate_v1.json`; journals are outside the candidate tree. Both marker artifacts are recorded separately as provenance evidence with their own hashes and schemas. The same exclusion function must be used for restored-source snapshot, candidate creation, candidate marker writing, pre-mutation validation, and post-mutation bounded-change validation. Unknown files, arbitrary dotfiles, added files, removed files, or changed normal content files fail closed with explicit `expected_only`, `actual_only`, and `changed_paths` evidence.

Selection comparison rule: production selection snapshots are normalized to declared schema `selection-snapshot-v1` before comparison. `active_generation` is represented consistently on both sides, derived from `RAG_DB_PATH` when absent. Non-null unknown fields are preserved under `extra_fields`; they must not be discarded to force a pass.

## Boundary

No command in this package authorizes production backup, production candidate creation, production repair, production switch, seal, commit, or push without separate operator approval.

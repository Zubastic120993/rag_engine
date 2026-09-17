# LC6 Operational Handoff Controls Runbook v2

Do not execute real production operations without separate operator approval. During validation, mutation paths must be under `/private/tmp`.

## Read-only production snapshot

```sh
RAG_DB_PATH="/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_20260814T182037Z_698e0df44604" CE_LIBRARY_ROOT="/Users/vladymyrzub/CE_Library" python3 "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z/lc6_operational_controls.py" snapshot-production --journal "/private/tmp/lc6_production_snapshot_v2.json"
```

## Verify immutable sealed package

```sh
python3 "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z/lc6_operational_controls.py" verify-sealed --package-dir "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z"
```

## Verify certified external baseline read-only

Batch B1 must not open the certified external baseline through Chroma. Baseline identity uses marker, canonical source-content inventory, file hashes, SQLite structural/count evidence, collection metadata, WAL/SHM/lock, and process state. Embedding payload digest is deferred as `DEFERRED_TO_BATCH_B2_DISPOSABLE_COPY`.

Inventory rule: `.orphan_repair_disposable_clone.json` is marker evidence and is excluded from the canonical source-content inventory. The content inventory must contain exactly 19 rows and SHA-256 `b189d622935faf6a0aa14830a80fa73af85c49f2d965bb83cdfb23acdb4f992a`. The marker is verified separately with SHA-256 `e148d9b83bbcff31d24f85ad461387457efc994138ff62003aafc2733284d2f1`.

Process-state rule: exclude the inspection process and known wrapper/ancestor chain by PID identity. Do not classify a command-line path mention alone as a writer. Fail closed for independent ingest, repair, executor, Chroma writer, write-FD holder, or ambiguous path match without reader/writer proof.

```sh
python3 "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z/lc6_operational_controls.py" verify-baseline --baseline "/Users/vladymyrzub/LC6_Disposable_Baselines/lc4_restored_baseline_20260904T135023Z"
```

## Future production backup gate

Destination shown is outside CE Library. Execute only after separate real-operation approval.

```sh
RAG_DB_PATH="/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_20260814T182037Z_698e0df44604" CE_LIBRARY_ROOT="/Users/vladymyrzub/CE_Library" python3 "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z/lc6_operational_controls.py" backup-create --source "/Users/vladymyrzub/CE_Library/.rag_db_generations/raggen_20260814T182037Z_698e0df44604" --dest "/private/tmp/lc6_real_gate_example_backup_20260910T172036Z" --journal "/private/tmp/lc6_backup_journal_v2.json"
```

## Restore verification

```sh
python3 "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z/lc6_operational_controls.py" restore-verify --backup "/private/tmp/lc6_real_gate_example_backup_20260910T172036Z" --restore-target "/private/tmp/lc6_restore_test_20260910T172036Z" --journal "/private/tmp/lc6_restore_verify_journal_v2.json"
```

## Candidate creation

```sh
python3 "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z/lc6_operational_controls.py" candidate-create --source "/private/tmp/lc6_restore_test_20260910T172036Z" --dest "/private/tmp/lc6_candidate_20260910T172036Z" --journal "/private/tmp/lc6_candidate_create_journal_v2.json" --purpose "LC6 orphan repair candidate"
```

## Candidate-only repair wrapper

Before any candidate mutation, validate and fsync the pre-mutation state to the operation journal. The pre-state binds marker identity, `source-content-hash-v1`, source embedding-payload digest, complete ordered vector-ID set, per-vector payload digest/dtype/dimension, embedding metadata keyed by ID, tracker records for the sealed-manifest affected set, and separate marker provenance. Source content excludes only `.orphan_repair_disposable_clone.json` and `.lc6_operational_candidate_v1.json`; journals remain outside the candidate tree. Marker/control artifacts are not source content and must be verified separately by hash/schema. Post-mutation validation must compare the same source-content hash contract and bounded set-aware changes against that journaled pre-state. Do not use current whole-file source hashes as the post-mutation authority after an authorized mutation.

Batch B2 must compute actual embedding-payload digests only after creating the fresh disposable candidate copy under `/private/tmp`. The provider uses Chroma public collection API, requires exactly the expected collection, fetches bounded pages, sorts by vector/embedding ID, and digests ID, dimension, dtype `float32-le`, and actual finite vector values. Protected production and certified baseline paths are refused.

Before Batch B2 creates any backup, restore, or candidate directory, run runtime preflight using the same interpreter that will open Chroma and invoke the sealed executor. The authoritative governed runtime is `/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/python`; it must import `chromadb` and record Python version plus installed dependency versions. Do not use `/Users/vladymyrzub/CE_Library/Tools/rag_engine/.venv/bin/python`; it is an unauthorized/uncommitted `uv run` side-effect runtime. Do not install dependencies during Batch B2. If the runtime is missing, is `.venv`, differs from the sealed LC6 authoritative runtime, or differs from the current orchestrating interpreter without a separately versioned approved split, stop before copy creation.

When comparing production selection before/after, normalize both snapshots to `selection-snapshot-v1`: include `CE_LIBRARY_ROOT`, `RAG_DB_PATH`, `mechanism`, and `active_generation` on both sides. Derive `active_generation` from `RAG_DB_PATH` only when absent. Preserve non-null unknown fields under `extra_fields`.

```sh
python3 "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z/lc6_operational_controls.py" repair-candidate --candidate "/private/tmp/lc6_candidate_20260910T172036Z" --work-dir "/private/tmp/lc6_candidate_repair_work_20260910T172036Z" --journal "/private/tmp/lc6_candidate_repair_wrapper_journal_v2.json" --package-dir "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z" --executor "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/orphan_repair_package_20260826T213554Z/orphan_repair_executor.py"
```

## Candidate postcheck

```sh
python3 "/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z/lc6_operational_controls.py" postcheck-candidate --candidate "/private/tmp/lc6_candidate_20260910T172036Z" --journal "/private/tmp/lc6_candidate_postcheck_journal_v2.json" --expected-orphans 0 --expected-embedding-digest "<digest from approved pre-repair candidate>"
```

`<digest from approved pre-repair candidate>` is not a command placeholder for execution; it must be copied from the verified candidate-create/repair journal before use.

For Batch B2 post-mutation checks, expected valid changes are bounded and set-aware. The authorized retired digest is `247b28a9ff07169473ddb7ac5f54dc61bfac046796bd7d0b36249fa36e166c90`; the only authorized removed vector IDs are `chunk:7cb9d07e312b2e6080aa112f5a165428` and `chunk:9c1743c71b1b7c2e9d9aea88d3fbc247`. Removed IDs must equal exactly that set, no added IDs are allowed, retained IDs must equal pre IDs minus those two IDs, retained float payload digest/dtype/dimensions must remain unchanged, and retired IDs must be absent from vectors and metadata. Embedding metadata and tracker changes must match exact sealed manifest operations by digest, chunk ID, metadata key, path, and collection transition.

Allowed changed files are only `chroma.sqlite3`, `embedded.json`, the named HNSW segment files under `abda5535-6e53-4124-b725-55046ffb0347/`, and separately validated marker/control artifacts. Unknown files and unexpected marker mutation fail closed. The Batch B2 runner must write certified-baseline, production, and selection after-snapshots in a finally safety stage on success or failure; postcheck failure must not skip same-run after evidence.

## Production selection / rollback

Real switch and rollback intentionally return `BLOCKED_MISSING_SELECTION_OWNER` until the persistent owner of `RAG_DB_PATH` is proven. No fake production switch command is provided.

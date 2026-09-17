# Batch B1 Embedding Provider Forward Erratum

Failed historical report preserved unchanged:
`BATCH_B1_PREREQUISITE_REPORT.json`

Historical report SHA-256:
`5bb53735d62fdcca70a7f316820837191daaf26ef24a31b50a617b8ce72d6167`

## Correction intent

The failed Batch B1 prerequisite workflow exposed an invalid assumption: current Chroma SQLite stores embedding IDs in `embeddings.embedding_id`, but not raw vectors in an `embeddings.embedding` column. Therefore the additive operational controls must not use direct SQLite payload reads as the production embedding-payload provider.

## Forward correction

- Raw embedding payload digesting now uses Chroma public collection API for disposable `/private/tmp` generations only.
- Chroma public API opening is refused for protected production, certified external baseline, CE Library `.rag_db`, and generations root paths.
- Batch B1 protected baseline/production checks must use marker, canonical inventory, file hashes, SQLite structural/count evidence, collection metadata, WAL/SHM/lock state, and process state only.
- Batch B1 embedding payload status for protected baseline/production is `DEFERRED_TO_BATCH_B2_DISPOSABLE_COPY`.
- Batch B2 must create a fresh disposable candidate copy first, then compute pre/post actual embedding payload digests only on the disposable candidate.

No sealed package, certified baseline, production, lock, selection, commit, push, seal, or promotion is authorized by this erratum.

# RAG Metadata Acceptance Test Plan — 20260731T065913Z

## Future acceptance tests
- schema validation for document, version, chunk, entity, and relationship records
- mandatory-field presence
- allowed controlled vocabulary enforcement
- stable document IDs across file moves
- stable document-version logic across unchanged re-ingest
- deterministic chunk IDs for unchanged chunking inputs
- duplicate detection and duplicate-family handling
- vessel separation and no cross-vessel leakage
- sister-vessel ambiguity handling
- authority ranking behavior by question class
- superseded-document exclusion
- citation preservation after file move and after metadata migration
- page metadata correctness
- source-hash and content-hash consistency
- re-ingestion determinism
- metadata migration rollback
- backup/restore compatibility
- Obsidian link integrity
- relationship integrity for defect/PMS/requisition/correspondence/photo links

## Minimum gate before implementation acceptance
- schema validated;
- mandatory fields present on all applicable records;
- controlled vocabularies enforced or explicitly marked `unknown` / `not_applicable`;
- stable IDs verified across file-move scenario;
- no cross-vessel leakage in filtered retrieval;
- superseded documents excluded when active successor exists;
- citations preserved after path move or registry redirect;
- migration rollback proven in isolated environment;
- backup/restore compatibility preserved.

## Test design notes
- Acceptance tests should be run first against an isolated metadata registry and isolated RAG copy.
- Any test requiring Chroma rebuild behavior must have a restore-tested backup boundary before execution.
- Obsidian link tests must verify that authoritative source references remain stable even if note titles or folders change.

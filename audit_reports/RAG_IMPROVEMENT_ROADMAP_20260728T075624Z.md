# RAG Improvement Roadmap — 20260728T075624Z

## 1. Immediate safety and integrity corrections

### I-01 Restore-tested backup workflow
- Objective: prove production DB/tracker can be recovered.
- Affected components: `.rag_db`, ops docs, doctor/recovery procedures.
- Proposed design: snapshot `chroma.sqlite3`, vector segment dirs, `embedded.json`, `index_fingerprint.json`; restore into isolated path; run doctor + sample queries.
- Expected benefit: real recovery confidence.
- Risk: moderate operational time.
- Migration needed: no.
- Re-indexing needed: no.
- Test required: successful isolated restore drill.
- Complexity: medium.
- Prerequisite: snapshot procedure.
- Sequence: **first**.
- Category: **required before normal use**.

#### Step 4 closure status — Validate production backup restore
- [x] Create backup procedure
- [x] Restore into isolated environment
- [x] Verify integrity
- [x] Document recovery time
- [ ] Repeat quarterly

Parent status:
- [x] Initial backup and restore validation complete
- [~] Ongoing quarterly validation active

Evidence references:
- `RAG_BACKUP_CREATION_20260728T112214Z.md`
- `RAG_RESTORE_VALIDATION_20260728T114008Z.md`
- `RAG_BACKUP_AND_RESTORE_PROCEDURE.md`
- `RAG_BACKUP_REGISTER.md`

Note:
- Initial restore proven with restrictions because normal CLI isolation is not yet supported through a scope/config override.

### I-02 Runtime self-sanitizing launcher
- Objective: remove dependence on manually clearing inherited Hermes Python state.
- Affected files/components: CLI entry wrappers, docs.
- Proposed design: standard launcher mirroring `run_tests.sh` sanitation for operational CLI entrypoints.
- Expected benefit: fewer false failures.
- Risk: low.
- Migration needed: no.
- Re-indexing needed: no.
- Test required: launch from contaminated shell.
- Complexity: small.
- Prerequisite: none.
- Sequence: second.
- Category: **required before normal use**.

## 2. Retrieval-quality improvements

### R-01 Hybrid retrieval for exact-number and code-sensitive questions
- Objective: combine semantic and lexical retrieval.
- Affected components: `query.py`, possible auxiliary index.
- Proposed design: semantic + BM25/FTS candidates, merge, rerank.
- Expected benefit: better exact limits, part numbers, requisitions, dates.
- Risk: medium complexity.
- Migration needed: maybe new lexical store.
- Re-indexing needed: likely yes.
- Test required: exact-number benchmark.
- Complexity: large.
- Prerequisite: benchmark set.
- Sequence: after metadata design.
- Category: **recommended before broad CE_Library indexing**.

### R-02 Duplicate-family suppression / diversification
- Objective: stop near-identical manuals consuming top-k.
- Affected components: retrieval ranking, metadata model.
- Proposed design: collapse same checksum/document family before final context assembly.
- Expected benefit: better evidence diversity.
- Risk: low to medium.
- Migration needed: metadata enrichment helpful.
- Re-indexing needed: optional.
- Test required: duplicate-result-rate metric.
- Complexity: medium.
- Prerequisite: checksum/family metadata.
- Sequence: after metadata enrichment.
- Category: **recommended before broad CE_Library indexing**.

## 3. Citation and evidence improvements

### C-01 Rich citation metadata and validation
- Objective: move from `source/page/collection` only to governed evidence references.
- Affected components: ingest, tracker, query, UI.
- Proposed design: attach revision, document type, authority, vessel, effective date, checksum, document ID; validate citations against source chunk membership.
- Expected benefit: stronger traceability and auditability.
- Risk: schema change.
- Migration needed: yes.
- Re-indexing needed: yes.
- Test required: citation/page accuracy benchmark.
- Complexity: large.
- Prerequisite: metadata schema approval.
- Sequence: high priority.
- Category: **recommended before broad CE_Library indexing**.

## 4. Metadata and document-governance improvements

### M-01 Expand chunk/document metadata schema
- Objective: support vessel-, authority-, and revision-aware retrieval.
- Affected components: `ingest.py`, tracker format, doctor, diagnostics, query filtering.
- Proposed design: add vessel, IMO, maker, model, system, title, revision, issue/effective date, document status, superseded-by, stable doc ID.
- Expected benefit: safer filtering and future governance automation.
- Risk: large migration.
- Migration needed: yes.
- Re-indexing needed: yes.
- Test required: metadata completeness audit.
- Complexity: large.
- Prerequisite: schema definition.
- Sequence: foundational.
- Category: **recommended before broad CE_Library indexing**.

#### Step 6 design status — Metadata Standard v1
- [x] Metadata current-state study complete
- [x] Metadata architecture boundary approved
- [x] Stable-ID policy approved
- [x] Controlled vocabulary v1 approved
- [x] Human-review boundary approved
- [x] Production registry location policy approved
- [x] Population policy approved
- [x] Backup and restore policy approved
- [x] Retrieval integration boundary approved
- [ ] SQLite registry implementation
- [ ] Production registry creation
- [ ] Registry population
- [ ] Registry-aware retrieval deployment
- [ ] Chroma metadata migration
- [ ] Controlled re-indexing
- [ ] Obsidian projection

Design evidence references:
- `MARITIME_ENGINEERING_METADATA_STANDARD_V1.md`
- `RAG_METADATA_REGISTRY_SCHEMA_V1.md`
- `RAG_METADATA_CONTROLLED_VOCABULARIES_V1.md`
- `RAG_STABLE_IDENTIFIER_SPECIFICATION_V1.md`
- `RAG_METADATA_HUMAN_REVIEW_POLICY_V1.md`
- `RAG_CHROMA_METADATA_BOUNDARY_V1.md`
- `RAG_OBSIDIAN_METADATA_BOUNDARY_V1.md`
- `RAG_METADATA_IMPLEMENTATION_PLAN_V1.md`
- `ADR_001_METADATA_REGISTRY_ARCHITECTURE.md`
- `RAG_METADATA_REGISTRY_LOCATION_POLICY_V1.md`
- `RAG_METADATA_REGISTRY_POPULATION_POLICY_V1.md`
- `RAG_METADATA_REGISTRY_BACKUP_RESTORE_POLICY_V1.md`
- `RAG_METADATA_RETRIEVAL_INTEGRATION_BOUNDARY_V1.md`

### M-02 Explicit orphan/deletion cleanup workflow
- Objective: remove stale chunks when files disappear or are superseded.
- Affected components: ingest/maintenance helpers/doctor.
- Proposed design: dry-run orphan detector + operator-approved cleanup command.
- Expected benefit: reduced stale retrieval.
- Risk: medium if cleanup is wrong.
- Migration needed: no.
- Re-indexing needed: no.
- Test required: controlled remove/move test corpus.
- Complexity: medium.
- Prerequisite: backup restore test.
- Sequence: after recovery proof.
- Category: **required before normal use**.

## 5. Testing and benchmark improvements

### T-01 Real maritime benchmark set
- Objective: replace self-authored 12-case eval as main confidence signal.
- Affected components: `eval/questions.json`, eval harness, test corpus process.
- Proposed design: build cases from actual CE_Library queries and past misses.
- Expected benefit: realistic retrieval quality measurement.
- Risk: low.
- Migration needed: no.
- Re-indexing needed: no.
- Test required: repeated benchmark runs.
- Complexity: medium.
- Prerequisite: question collection.
- Sequence: parallel with metadata work.
- Category: **recommended before broad CE_Library indexing**.

### T-02 Isolated `/tmp` Phase 4 harness execution in trusted runtime
- Objective: complete blocked isolated write-scope tests.
- Affected components: audit/test tooling.
- Proposed design: run approved harness in runtime that honors explicit consent and clean env.
- Expected benefit: closes untested failure-mode gaps.
- Risk: low.
- Migration needed: no.
- Re-indexing needed: no.
- Test required: harness itself.
- Complexity: small.
- Prerequisite: compliant runtime.
- Sequence: soon.
- Category: **useful later**.

## 6. Reliability and recovery improvements

### L-01 Consolidated maintenance API without private Chroma calls
- Objective: remove direct dependence on `db._collection.update`.
- Affected components: `ingest.py`, `backfill_collections.py`.
- Proposed design: one compatibility layer or official client path.
- Expected benefit: safer upgrades.
- Risk: medium.
- Migration needed: no.
- Re-indexing needed: no.
- Test required: rename/backfill tests.
- Complexity: medium.
- Prerequisite: API design.
- Sequence: after core recovery fixes.
- Category: **useful later**.

### L-02 Split logs from index directory
- Objective: separate operational telemetry from recovery-critical data.
- Affected components: `events.py`, config.
- Proposed design: configurable log path outside `.rag_db`.
- Expected benefit: cleaner backup/retention policy.
- Risk: low.
- Migration needed: maybe move existing JSONL.
- Re-indexing needed: no.
- Test required: ask path append test.
- Complexity: small.
- Prerequisite: config extension.
- Sequence: later.
- Category: **useful later**.

## 7. Performance improvements

### P-01 Measure and tune search width, top-k, and context size against benchmark
- Objective: tune latency/quality trade-off using real benchmark, not comments alone.
- Affected components: `query.py`, model settings.
- Proposed design: benchmark matrix over width/top-k/ctx.
- Expected benefit: lower latency or better recall with evidence.
- Risk: low.
- Migration needed: no.
- Re-indexing needed: no.
- Test required: latency + hit-rate benchmark.
- Complexity: medium.
- Prerequisite: benchmark set.
- Sequence: after benchmark creation.
- Category: **useful later**.

## 8. User-interface and workflow improvements

### U-01 Make scope intent clearer in UI and diagnostics
- Objective: reduce operator misunderstanding of broad aliases.
- Affected components: `app.py`, CLI diagnostics, docs.
- Proposed design: display resolved scope meaning and coverage note before answer.
- Expected benefit: fewer cross-scope mistakes.
- Risk: low.
- Migration needed: no.
- Re-indexing needed: no.
- Test required: UI/manual review.
- Complexity: small.
- Prerequisite: none.
- Sequence: near term.
- Category: **useful later**.

## 9. Longer-term architecture improvements

### A-01 OCR/table extraction lane with document-class routing
- Objective: handle scanned PDFs and structured manuals properly.
- Affected components: ingest pipeline, metadata schema, benchmark.
- Proposed design: route scanned/image-heavy docs through OCR; route structured tables through specialized extractors.
- Expected benefit: better coverage of real maritime manuals and service letters.
- Risk: large implementation and QA effort.
- Migration needed: yes.
- Re-indexing needed: yes.
- Test required: scanned/table benchmark.
- Complexity: large.
- Prerequisite: metadata and benchmark framework.
- Sequence: after immediate governance fixes.
- Category: **recommended before broad CE_Library indexing**.

## Priority buckets

### Required before normal use
- I-01 Restore-tested backup workflow
- I-02 Runtime self-sanitizing launcher
- M-02 Explicit orphan/deletion cleanup workflow

### Recommended before broad CE_Library indexing
- R-01 Hybrid retrieval
- C-01 Rich citation metadata and validation
- M-01 Expanded metadata schema
- T-01 Real maritime benchmark set
- A-01 OCR/table extraction lane
- R-02 Duplicate-family suppression

### Useful later
- T-02 Isolated `/tmp` harness rerun in trusted runtime
- L-01 Remove private Chroma API dependence
- L-02 Split logs from index directory
- P-01 Performance tuning benchmark
- U-01 Clearer scope UX

### Not currently justified
- Full remote/multi-user auth stack for the local Gradio UI, as long as it remains bound to `127.0.0.1` and not exposed remotely.

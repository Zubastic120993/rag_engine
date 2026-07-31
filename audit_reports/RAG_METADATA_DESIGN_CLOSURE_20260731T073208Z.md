# RAG Metadata Design Closure — 20260731T073208Z

## Study and design status
Step 6 completed as design/documentation only.

No migration, registry creation, ingestion, re-indexing, embedding generation, production SQLite registry creation, or Chroma rebuild was executed.

## Evidence reviewed
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/MARITIME_ENGINEERING_METADATA_STANDARD_PROPOSAL_V1_20260731T065913Z.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_METADATA_CURRENT_STATE_20260731T065913Z.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_METADATA_GAP_ANALYSIS_20260731T065913Z.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_METADATA_MIGRATION_IMPACT_20260731T065913Z.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_METADATA_ACCEPTANCE_TEST_PLAN_20260731T065913Z.md`

## Verified Step 5 evidence confirmed in Step 6
- current production metadata fields: `collection`, `page`, `source`
- production chunk count: `108685`
- stable IDs absent in current production metadata
- vessel applicability absent in current production metadata
- authority metadata absent in current production metadata
- revision and supersession metadata absent in current production metadata
- relationship metadata absent in current production metadata
- Option C confirmed as the recommended initial implementation boundary
- Option D confirmed as the target architecture
- registry-first implementation confirmed as not requiring immediate embedding rebuild

## Conflict check
No conflict was found between the approved Step 6 architecture decisions and the verified Step 5 evidence.

## Decisions approved
- SQLite registry approved as authoritative metadata layer
- Chroma retained as embedding/chunk index only
- Obsidian retained as derived knowledge layer only
- vessel identity approved as `vessel:<IMO>`
- document identity approved as stable UUID-based logical identity
- document-version identity approved as deterministic version identity
- chunk identity approved as later deterministic governed identity
- human-review boundary approved for applicability, authority, supersession, confidentiality, and operational relationships
- generated Obsidian graph outputs remain excluded from production ingestion by default

## Documents created
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/MARITIME_ENGINEERING_METADATA_STANDARD_V1.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/RAG_METADATA_REGISTRY_SCHEMA_V1.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/RAG_METADATA_CONTROLLED_VOCABULARIES_V1.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/RAG_AUTHORITY_AND_CONFLICT_MODEL_V1.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/RAG_STABLE_IDENTIFIER_SPECIFICATION_V1.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/RAG_METADATA_HUMAN_REVIEW_POLICY_V1.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/RAG_CHROMA_METADATA_BOUNDARY_V1.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/RAG_OBSIDIAN_METADATA_BOUNDARY_V1.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/RAG_METADATA_IMPLEMENTATION_PLAN_V1.md`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/docs/ADR_001_METADATA_REGISTRY_ARCHITECTURE.md`

## Mandatory v1 fields
### Registry v1 mandatory now
- `document_id`
- `document_version_id`
- `source_hash`
- `relative_path`
- `filename`
- `file_extension`
- `scope`
- `document_type`
- `status`
- `authority_class`
- `approval_status`
- `verification_status`
- `ingestion_timestamp`
- `ingestion_version`
- `metadata_confidence`
- `human_review_status`

### Conditional fields
- `vessel_id`
- `vessel_name`
- `vessel_imo`
- `revision`
- `issue_date`
- `effective_date`
- `superseded_by`
- `maker`
- `model`
- `equipment`
- `document_number`

### Chroma mandatory now
- existing `collection`, `page`, and `source` only

### Chroma mandatory after future governed re-indexing
- `chunk_id`
- `document_version_id`
- `source_hash`
- `page_number`
- `chunk_index`
- `content_hash`
- `ingestion_version`

### Optional now
- `expiry_date`
- `language`
- `safety_criticality`
- `operational_mode`
- `operational_phase`
- `confidentiality_class`
- `issuer`
- `equipment_tag`
- `subsystem`

### Deferred v2
- `serial_number`
- `maintenance_interval`
- `alarm_code`
- `work_order_number`
- advanced table-extraction outputs
- richer contradiction-resolution graph fields
- extended relationship scoring and weighting

## Stable-ID decisions
- vessel stable ID: `vessel:<IMO>`
- approved example vessel: `vessel:9961192` / `GASCHEM EUROPE` / `9961192`
- path is not a primary identity
- document logical identity uses stable UUID
- document version identity is deterministic and version-sensitive
- chunk identity is deterministic and chunking-version-sensitive

## Vocabulary decisions
Controlled vocabularies approved for:
- scope
- document_type
- authority_class
- approval_status
- document status
- verification_status
- human_review_status
- safety_criticality
- fleet_applicability
- operational_mode
- operational_phase
- relationship_type
- confidentiality_class
- metadata_source_type
- assertion_status

## Authority model
Approved as question-dependent, applicability-aware, revision-aware, approval-aware, verification-aware, and contradiction-aware.

No universal absolute ranking is approved.

## Human-review model
Approved as mandatory for:
- vessel applicability
- sister-vessel applicability
- authority class
- approval status
- supersession relationships
- ambiguous revision
- ambiguous document type
- confidentiality classification for correspondence/defect/requisition/photo relationships
- relationship validation between operational records

Role-based reviewer values are preferred.

## Chroma boundary
Approved as compact retrieval metadata only.

Rich authority, applicability, relationship, provenance history, confidentiality detail, and review state remain governed in SQLite.

## Obsidian boundary
Approved as derived only.

Automatic Obsidian note generation and graph projection are not approved in this Step.

Generated graph folders remain excluded from production ingestion by default.

## Migration phases
- Phase 1: registry schema and read-only discovery
- Phase 2: deterministic registry population from existing evidence
- Phase 3: retrieval joins and registry-aware filters without chunk-ID change if avoidable
- Phase 4: deterministic chunk IDs and governed Chroma metadata with controlled re-indexing
- Phase 5: Obsidian relationship projection

## Roadmap status
Updated to show:
- `[x] Metadata current-state study complete`
- `[x] Metadata architecture boundary approved`
- `[x] Stable-ID policy approved`
- `[x] Controlled vocabulary v1 approved`
- `[x] Human-review boundary approved`
- implementation items still pending

## Remaining risks
- logical-document grouping during migration may still require careful review
- authority and applicability review workload may be significant
- future Chroma/registry divergence must be actively checked
- confidentiality handling must remain strict for relationship records
- deterministic chunk-ID phase still requires later controlled re-indexing and rollback gating

## Next approved implementation step
Proceed only to Step 7 preparation for registry schema realization, deterministic population rules, and acceptance-test scaffolding, without production migration until separately approved.

## Protected-file verification
Protected production/source/configuration files remained unchanged during Step 6 design work.

Verified unchanged by matching SHA-256 before/after snapshot for:
- `/Users/vladymyrzub/CE_Library/.rag_db/chroma.sqlite3`
- `/Users/vladymyrzub/CE_Library/.rag_db/embedded.json`
- `/Users/vladymyrzub/CE_Library/.rag_db/index_fingerprint.json`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/rag_engine/scopes.yaml`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/pyproject.toml`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/rag_engine/ingest.py`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/rag_engine/query.py`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/rag_engine/config.py`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/rag_engine/scope_rules.py`
- `/Users/vladymyrzub/CE_Library/Tools/rag_engine/rag_engine/pdf_links.py`

## Classification
**METADATA V1 DESIGN APPROVED WITH RESTRICTIONS**

## PASS / FAIL / BLOCKED
**PASS**

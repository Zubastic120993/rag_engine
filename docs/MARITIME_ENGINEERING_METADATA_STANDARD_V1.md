# Maritime Engineering Metadata Standard v1

Revision date: 2026-07-31
Status: Approved for v1 design
Applies to: `rag_engine` design and future implementation work

## 1. Purpose
This standard defines the governed metadata model for the Chief Engineer knowledge system built around CE_Library, a SQLite metadata registry, a Chroma retrieval index, and a derived Obsidian knowledge layer.

It converts the Step 5 study into a formal implementation specification.

This document is design-only. It does not authorize registry creation, migration, Chroma rebuild, ingestion, or production metadata changes.

## 2. Scope
This standard applies to:
- source-document metadata;
- document-version metadata;
- vessel applicability metadata;
- authority and approval metadata;
- provenance and review metadata;
- relationship metadata;
- minimal Chroma retrieval metadata;
- derived Obsidian knowledge links.

This standard does not itself define application code changes or execute any migration.

## 3. Architecture boundary
Approved target architecture:

CE_Library source files
→ authoritative SQLite metadata registry
→ Chroma embedding and chunk retrieval index
→ derived Obsidian knowledge-graph layer

Approved v1 implementation boundary:
- **Option C**: Chroma plus structured SQLite metadata registry.

Approved target architecture after registry maturity:
- **Option D**: SQLite registry plus Obsidian graph layer.

## 4. Sources of truth
### 4.1 SQLite registry
The SQLite registry is authoritative for governed document metadata.

### 4.2 Chroma
Chroma is authoritative only for:
- vector embeddings;
- chunk text;
- retrieval index state;
- minimal retrieval-filter metadata;
- page/citation linkage.

### 4.3 Obsidian
Obsidian is not authoritative for source-document metadata. It is a derived knowledge layer only.

### 4.4 Source files
Original CE_Library files remain authoritative for the document body, maker text, company forms, correspondence files, certificates, reports, and similar source artifacts.

## 5. Metadata layers
### 5.1 Document-level metadata
Logical document identity, document type, scope, authority, approval, revision, status, applicability, and confidentiality.

### 5.2 Chunk-level metadata
Chunk identity, document-version linkage, page coordinates, content hash, chunk index, and retrieval-safe filter metadata.

### 5.3 Entity metadata
Canonical entities such as vessels, equipment, makers, models, systems, defects, PMS jobs, requisitions, certificates, and correspondence threads.

### 5.4 Relationship metadata
Explicit cross-record links such as supersedes, applies_to_vessel, evidences, linked_to_defect, linked_to_pms, linked_to_requisition, linked_to_photo, and linked_to_note.

### 5.5 Authority and governance metadata
Authority class, issuer, approval status, verification status, confidentiality class, and review state.

### 5.6 Ingestion and provenance metadata
Source hash, ingestion timestamp, ingestion version, extractor, metadata source type, assertion provenance, and human-review trace.

### 5.7 Runtime and retrieval metadata
Resolved scope, retrieval distance, benchmark status, answer gate, and other query-time data. These are not the governed source-document truth.

### 5.8 Obsidian and knowledge-graph metadata
Derived note IDs, backlinks, evidence references, generated-note flags, and stable-ID links back to registry records.

## 6. Identity model
The system uses separate identities for:
- vessel identity;
- logical document identity;
- exact document-version identity;
- source-file identity;
- chunk identity;
- equipment identity;
- relationship identity;
- ingestion-run identity;
- human-review identity.

Path must not be the primary identity.

## 7. Vessel identity policy
### 7.1 Stable vessel identifier
Use:
- `vessel_id = vessel:<IMO number>`

Approved example:
- `vessel_id = vessel:9961192`
- `vessel_name = GASCHEM EUROPE`
- `vessel_imo = 9961192`

### 7.2 Vessel name
Vessel name is descriptive metadata only. It must not be the stable identifier.

### 7.3 Generic documents
Generic documents may use:
- `vessel_id = null`
- `scope = generic`

### 7.4 Sister-vessel applicability
Sister-vessel applicability must be represented separately. It must not imply that sister vessels are interchangeable.

## 8. Document identity policy
`document_id`:
- is a stable UUID;
- identifies the logical document;
- survives file moves and folder renames;
- does not change merely because metadata is enriched.

A logical document may have several document versions over time.

## 9. Document-version identity policy
`document_version_id`:
- identifies one exact document version;
- is derived deterministically from logical document identity, revision/status information where available, and source hash;
- changes when the source document version changes.

Document-version identity distinguishes changed source files even when they remain the same logical document.

## 10. Chunk identity policy
`chunk_id`:
- is deterministic;
- is based on `document_version_id`, chunking-version identifier, page/section context, and chunk sequence or chunk content hash;
- changes when the chunking algorithm materially changes.

Chunk identity belongs to the later governed Chroma phase and is not retrofitted in Step 6.

## 11. Authority model
The model must distinguish at minimum:
- statutory/regulatory source;
- flag-state source;
- classification-society source;
- company/SMS instruction;
- vessel-approved procedure;
- maker manual;
- maker service letter;
- technical correspondence;
- PMS record;
- defect report;
- work report;
- handover note;
- personal working note;
- AI-generated derivative note.

No universal absolute ranking is approved. Preference depends on the question and on applicability, revision, approval, verification, safety criticality, and contradiction state.

## 12. Applicability model
Applicability must be represented explicitly and separately from scope.

Required concepts:
- single-vessel applicability;
- sister-vessel applicability;
- fleet-wide applicability;
- generic applicability;
- equipment applicability;
- model-specific applicability;
- operational-mode applicability;
- operational-phase applicability.

Folder location alone is not authoritative proof of applicability.

## 13. Revision and supersession model
The registry must distinguish:
- `revision`;
- `issue_date`;
- `effective_date`;
- `status`;
- `supersedes`;
- `superseded_by`.

Supersession links require human approval when ambiguous.

## 14. Human-review model
Human approval is mandatory for:
- vessel applicability;
- sister-vessel applicability;
- authority_class;
- approval_status;
- supersession relationships;
- ambiguous revision;
- ambiguous document type;
- confidentiality classification where relationships involve correspondence, defects, requisitions, or photos;
- relationship validation between operational records.

Automatically inferred values must remain distinguishable from human-approved values.

## 15. Confidence and verification model
Every governed assertion should be representable with:
- assertion status;
- metadata confidence;
- verification status;
- provenance source type;
- human-review status.

Confidence is not a substitute for approval.

## 16. Confidentiality model
Relationship records must avoid unnecessary duplication of confidential text.

Store references such as:
- stable IDs;
- relationship type;
- source and target identifiers;
- date;
- verification status;
- confidentiality class;
- provenance.

Do not store complete email bodies, defect narratives, requisition text, or photo contents in relationship records unless explicitly required and approved.

## 17. Provenance model
Each governed record or assertion should preserve:
- source file identifier;
- source hash;
- metadata source type;
- extractor and extractor version where relevant;
- ingestion timestamp;
- ingestion version;
- review status;
- reviewer role;
- reviewed-at timestamp when applicable.

## 18. Relationship model
Relationships are first-class governed records. They must not be flattened into arbitrary free-text notes.

Required minimum concepts:
- `relationship_id`
- `relationship_type`
- `source_record_type`
- `source_record_id`
- `target_record_type`
- `target_record_id`
- `verification_status`
- `confidentiality_class`
- provenance and audit fields

## 19. Chroma metadata boundary
Chroma metadata must remain compact and retrieval-focused.

Current production fields verified in Step 5:
- `collection`
- `page`
- `source`

Future governed Chroma metadata is defined separately in the Chroma boundary document.

## 20. SQLite registry boundary
The SQLite registry is the authoritative governed metadata layer. It holds:
- stable identities;
- document control;
- authority metadata;
- vessel and equipment applicability;
- relationships;
- review and provenance records;
- controlled vocabulary governance.

### 20.1 Registry v1 mandatory now
Registry-governed mandatory v1 fields are:
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

### 20.2 Conditional registry fields
Conditional fields are required when applicable to the document class or evidence set:
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

### 20.3 Registry optional now
Useful but not mandatory in v1 initial implementation:
- `expiry_date`
- `language`
- `safety_criticality`
- `operational_mode`
- `operational_phase`
- `confidentiality_class`
- `issuer`
- `equipment_tag`
- `subsystem`

### 20.4 Deferred v2 registry candidates
Deferred v2 candidates remain outside the initial registry-governed core:
- `serial_number`
- `maintenance_interval`
- `alarm_code`
- `work_order_number`
- advanced table-extraction outputs
- richer contradiction-resolution graph fields
- extended relationship scoring and weighting

## 21. Obsidian boundary
Obsidian may contain:
- derived note IDs;
- user-approved summaries;
- evidence links;
- hub pages;
- generated relationship projections.

Obsidian must not become the source of truth for governed source-document metadata.

## 22. Generated-content exclusion rule
Generated Obsidian graph folders and generated derivative indexes remain excluded from production ingestion by default.

The standard must prevent circular ingestion:

source document
→ generated derivative note
→ re-ingested derivative note
→ duplicate or self-referential evidence

## 23. Controlled vocabularies
Controlled vocabularies are defined in the separate vocabulary specification. They include at minimum:
- scope;
- document_type;
- authority_class;
- approval_status;
- document status;
- verification_status;
- human_review_status;
- safety_criticality;
- fleet_applicability;
- operational_mode;
- operational_phase;
- relationship_type;
- confidentiality_class;
- metadata_source_type;
- assertion_status.

## 24. Unknown, ambiguous, and conflicting-value rules
Use distinct machine values and meanings:
- `unknown`: value not yet known
- `not_applicable`: field does not apply
- `ambiguous`: more than one plausible value exists and is not resolved
- `conflicting`: evidence sources disagree and no approved resolution exists

These states must not be collapsed into one another.

## 25. Validation requirements
A future implementation must validate at minimum:
- mandatory-field presence;
- vocabulary compliance;
- stable ID format;
- uniqueness constraints;
- source-hash consistency;
- vessel separation;
- supersession consistency;
- registry/Chroma consistency for copied fields;
- provenance completeness for governed assertions.

## 26. Migration principles
Approved migration principles:
- registry-first implementation;
- no immediate re-embedding required;
- deterministic use of existing evidence where possible;
- human-review queue for ambiguous fields;
- deferred governed chunk-ID phase;
- no Chroma rebuild in Step 6.

## 27. Backup and rollback requirements
Before any future production migration that affects registry or Chroma state:
- maintain verified backup discipline;
- require restore-tested backup before major destructive changes;
- use rollback points between schema creation, registry population, review approvals, and any later Chroma rebuild.

## 28. Deferred v2 fields
Deferred v2 candidates include:
- serial_number;
- maintenance_interval;
- alarm_code;
- work_order_number;
- advanced table-extraction fields;
- richer relationship graphs;
- contradiction-resolution evidence models;
- extended operational-mode and operational-phase refinement.

## 29. Revision history
- **v1.0 design approval — 2026-07-31**: formalized architecture boundary, source-of-truth model, stable-ID policy, human-review boundary, Chroma boundary principles, registry-first migration principle, and generated-content exclusion rule.

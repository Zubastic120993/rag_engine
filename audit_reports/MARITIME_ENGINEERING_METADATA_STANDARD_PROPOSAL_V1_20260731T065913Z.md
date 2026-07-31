# Maritime Engineering Metadata Standard Proposal v1 — 20260731T065913Z

## Design intent
Build a metadata standard suitable for a Chief Engineer knowledge system that must support:
- vessel separation;
- authority-aware retrieval;
- document revision control;
- safe citations;
- equipment applicability;
- future relationship and knowledge-graph functions.

## Current-state anchor
Observed current production chunk metadata fields:
`['collection', 'page', 'source']`

Current design strength: simple, lightweight, and sufficient for source-page citations and coarse scope filtering.

Current design limitation: it is not a governed metadata model; it is a mixture of explicit path-derived fields and opportunistic loader metadata.

## Proposed metadata layers
| Metadata layer | Purpose |
| --- | --- |
| Document-level metadata | Stable identity, document control, applicability, authority, revision, language, source provenance. |
| Chunk-level metadata | Chunk identity, page/section coordinates, source traceability, retrieval-safe filtering. |
| Entity metadata | Vessel, equipment, maker, model, component, certificate, defect, PMS, requisition canonical entities. |
| Relationship metadata | Supersession, evidence links, applicability links, event/document joins. |
| Authority and governance metadata | Why a source is preferred, approval state, verification, confidentiality, conflict flags. |
| Ingestion/provenance metadata | Extractor, version, OCR, timestamps, confidence, review state. |
| Runtime/retrieval metadata | Distances, scopes requested/resolved, answer gates, benchmark outcomes, event logs. |
| Obsidian/knowledge-graph metadata | Note hubs, backlinks, evidence registers, derived summaries, user-approved knowledge. |

## Recommended architecture option
**Recommended target architecture: Option D** — Chroma plus SQLite registry plus Obsidian graph layer.

**Recommended v1 implementation core: Option C boundary first** — Chroma plus structured SQLite metadata registry, with Obsidian treated as a derived/working layer connected through stable IDs.

Rationale:
- Chroma should remain optimized for embeddings and retrieval.
- Structured SQLite metadata should become the governed source of truth for document, version, applicability, authority, and relationship metadata.
- Obsidian should carry note-level knowledge and human-approved synthesis, not replace the governed registry.

## Field-layer model
### Document-level metadata
Identity, revision, authority, applicability, and document-control facts that should not be repeated independently per chunk.

### Chunk-level metadata
Minimal retrieval-safe coordinates and traceability fields required to tie answer fragments back to the correct document version and page/section.

### Entity metadata
Canonical entities such as vessel, equipment, maker, model, component, defect, PMS job, requisition, certificate, and correspondence thread.

### Relationship metadata
Explicit link records such as supersedes, applies_to, references, evidences, linked_to_defect, linked_to_requisition, linked_to_pms, linked_to_note, and linked_to_photo.

### Authority and governance metadata
Fields explaining why a source may outrank another in a given operational question.

### Ingestion/provenance metadata
Fields describing how a record was extracted, enriched, reviewed, and versioned.

### Runtime/retrieval metadata
Fields produced at query time or benchmark time, not persisted as core document truth.

### Obsidian/knowledge-graph metadata
Note hubs, evidence maps, and derived links that reference governed document/entity IDs.

## Candidate-field assessment summary
### Required now (v1 mandatory)
document_id, document_version_id, chunk_id, source_hash, content_hash, relative_path, filename, file_extension, page_number, page_label, chunk_index, scope, vessel_name, fleet_applicability, system, equipment, maker, model, document_type, revision, effective_date, status, authority_class, issuer, approval_status, verification_status, ingestion_timestamp, ingestion_version, extractor, metadata_extraction_method, metadata_confidence, human_review_status

### Optional now
- vessel_imo
- vessel_class
- sister_vessel_group
- generic_applicability
- subsystem
- equipment_tag
- component
- issue_date
- expiry_date
- language
- safety_criticality
- service_letter_number
- certificate_number
- event_date
- related_document_ids
- source_note_id
- ocr_used
- ocr_confidence
- human_reviewed_at
- human_reviewed_by

### Future / v2
- serial_number
- maintenance_interval
- alarm_code
- work_order_number
- table_extraction_used
- related_equipment_ids
- related_defect_ids
- related_requisition_ids
- related_pms_job_ids
- related_correspondence_ids
- related_photo_ids
- related_note_ids
- relationship confidence weighting
- contradiction-resolution evidence graph

### Rejected or represented elsewhere
- raw absolute file path as canonical identity field — reject; use governed `relative_path` plus stable IDs.
- embedding distance as stored document truth — represent in runtime/retrieval layer only.
- copied full manual content in Obsidian — reject.

## Controlled vocabularies
| Vocabulary | Governance | Example values |
| --- | --- | --- |
| scope | centrally governed | me-c, maker-manuals, regulatory, inspection, sms, vessels, wiki, career, rules, other |
| document_type | centrally governed with extension review | maker_manual, service_letter, vessel_procedure, company_instruction, pms_record, defect_report, requisition, correspondence, certificate, sds, drawing, work_report, handover_note, wiki_note, media_record |
| authority_class | centrally governed | statutory, flag_state, class, company_sms, vessel_approved, maker_manual, maker_service_letter, correspondence, pms_record, defect_record, work_report, handover_note, working_note, ai_derivative |
| approval_status | centrally governed | approved, conditionally_approved, draft, unapproved, superseded, unknown |
| status | centrally governed | active, superseded, obsolete, draft, archived, unknown |
| verification_status | centrally governed | verified, partially_verified, unverified, conflicting, not_applicable |
| safety_criticality | centrally governed | critical, high, medium, low, unknown |
| fleet_applicability | centrally governed | single_vessel, sister_group, fleet_wide, generic, unknown |
| operational_mode | extensible with central review | gas_mode, fuel_oil_mode, dual_fuel, harbour, sea_passage, emergency, maintenance, unknown |
| operational_phase | extensible with central review | manoeuvring, startup, shutdown, bunkering, cargo_ops, normal_running, emergency_response, unknown |
| extraction_method | centrally governed | pdf_loader, text_loader, ocr, table_extractor, manual_entry, derived_relation |
| human_review_status | centrally governed | not_reviewed, review_required, reviewed, approved, rejected |
| relationship_type | centrally governed | supersedes, references, applies_to, evidences, linked_to_defect, linked_to_pms, linked_to_requisition, linked_to_note, linked_to_photo |

Handling rules:
- `unknown` means not yet known.
- `not_applicable` means the field does not apply to this record class.
- `ambiguous` means evidence conflicts or identity is not resolved safely.
- `conflicting` means multiple candidate values remain unresolved.
- multiple values require explicit list cardinality, not comma-packed strings.
- inherited values must record source of inheritance.
- human-overridden values must preserve original extracted value and override provenance.

## Authority and conflict model
The metadata model must distinguish and rank at least these classes:
1. statutory/regulatory source
2. flag-state requirement
3. classification-society requirement
4. company/SMS instruction
5. vessel-approved procedure
6. maker instruction
7. maker service letter
8. technical correspondence
9. PMS record
10. defect report
11. work report
12. Chief Engineer handover
13. personal/working note
14. AI-generated derivative note

Authority is **not** one universal ranking. Preference must depend on:
- question type;
- vessel applicability;
- revision/effective date;
- approval state;
- safety criticality;
- source completeness;
- contradiction status.

Required fields to explain preference:
- `authority_class`
- `issuer`
- `approval_status`
- `verification_status`
- `effective_date`
- `status`
- `fleet_applicability`
- `vessel_name`
- `maker_authoritative` or equivalent authority flags
- `source_completeness` (future)
- `conflict_flag` (future)

## Stable identifier requirements
Separate stable-ID strategies are required for:
- document identity
- document version identity
- chunk identity
- equipment identity
- vessel identity
- relationship identity

Recommended direction:
- `document_id`: canonical identity independent of path; preferably UUID or registry key bound to business identity, not raw path.
- `document_version_id`: stable per content/revision/version, combining `document_id` with version lineage or content hash.
- `chunk_id`: derived from `document_version_id + chunk_index + chunking_version`, not opaque Chroma-generated UUID only.
- `source_hash`: file-byte hash of the source artifact.
- `content_hash`: normalized text/content hash of the document or chunk.

## Obsidian boundary
Future metadata must connect source documents to:
- equipment hub
- vessel hub
- maker/model page
- defect page
- PMS job
- requisition
- service letter
- event timeline
- photo
- correspondence
- procedure
- evidence citation

Boundary rule:
- authoritative source metadata belongs in governed registries;
- Obsidian note metadata belongs in note files;
- generated relationship indexes belong in derived graph output;
- AI summaries must be marked derivative and linked back to authoritative evidence;
- user-approved knowledge must remain distinguishable from automatic inference.

## Recommended v1 scope
### V1 mandatory fields
Stable identity, vessel separation, authority class, revision/status, equipment applicability, safe citation coordinates, and ingestion provenance.

### V1 optional fields
Useful but not mandatory for first safe implementation; add when extraction or review is reliable.

### Deferred v2 fields
Advanced relationship, alarm, interval, serial, and richer operational-context fields after the registry boundary is proven.

### Fields requiring human review
- approval status
- authority class in ambiguous cases
- vessel applicability where path/name disagree
- supersession relationships
- equipment/component resolution for complex packages
- note/source relationship approval

### Fields unsuitable for automatic inference alone
- company_approved
- vessel_approved
- verification_status
- superseded_by / supersedes
- safety_criticality where not explicitly stated
- defect / PMS / requisition relationships

## Open design decisions for formal review
- exact canonical vessel naming and IMO policy;
- whether `document_id` is registry UUID-first or business-key-first;
- minimum required human review for authority/applicability resolution;
- confidentiality treatment for correspondence, defect, requisition, and photo relationships;
- whether Obsidian generated graph files should remain excluded from production ingest permanently.

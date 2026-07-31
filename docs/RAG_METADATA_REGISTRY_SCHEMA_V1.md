# RAG Metadata Registry Schema v1

Revision date: 2026-07-31
Status: Logical schema specification only

This document defines the logical SQLite registry schema for Metadata Standard v1.

No production database is created by this document.

## Design principles
- SQLite registry is authoritative for governed metadata.
- Chroma remains the embedding and chunk index.
- Registry records must preserve provenance and audit trace.
- Confidentiality-sensitive relationships must store references, not unnecessary body text.
- Logical schema is normalized enough for auditability and filterability, but practical for maritime operations.

## 1. `documents`
### Purpose
Logical document identity independent of path and file movement.

### Primary key
- `document_id` TEXT PRIMARY KEY

### Mandatory fields
- `document_id`
- `document_type`
- `scope`
- `created_at`
- `updated_at`

### Optional fields
- `canonical_title`
- `document_number`
- `default_authority_id`
- `notes`

### Foreign keys
- `default_authority_id` → `authorities.authority_id`

### Uniqueness constraints
- optional unique business key may later combine `document_number` + issuer when approved

### Indexes
- `(document_type)`
- `(scope)`
- `(document_number)`

### Deletion policy
Soft-delete not approved in v1. Documents should remain and be marked by status in version records.

### Update policy
`document_id` immutable. Descriptive fields updateable with audit trail.

### Provenance fields
- `created_by_run_id`
- `updated_by_run_id`

### Audit fields
- `created_at`
- `updated_at`

### Confidentiality considerations
No confidential document body text stored here.

## 2. `document_versions`
### Purpose
Exact version-level metadata for one document artifact/version.

### Primary key
- `document_version_id` TEXT PRIMARY KEY

### Foreign keys
- `document_id` → `documents.document_id`
- `source_file_id` → `source_files.source_file_id`
- `authority_id` → `authorities.authority_id`

### Mandatory fields
- `document_version_id`
- `document_id`
- `source_file_id`
- `source_hash`
- `relative_path`
- `filename`
- `file_extension`
- `status`
- `authority_id`
- `approval_status`
- `verification_status`
- `ingestion_timestamp`
- `ingestion_version`
- `metadata_confidence`
- `human_review_status`
- `ingestion_run_id`
- `created_at`
- `updated_at`

### Optional fields
- `revision`
- `issue_date`
- `effective_date`
- `expiry_date`
- `document_number`
- `language`
- `supersedes_document_version_id`
- `superseded_by_document_version_id`
- `title`
- `confidentiality_class`

### Uniqueness constraints
- `(document_id, source_hash)` unique
- optional `(document_id, revision, status)` unique only where data quality permits

### Indexes
- `(document_id)`
- `(status)`
- `(approval_status)`
- `(verification_status)`
- `(revision)`
- `(effective_date)`
- `(source_hash)`

### Deletion policy
Do not hard-delete operational history in normal workflow. Mark obsolete/superseded status instead.

### Update policy
Version identity immutable after creation. Status and review fields updateable with audit trail.

### Provenance fields
- `ingestion_run_id`
- `ingestion_timestamp`
- `ingestion_version`
- `metadata_source_type`
- `extractor`
- `extractor_version`

### Audit fields
- `created_at`
- `updated_at`

### Confidentiality considerations
Store only version metadata, not document body text.

## 3. `source_files`
### Purpose
Track physical file identity and storage-specific facts without making path the logical identity.

### Primary key
- `source_file_id` TEXT PRIMARY KEY

### Mandatory fields
- `source_file_id`
- `source_hash`
- `relative_path`
- `filename`
- `file_extension`
- `file_size_bytes`
- `first_seen_at`
- `last_seen_at`

### Optional fields
- `content_hash`
- `path_changed_at`
- `is_current_path`
- `storage_root`

### Uniqueness constraints
- `(source_hash, relative_path)` unique

### Indexes
- `(source_hash)`
- `(relative_path)`
- `(filename)`

### Deletion policy
Preserve history; do not delete path history casually.

### Update policy
Relative path may update; path history should remain auditable through new rows or historical linkage.

### Provenance fields
- `observed_by_run_id`

### Audit fields
- `first_seen_at`
- `last_seen_at`

### Confidentiality considerations
Path may reveal location sensitivity; avoid exporting unnecessarily.

## 4. `vessels`
### Purpose
Canonical vessel identity registry.

### Primary key
- `vessel_id` TEXT PRIMARY KEY

### Mandatory fields
- `vessel_id`
- `vessel_name`
- `vessel_imo`
- `created_at`
- `updated_at`

### Optional fields
- `vessel_class`
- `flag_state`
- `sister_vessel_group`
- `status`

### Uniqueness constraints
- `vessel_imo` unique

### Indexes
- `(vessel_name)`
- `(vessel_imo)`
- `(sister_vessel_group)`

### Deletion policy
Do not delete canonical vessel rows used by document links.

### Update policy
`vessel_id` immutable. Name/class/flag may update with audit trail.

### Provenance fields
- `created_by_run_id`

### Audit fields
- `created_at`
- `updated_at`

### Confidentiality considerations
No sensitive personal data.

## 5. `document_vessel_applicability`
### Purpose
Governed applicability between document versions and vessels.

### Primary key
- `document_vessel_applicability_id` TEXT PRIMARY KEY

### Foreign keys
- `document_version_id` → `document_versions.document_version_id`
- `vessel_id` → `vessels.vessel_id`
- `review_id` → `human_reviews.review_id`

### Mandatory fields
- `document_vessel_applicability_id`
- `document_version_id`
- `fleet_applicability`
- `verification_status`
- `human_review_status`
- `created_at`

### Optional fields
- `vessel_id`
- `sister_vessel_group`
- `generic_applicability`
- `operational_mode`
- `operational_phase`
- `review_id`

### Uniqueness constraints
- one active applicability row per `(document_version_id, vessel_id, fleet_applicability, operational_mode, operational_phase)`

### Indexes
- `(document_version_id)`
- `(vessel_id)`
- `(fleet_applicability)`

### Deletion policy
Supersede or close records; do not silently delete approved applicability history.

### Update policy
Approval-related changes require audit trail.

### Provenance fields
- `metadata_source_type`
- `assertion_source`

### Audit fields
- `created_at`
- `updated_at`

### Confidentiality considerations
Low, unless linked to sensitive operational records.

## 6. `authorities`
### Purpose
Canonical authority and issuer records.

### Primary key
- `authority_id` TEXT PRIMARY KEY

### Mandatory fields
- `authority_id`
- `authority_class`
- `issuer_name`
- `created_at`

### Optional fields
- `issuer_code`
- `maker_authoritative`
- `statutory_authority`
- `classification_society`
- `notes`

### Uniqueness constraints
- `(authority_class, issuer_name)` unique where practical

### Indexes
- `(authority_class)`
- `(issuer_name)`

### Deletion policy
Do not delete canonical authorities referenced by version rows.

### Update policy
Update descriptive fields with audit trail.

### Provenance fields
- `created_by_run_id`

### Audit fields
- `created_at`
- `updated_at`

### Confidentiality considerations
None beyond normal business sensitivity.

## 7. `equipment_entities`
### Purpose
Canonical equipment/system/maker/model identity.

### Primary key
- `equipment_id` TEXT PRIMARY KEY

### Mandatory fields
- `equipment_id`
- `equipment_name`
- `created_at`

### Optional fields
- `system`
- `subsystem`
- `maker`
- `model`
- `equipment_tag`
- `serial_number`
- `component`

### Uniqueness constraints
- no single universal uniqueness rule approved in v1; use controlled matching and review

### Indexes
- `(equipment_name)`
- `(maker)`
- `(model)`
- `(system)`

### Deletion policy
Preserve referenced entities.

### Update policy
Merge/correction requires audit trail and review.

### Provenance fields
- `created_by_run_id`

### Audit fields
- `created_at`
- `updated_at`

### Confidentiality considerations
Serial numbers may be sensitive; treat as optional and controlled.

## 8. `document_equipment_applicability`
### Purpose
Link document versions to equipment entities.

### Primary key
- `document_equipment_applicability_id` TEXT PRIMARY KEY

### Foreign keys
- `document_version_id` → `document_versions.document_version_id`
- `equipment_id` → `equipment_entities.equipment_id`
- `review_id` → `human_reviews.review_id`

### Mandatory fields
- `document_equipment_applicability_id`
- `document_version_id`
- `equipment_id`
- `verification_status`
- `human_review_status`
- `created_at`

### Optional fields
- `applicability_note`
- `review_id`

### Uniqueness constraints
- `(document_version_id, equipment_id)` unique

### Indexes
- `(document_version_id)`
- `(equipment_id)`

### Deletion policy
Close or supersede instead of silent deletion where reviewed.

### Update policy
Reviewed associations require audit trail.

### Provenance fields
- `metadata_source_type`

### Audit fields
- `created_at`
- `updated_at`

### Confidentiality considerations
Low.

## 9. `relationships`
### Purpose
Governed cross-record relationship store.

### Primary key
- `relationship_id` TEXT PRIMARY KEY

### Mandatory fields
- `relationship_id`
- `relationship_type`
- `source_record_type`
- `source_record_id`
- `target_record_type`
- `target_record_id`
- `verification_status`
- `human_review_status`
- `confidentiality_class`
- `created_at`

### Optional fields
- `event_date`
- `notes`
- `review_id`

### Foreign keys
- `review_id` → `human_reviews.review_id`

### Uniqueness constraints
- `(relationship_type, source_record_type, source_record_id, target_record_type, target_record_id)` unique

### Indexes
- `(source_record_type, source_record_id)`
- `(target_record_type, target_record_id)`
- `(relationship_type)`
- `(confidentiality_class)`

### Deletion policy
Do not delete approved operational relationships without corrective audit trace.

### Update policy
Verification and review state updateable; endpoints immutable except via replacement.

### Provenance fields
- `metadata_source_type`
- `assertion_source`

### Audit fields
- `created_at`
- `updated_at`

### Confidentiality considerations
Store references only, not unnecessary body text.

## 10. `metadata_assertions`
### Purpose
Preserve extracted or inferred assertions separately from final governed accepted fields where needed.

### Primary key
- `assertion_id` TEXT PRIMARY KEY

### Mandatory fields
- `assertion_id`
- `record_type`
- `record_id`
- `field_name`
- `asserted_value`
- `metadata_source_type`
- `assertion_status`
- `metadata_confidence`
- `created_at`

### Optional fields
- `source_file_id`
- `review_id`
- `superseded_assertion_id`

### Foreign keys
- `source_file_id` → `source_files.source_file_id`
- `review_id` → `human_reviews.review_id`

### Uniqueness constraints
- none beyond primary key; multiple assertions may exist for one field

### Indexes
- `(record_type, record_id)`
- `(field_name)`
- `(assertion_status)`

### Deletion policy
Prefer assertion supersession over delete.

### Update policy
Assertions immutable; state changes via superseding or review linkage.

### Provenance fields
Intrinsic to table purpose.

### Audit fields
- `created_at`

### Confidentiality considerations
Avoid full confidential body text unless explicitly approved.

## 11. `ingestion_runs`
### Purpose
Track registry population or enrichment runs.

### Primary key
- `ingestion_run_id` TEXT PRIMARY KEY

### Mandatory fields
- `ingestion_run_id`
- `ingestion_version`
- `started_at`
- `status`

### Optional fields
- `completed_at`
- `source_scope`
- `extractor`
- `extractor_version`
- `notes`

### Uniqueness constraints
- none beyond primary key

### Indexes
- `(started_at)`
- `(status)`
- `(ingestion_version)`

### Deletion policy
Do not delete audit history casually.

### Update policy
Run status may update until completion.

### Provenance fields
Table itself is provenance.

### Audit fields
- `started_at`
- `completed_at`

### Confidentiality considerations
No confidential content required.

## 12. `human_reviews`
### Purpose
Track human approvals, rejections, corrections, and reviewer role decisions.

### Primary key
- `review_id` TEXT PRIMARY KEY

### Mandatory fields
- `review_id`
- `reviewer_role`
- `review_status`
- `reviewed_at`

### Optional fields
- `reviewer_identifier`
- `review_scope`
- `notes`
- `supersedes_review_id`

### Uniqueness constraints
- none beyond primary key

### Indexes
- `(reviewer_role)`
- `(review_status)`
- `(reviewed_at)`

### Deletion policy
Do not delete approval history.

### Update policy
Reviews are append-oriented; corrections should create superseding reviews.

### Provenance fields
Intrinsic to table purpose.

### Audit fields
- `reviewed_at`

### Confidentiality considerations
Use role-based identity where practical; do not store unnecessary personal data.

## 13. `controlled_vocabulary_values`
### Purpose
Govern controlled vocabularies, aliases, and deprecations.

### Primary key
- `vocabulary_value_id` TEXT PRIMARY KEY

### Mandatory fields
- `vocabulary_value_id`
- `vocabulary_name`
- `canonical_value`
- `label`
- `status`
- `created_at`

### Optional fields
- `permitted_aliases_json`
- `deprecated_aliases_json`
- `multiple_allowed`
- `inheritance_allowed`
- `human_approval_required`
- `notes`

### Uniqueness constraints
- `(vocabulary_name, canonical_value)` unique

### Indexes
- `(vocabulary_name)`
- `(status)`

### Deletion policy
Deprecate rather than delete where already referenced.

### Update policy
Canonical value stable; alias lists updateable with audit trail.

### Provenance fields
- `created_by_run_id`

### Audit fields
- `created_at`
- `updated_at`

### Confidentiality considerations
None.

## 14. `registry_schema_version`
### Purpose
Track schema version and migration compatibility.

### Primary key
- `schema_version` INTEGER PRIMARY KEY

### Mandatory fields
- `schema_version`
- `applied_at`
- `status`

### Optional fields
- `description`
- `backward_compatible`

### Uniqueness constraints
- one row per schema version

### Indexes
- `(applied_at)`

### Deletion policy
Do not delete schema history.

### Update policy
Append new version rows; do not rewrite history.

### Provenance fields
- `applied_by_run_id`

### Audit fields
- `applied_at`

### Confidentiality considerations
None.

## Cross-table policy notes
### Deletion policy
Default posture is preserve-and-supersede, not silent delete.

### Update policy
Stable identifiers are immutable. Review, approval, and status changes must remain auditable.

### Provenance policy
Every governed row should be traceable to ingestion or review context.

### Audit policy
Created/updated timestamps are required on governed mutable records.

### Confidentiality policy
Confidential operational text should remain in the source file, not be duplicated unnecessarily in registry relationship structures.

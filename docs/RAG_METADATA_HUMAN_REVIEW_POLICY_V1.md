# RAG Metadata Human Review Policy v1

Revision date: 2026-07-31
Status: Approved for v1 design

## Purpose
Define which metadata fields require human approval, which may be automatically accepted, and how review state, audit trail, correction, and revocation must be represented.

## Reviewer identity model
Prefer role-based reviewer identity values:
- `chief_engineer`
- `technical_superintendent`
- `library_administrator`
- `system_migration`

Optional reviewer identifier may be stored only when necessary and approved.

## Fields requiring human approval
Human approval is mandatory for:
- vessel applicability
- sister-vessel applicability
- authority_class
- approval_status
- supersession relationships
- ambiguous revision
- ambiguous document type
- confidentiality classification where relationships involve correspondence, defects, requisitions, or photos
- relationship validation between operational records

## Fields eligible for automatic acceptance
Automatic acceptance may be permitted for low-risk fields when deterministic evidence is strong, such as:
- relative_path
- filename
- file_extension
- source_hash
- ingestion_timestamp
- ingestion_version
- extractor name/version
- page_number from current extraction pipeline
- scope when path rule is unambiguous and later review does not contradict it

Automatic acceptance is not the same as human approval.

## Confidence thresholds
The future implementation may use confidence thresholds to route items to review, but the policy requirement is:
- high confidence does not override a mandatory review field;
- low confidence must trigger `review_required`;
- conflicting evidence must not be auto-accepted.

## Ambiguous-value handling
When more than one plausible value exists:
- store the assertion as `ambiguous`;
- preserve provenance for each candidate value;
- require human review before promoting to governed accepted metadata.

## Conflicting-value handling
When evidence sources disagree:
- store contradiction state as `conflicting`;
- preserve provenance;
- avoid silent overwrite;
- require human review or escalation.

## Review states
Approved review states:
- `not_reviewed`
- `review_required`
- `under_review`
- `approved`
- `rejected`
- `corrected`
- `revoked`

## Review timestamps
Every human review record must preserve:
- `reviewed_at`
- reviewer role
- review status
- affected record or assertion identity

## Change history
Review history is append-oriented. Corrections should create superseding review events rather than erase older review evidence.

## Revocation and correction
If a previously approved value is later found incorrect:
- create a corrective review record;
- mark the affected value corrected or revoked;
- preserve prior review history;
- update the active governed value through approved replacement, not silent deletion.

## Audit trail
A future implementation must preserve:
- review ID
- reviewer role
- review time
- review status
- affected fields or relationships
- rationale or notes where required

## Bulk approval restrictions
Bulk approval is restricted.

Not approved for bulk acceptance without explicit workflow controls:
- vessel applicability
- authority_class
- approval_status
- supersession relationships
- confidentiality classification
- operational-record relationships

## Vessel-applicability approval
Vessel applicability requires explicit review whenever:
- path-derived vessel inference could be wrong;
- sister-vessel applicability is claimed;
- a generic document is being narrowed to a specific vessel;
- a vessel-specific document may have broader applicability.

## Authority approval
Authority class requires human approval when:
- source class is not explicit;
- correspondence may be mistaken for approved instruction;
- company instruction and vessel practice are mixed;
- note and source-document boundaries are unclear.

## Supersession approval
Supersession relationships require human approval when:
- revision markers are inconsistent;
- no explicit superseding statement exists;
- two active-looking documents coexist;
- maker and company versions overlap ambiguously.

## Relationship approval
Operational-record relationships require human approval when linking:
- defects to PMS
- defects to requisitions
- defects to correspondence
- defects to photos
- work reports to corrective actions

## Confidentiality approval
Confidentiality classification requires approval whenever relationship records touch:
- correspondence
- defects
- requisitions
- photos
- mixed operational evidence packages

## Sensitive data minimization
Do not store unnecessary personal data in review records. Role-based reviewer identity is preferred.

## Design restriction
This policy approves the review boundary only. It does not approve review UI, workflow code, or production state changes in Step 6.

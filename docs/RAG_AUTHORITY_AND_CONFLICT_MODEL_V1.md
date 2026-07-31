# RAG Authority and Conflict Model v1

Revision date: 2026-07-31
Status: Approved for v1 design

## Purpose
Define how the metadata system represents authority classes and explains source preference without using one universal absolute ranking.

## Authority classes in scope
The system must represent at minimum:
1. statutory/regulatory source
2. flag-state source
3. classification-society source
4. company/SMS instruction
5. vessel-approved procedure
6. maker manual
7. maker service letter
8. technical correspondence
9. PMS record
10. defect report
11. work report
12. handover note
13. personal working note
14. AI-generated derivative note

## Core principle
Authority preference depends on the question being asked.

Examples:
- statutory compliance question: statutory / flag / class sources may outrank maker guidance
- maker maintenance method question: maker manual or maker service letter may outrank generic notes
- vessel-specific operating practice question: approved vessel procedure may outrank generic maker instruction if it governs onboard execution within approved authority boundaries

## Required metadata to explain preference
A source-preference decision must be explainable using metadata such as:
- `authority_class`
- `issuer`
- `approval_status`
- `verification_status`
- `status`
- `revision`
- `effective_date`
- `fleet_applicability`
- `vessel_id`
- `equipment_id`
- `maker`
- `model`
- `safety_criticality`
- `human_review_status`
- contradiction indicators

## Preference factors
### 1. Applicability
A highly authoritative source that does not apply to the vessel, equipment, or operating mode must not automatically outrank an applicable one.

### 2. Authority class
Authority class describes the nature of the source, not a fixed numeric score.

### 3. Approval status
Approved sources generally outrank draft or unapproved sources within the same question context.

### 4. Revision and effective date
A current effective source generally outranks a superseded one.

### 5. Vessel specificity
Where safe and approved, vessel-specific reviewed procedures may outrank generic references for onboard practice.

### 6. Equipment and model match
Model-specific maker information may outrank generic family-level guidance for equipment-specific maintenance questions.

### 7. Source completeness
A partial or fragmentary source may require support from another source and must not be overstated.

### 8. Safety criticality
For safety-critical questions, stronger verified and approved sources are required.

### 9. Human verification
Human-reviewed assertions may outrank raw automatic inference.

### 10. Contradiction status
If sources conflict, the system must preserve the conflict and show why a preference was made or why escalation is required.

## Suggested decision logic
The system should evaluate sources in this order:
1. is the source applicable?
2. is the source current and not superseded?
3. what authority class does it represent?
4. what is its approval status?
5. is it verified or only inferred?
6. is it model- and vessel-matched?
7. is there contradiction with another applicable source?
8. does the question require escalation due to unresolved conflict?

## Conflict handling model
When conflict is detected:
- preserve both sources;
- preserve their metadata;
- mark contradiction status;
- avoid silent reconciliation;
- prefer the reviewed, applicable, current, and question-appropriate source where approved;
- require escalation where conflict remains unresolved.

## Metadata records needed
### Authority records
Authority issuer and class should be normalized in `authorities`.

### Review records
Human approvals and conflict resolutions should be recorded in `human_reviews`.

### Assertion records
Competing extracted values may coexist in `metadata_assertions` until approved.

### Relationship records
Contradiction or supersession links should be explicit relationships where appropriate.

## Non-authoritative materials
The following are not inherently authoritative for source-document truth:
- raw path location;
- filename pattern alone;
- AI-generated summary;
- unreviewed derivative note;
- unchecked correspondence excerpt;
- copied text without provenance.

## Escalation conditions
Escalation or refusal should occur when:
- vessel applicability is unresolved;
- authority class is unresolved;
- supersession is unresolved;
- revision status is unresolved for a compliance-critical question;
- confidentiality boundary prevents direct evidence comparison;
- a safety-critical question has conflicting reviewed sources.

## Preferred output requirement for future retrieval behavior
The future system should be able to explain preference in plain terms, for example:
- source A preferred because it is current company-approved vessel procedure applicable to vessel X;
- source B retained as supporting maker reference;
- source C excluded because it is superseded;
- source D flagged as conflicting correspondence pending review.

## Design restriction
This document approves the metadata model only. It does not approve automated ranking code changes in Step 6.

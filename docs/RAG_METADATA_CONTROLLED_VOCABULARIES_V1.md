# RAG Metadata Controlled Vocabularies v1

Revision date: 2026-07-31
Status: Approved for v1 design

Machine values are lower-case.
Human-facing labels may use title case in UI.

## General rules
- Canonical values are the only stored values in governed records.
- Permitted aliases may be accepted during extraction or migration, but must normalize to the canonical value.
- Deprecated aliases must not be written as canonical values.
- `unknown`, `not_applicable`, `ambiguous`, and `conflicting` are semantic states and must not be merged.

---

## 1. `scope`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: only when automatic scope assignment conflicts with reviewed applicability

| Canonical value | Meaning | Permitted aliases | Deprecated aliases | Unknown | Not applicable | Ambiguous | Conflicting |
| --- | --- | --- | --- | --- | --- | --- | --- |
| generic | generic non-vessel-specific applicability | general | other | supported | not used | supported | supported |
| me-c | main-engine related engineering corpus | mec | me_c | supported | not used | supported | supported |
| maker-manuals | maker manuals and technical publications | maker_manuals, manuals | maker | supported | not used | supported | supported |
| regulatory | statutory and regulatory corpus | imo, statutory | rules_only | supported | not used | supported | supported |
| inspection | inspection / vetting / CDI / SIRE corpus | sire, cdi | vetting_only | supported | not used | supported | supported |
| sms | company SMS / IMM corpus | sms_library | company_sms | supported | not used | supported | supported |
| vessels | vessel-specific source corpus | vessel_docs | ship | supported | not used | supported | supported |
| wiki | approved CE Wiki / note corpus | ce_wiki | notes_only | supported | not used | supported | supported |
| career | general career and technical reference corpus | reference | reference_docs | supported | not used | supported | supported |
| rules | local rules corpus if used separately | local_rules | rulebook | supported | not used | supported | supported |

---

## 2. `document_type`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: yes when ambiguous

| Canonical value | Meaning | Permitted aliases | Deprecated aliases | Unknown | Not applicable | Ambiguous | Conflicting |
| --- | --- | --- | --- | --- | --- | --- | --- |
| maker_manual | maker instruction manual | manual, instruction_manual | maker_doc | supported | supported | supported | supported |
| maker_service_letter | maker service letter / bulletin | service_letter, bulletin | sl_only | supported | supported | supported | supported |
| vessel_procedure | vessel-approved or vessel-specific procedure | onboard_procedure | vessel_instruction | supported | supported | supported | supported |
| company_instruction | company / SMS / IMM instruction | sms_instruction, imm_procedure | company_doc | supported | supported | supported | supported |
| pms_record | PMS or maintenance record | maintenance_record | pms_job | supported | supported | supported | supported |
| defect_report | defect report | defect | defect_note | supported | supported | supported | supported |
| requisition | requisition or spare-parts request record | spare_request | req | supported | supported | supported | supported |
| correspondence | technical correspondence | letter, email | mail | supported | supported | supported | supported |
| certificate | certificate or statutory certificate | statutory_document | cert_only | supported | supported | supported | supported |
| sds | safety data sheet | msds | datasheet_sds | supported | supported | supported | supported |
| drawing | drawing or diagram | diagram | plan | supported | supported | supported | supported |
| work_report | work / inspection / attendance report | inspection_report | report_generic | supported | supported | supported | supported |
| handover_note | handover document or note | handover | shift_handover | supported | supported | supported | supported |
| wiki_note | governed knowledge note | note | obsidian_note | supported | supported | supported | supported |
| media_record | photo or media-linked evidence record | photo, image_record | media_only | supported | supported | supported | supported |

---

## 3. `authority_class`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: yes

| Canonical value | Meaning | Permitted aliases | Deprecated aliases | Unknown | Not applicable | Ambiguous | Conflicting |
| --- | --- | --- | --- | --- | --- | --- | --- |
| statutory | statutory / convention / code requirement | regulatory | legal | supported | supported | supported | supported |
| flag_state | flag administration requirement | flag | administration | supported | supported | supported | supported |
| class | classification-society requirement | classification_society | class_rule | supported | supported | supported | supported |
| company_sms | company / SMS / IMM requirement | sms, company | office_rule | supported | supported | supported | supported |
| vessel_approved | vessel-approved onboard procedure | onboard_approved | vessel_specific | supported | supported | supported | supported |
| maker_manual | maker instruction manual authority | maker | maker_instruction | supported | supported | supported | supported |
| maker_service_letter | maker service-letter authority | service_letter | maker_bulletin | supported | supported | supported | supported |
| correspondence | technical correspondence authority | letter, email | mail | supported | supported | supported | supported |
| pms_record | PMS record authority class | maintenance_record | pms | supported | supported | supported | supported |
| defect_record | defect-report authority class | defect | defect_note | supported | supported | supported | supported |
| work_report | work-report authority class | inspection_report | report | supported | supported | supported | supported |
| handover_note | handover authority class | handover | watch_handover | supported | supported | supported | supported |
| working_note | personal or working note | note | personal_note | supported | supported | supported | supported |
| ai_derivative | AI-generated derivative note | ai_note | generated_note | supported | supported | supported | supported |

---

## 4. `approval_status`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: yes

| Canonical value | Meaning | Permitted aliases | Deprecated aliases |
| --- | --- | --- | --- |
| approved | formally approved for use | accepted | final |
| conditionally_approved | approved with stated condition or boundary | limited_approval | provisional_approved |
| draft | not approved final issue | proposed | working_draft |
| unapproved | not approved | not_approved | rejected_for_use |
| superseded | replaced by a newer approved issue | obsolete_by_newer | old_approved |
| unknown | approval not yet known | pending_unknown | none |
| not_applicable | approval concept not applicable | n_a | n/a |
| ambiguous | conflicting or unclear approval evidence | unclear | mixed |
| conflicting | explicit contradictory approval evidence | contradiction | disputed |

---

## 5. `document_status`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: for superseded / obsolete decisions when ambiguous

Canonical values:
- active
- superseded
- obsolete
- draft
- archived
- unknown
- not_applicable
- ambiguous
- conflicting

Permitted aliases:
- active: current
- superseded: replaced
- obsolete: withdrawn
- archived: retained_history

---

## 6. `verification_status`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: yes when marking `verified` for governed sensitive fields

Canonical values:
- verified
- partially_verified
- unverified
- not_applicable
- ambiguous
- conflicting
- unknown

Permitted aliases:
- verified: confirmed
- partially_verified: partly_confirmed
- unverified: unchecked

---

## 7. `human_review_status`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: state itself reflects review workflow

Canonical values:
- not_reviewed
- review_required
- under_review
- approved
- rejected
- corrected
- revoked

Permitted aliases:
- not_reviewed: pending
- approved: accepted
- rejected: refused

---

## 8. `safety_criticality`
Multiple values permitted: no
Inheritance permitted: yes only from approved authority source
Human approval required: yes when not explicit in source

Canonical values:
- critical
- high
- medium
- low
- unknown
- not_applicable
- ambiguous
- conflicting

---

## 9. `fleet_applicability`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: yes

Canonical values:
- single_vessel
- sister_group
- fleet_wide
- generic
- unknown
- not_applicable
- ambiguous
- conflicting

Permitted aliases:
- single_vessel: vessel_only
- fleet_wide: company_fleet
- generic: non_vessel_specific

---

## 10. `operational_mode`
Multiple values permitted: yes
Inheritance permitted: yes from approved procedure context
Human approval required: if inferred from context rather than stated

Canonical values:
- gas_mode
- fuel_oil_mode
- dual_fuel
- harbour
- sea_passage
- emergency
- maintenance
- unknown
- not_applicable
- ambiguous
- conflicting

---

## 11. `operational_phase`
Multiple values permitted: yes
Inheritance permitted: yes from approved procedure context
Human approval required: if inferred rather than stated

Canonical values:
- manoeuvring
- startup
- shutdown
- bunkering
- cargo_ops
- normal_running
- emergency_response
- maintenance
- unknown
- not_applicable
- ambiguous
- conflicting

---

## 12. `relationship_type`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: yes for operational-record links

Canonical values:
- supersedes
- superseded_by
- references
- applies_to_vessel
- applies_to_equipment
- evidences
- linked_to_defect
- linked_to_pms
- linked_to_requisition
- linked_to_correspondence
- linked_to_photo
- linked_to_note
- derived_from

Permitted aliases:
- linked_to_pms: linked_to_pms_job
- linked_to_note: source_note_link
- derived_from: generated_from

---

## 13. `confidentiality_class`
Multiple values permitted: no
Inheritance permitted: yes from stricter reviewed parent source
Human approval required: yes for sensitive operational relationships

Canonical values:
- public_internal
- operational_sensitive
- correspondence_sensitive
- defect_sensitive
- requisition_sensitive
- photo_sensitive
- restricted
- unknown
- not_applicable
- ambiguous
- conflicting

---

## 14. `metadata_source_type`
Multiple values permitted: yes
Inheritance permitted: no
Human approval required: no for simple provenance capture

Canonical values:
- explicit_stored_metadata
- path_derived
- filename_derived
- content_derived
- inferred
- human_approved
- migrated
- generated_relationship

Permitted aliases:
- explicit_stored_metadata: explicit
- content_derived: extracted_from_content
- human_approved: reviewed

---

## 15. `assertion_status`
Multiple values permitted: no
Inheritance permitted: no
Human approval required: only when promoted to governed accepted value

Canonical values:
- proposed
- accepted
- rejected
- superseded
- unknown
- ambiguous
- conflicting

Permitted aliases:
- proposed: candidate
- accepted: approved_assertion
- rejected: discarded

---

## Inheritance rules summary
Inheritance may be permitted only where explicitly stated above. Inherited values must preserve:
- source record identifier;
- inheritance reason;
- inherited-from field;
- reviewer decision where human approval was required.

## Multiple-value rules summary
Where multiple values are permitted, store them as structured related rows or approved arrays, not comma-packed strings.

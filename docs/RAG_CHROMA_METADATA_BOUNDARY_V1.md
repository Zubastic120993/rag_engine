# RAG Chroma Metadata Boundary v1

Revision date: 2026-07-31
Status: Approved for v1 design

## Purpose
Define which metadata belongs in Chroma and which metadata must remain governed in the SQLite registry.

## Boundary principle
Chroma metadata must remain compact, retrieval-focused, and synchronized from the authoritative registry where applicable.

Chroma must not become the primary store for:
- full authority logic
- rich relationship graphs
- large arrays of linked records
- confidential relationship detail
- human-review history

## Current production state verified in Step 5
Current production chunk metadata fields:
- `collection`
- `page`
- `source`

Current production chunk count:
- `108685`

## Future Chroma candidate fields
Assessed fields:
- `chunk_id`
- `document_version_id`
- `document_id`
- `source_hash`
- `relative_path`
- `page_number`
- `scope`
- `vessel_id`
- `document_type`
- `authority_class`
- `status`
- `verification_status`
- `equipment_id`
- `chunk_index`
- `content_hash`
- `ingestion_version`

## Fields required before re-indexing
No additional production Chroma field change is approved in Step 6.

Current minimum retained production Chroma fields remain:
- source/page linkage
- collection/scope linkage

## Fields copied from the registry in the later governed Chroma phase
Approved later-phase compact Chroma fields:
- `chunk_id`
- `document_version_id`
- `document_id`
- `source_hash`
- `relative_path`
- `page_number`
- `scope`
- `vessel_id`
- `document_type`
- `authority_class`
- `status`
- `verification_status`
- `equipment_id`
- `chunk_index`
- `content_hash`
- `ingestion_version`

These are copied, not authoritative.

## Fields not permitted in Chroma
Do not store in Chroma as governed metadata:
- full relationship arrays
- full correspondence bodies
- defect narratives beyond compact identifiers if later approved
- requisition text bodies
- photo body descriptions unless explicitly required
- human-review history objects
- bulky assertion history
- generated-note backreference graphs

## Array handling
Chroma should avoid complex arrays where possible.

Preferred pattern:
- store one compact scalar filter field where necessary;
- keep many-to-many relationships in SQLite.

## Metadata-size limits
The Chroma metadata object must remain compact enough for reliable filterability and portability. Rich governance detail belongs in SQLite.

## Type restrictions
Chroma fields should use simple scalar types compatible with the chosen Chroma layer:
- string
- integer
- boolean only where supported safely

Avoid nested governance structures in Chroma metadata.

## Consistency checks against SQLite
The future implementation must verify that copied Chroma fields match the authoritative registry for:
- `document_version_id`
- `source_hash`
- `relative_path`
- `scope`
- `vessel_id`
- `document_type`
- `authority_class`
- `status`
- `verification_status`
- `equipment_id`
- `ingestion_version`

## Registry/Chroma mismatch handling
If copied fields mismatch the registry:
- SQLite remains authoritative;
- mismatch must be flagged;
- retrieval should not silently treat stale Chroma metadata as authoritative;
- corrective migration or rebuild must follow approved workflow.

## Chroma mandatory-now vs later
### Chroma mandatory now
- existing source/page/scope linkage only

### Chroma mandatory after governed re-indexing
- `chunk_id`
- `document_version_id`
- `source_hash`
- `page_number`
- `chunk_index`
- `content_hash`
- `ingestion_version`

### Recommended copied fields after governed re-indexing
- `document_id`
- `relative_path`
- `scope`
- `vessel_id`
- `document_type`
- `authority_class`
- `status`
- `verification_status`
- `equipment_id`

## Design restriction
No Chroma metadata migration or rebuild is approved in Step 6.

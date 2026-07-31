# RAG Obsidian Metadata Boundary v1

Revision date: 2026-07-31
Status: Approved for v1 design

## Purpose
Define how Obsidian participates as a derived knowledge layer without becoming the authority for source-document metadata.

## Core rule
Obsidian is a derived knowledge layer.

It may contain:
- hub notes
- evidence maps
- user-approved knowledge notes
- generated derivative notes
- stable-ID links to registry and source records

It is not authoritative for governed source-document metadata.

## Permitted frontmatter
Permitted future frontmatter may include compact identifiers such as:
- `note_id`
- `document_id`
- `document_version_id`
- `vessel_id`
- `equipment_id`
- `source_note_type`
- `generated_status`
- `review_status`
- `source_record_ids`

## Stable-ID links
Obsidian notes should link to registry entities and source records by stable IDs, not only by path or title.

## Evidence links
Evidence links should point back to:
- `document_version_id`
- page number
- chunk identifier where later approved
- source hash where useful

## Generated-note identification
Generated notes must be clearly marked as generated or derivative.

Approved concepts:
- generated note flag
- generator version reference
- source record IDs
- regeneration timestamp

## User-approved notes
User-approved notes must remain distinguishable from generated notes and from raw source documents.

## AI-generated notes
AI-generated notes are derivative and non-authoritative unless explicitly approved for a defined use.

## Source citations
Obsidian notes must preserve stable evidence references and must not sever the link to authoritative source pages.

## Generated-folder exclusions
Generated Obsidian graph folders and derivative index folders remain excluded from production ingestion by default.

## Circular-ingestion prevention
The system must prevent:
- source document → generated note → re-ingested generated note → duplicate/self-referential evidence loops

Generated notes and graph outputs must therefore remain outside normal production ingest unless explicitly approved in a later controlled design.

## Note regeneration
Generated notes may be regenerated later, but regeneration rules must preserve stable source links and must not overwrite approved human-authored content without explicit workflow control.

## Stale-link detection
A future implementation should detect stale links where:
- source path moved
- document version changed
- superseded source remains referenced
- note points to obsolete evidence

## Source-of-truth rules
- source document metadata: authoritative in SQLite registry
- chunk retrieval metadata: authoritative in Chroma for retrieval state only
- derived note metadata: authoritative only for the note itself
- AI summaries: derivative only

## Design restriction
Step 6 does not approve automatic creation of Obsidian notes or graph projections. It defines the boundary only.

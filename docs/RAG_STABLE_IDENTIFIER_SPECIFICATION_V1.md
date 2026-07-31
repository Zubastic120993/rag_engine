# RAG Stable Identifier Specification v1

Revision date: 2026-07-31
Status: Approved for v1 design

## Purpose
Define stable identifier formats and generation rules for registry and future governed Chroma metadata.

## General rules
- Identity must not be based only on path.
- Deterministic IDs are required where reproducibility matters.
- UUIDs are required where logical identity must survive file-content changes and file moves.
- `source_hash` must remain separately stored and must not be treated as the only logical identity.
- IDs must be safe for SQLite, JSON, Chroma metadata, filenames, and Obsidian links.

## 1. `vessel_id`
### Format
`vessel:<imo_number>`

### Rules
- lower-case prefix
- numeric IMO unchanged
- stable across folder moves, naming changes, and metadata enrichment

### Example
- `vessel:9961192`
- `vessel_name = GASCHEM EUROPE`
- `vessel_imo = 9961192`

## 2. `document_id`
### Purpose
Logical document identity.

### Format
UUID-based string, for example:
- `doc:550e8400-e29b-41d4-a716-446655440000`

### Rules
- generated once when logical document identity is established
- survives path change, folder rename, and metadata enrichment
- does not change merely because a new version is issued

### Notes
`document_id` must not be derived from path alone.

## 3. `document_version_id`
### Purpose
Exact version identity for one logical document version.

### Recommended format
`docver:<deterministic-token>`

### Generation basis
Deterministic function over:
- `document_id`
- normalized `revision` where available
- normalized `status`
- `source_hash`
- optional effective-date signal where approved

### Example shape
- `docver:7f1d2a0f5c8e...`

### Rules
- changes when source document version changes
- remains stable for the same approved version metadata and same source hash
- not based solely on relative path

## 4. `source_file_id`
### Purpose
Track physical file identity/history without replacing logical document identity.

### Recommended format
`src:<deterministic-token>`

### Generation basis
Deterministic function over:
- storage root identifier
- normalized relative path at observation time
- source hash

### Rules
- path change may create a new source-file observation state if desired by implementation
- logical document identity remains separate

## 5. `chunk_id`
### Purpose
Deterministic governed chunk identity for the later governed Chroma phase.

### Recommended format
`chunk:<deterministic-token>`

### Generation basis
Deterministic function over:
- `document_version_id`
- `chunking_version`
- `page_number` or page context
- `section_path` where available
- `chunk_index` or chunk content hash

### Rules
- changes when chunking algorithm materially changes
- changes when document version changes
- stable for unchanged source, unchanged chunking version, and unchanged chunk sequence/content

## 6. `equipment_id`
### Purpose
Canonical equipment identity.

### Recommended format
- UUID-backed stable ID or approved deterministic namespace where a business identity exists
- example: `eq:3d4b5b2e-...`

### Rules
- must not depend only on folder placement
- maker/model may contribute to deterministic matching but human review may still be required

## 7. `relationship_id`
### Purpose
Unique governed relationship identity.

### Recommended format
`rel:<deterministic-token>`

### Generation basis
Deterministic function over:
- relationship type
- source record type + ID
- target record type + ID
- event date if required by relationship model

### Rules
- identical endpoints and type should produce same relationship identity unless policy requires multiple separate instances

## 8. `ingestion_run_id`
### Format
`run:<utcstamp>:<nonce-or-token>`

### Example
`run:20260731T120000Z:001`

### Rules
- unique per run
- suitable for audit logs and rollback boundaries

## 9. `human_review_id`
### Format
`review:<utcstamp>:<token>`

### Rules
- unique per review action
- safe for linking multiple approved or corrective review events

## 10. Hash fields
### `source_hash`
- source-file byte hash
- separate from logical identity
- useful for exact-file version detection

### `content_hash`
- normalized text or chunk content hash
- useful for deterministic chunk and content comparison

## 11. ID character set rules
Allowed practical character set:
- lower-case ASCII prefix
- colon separator
- lower-case hex or UUID body
- hyphen allowed inside UUID bodies

Avoid:
- spaces
- slashes
- backslashes
- unbounded free text
- path fragments as the main identity body

## 12. Obsidian and filename safety
IDs must be safe to:
- store in SQLite TEXT columns
- serialize in JSON
- place in Chroma metadata values
- embed in Obsidian frontmatter
- use inside filenames or generated note names when required

## 13. Example identity set for GASCHEM EUROPE
- `vessel_id = vessel:9961192`
- `document_id = doc:<uuid>`
- `document_version_id = docver:<deterministic-token>`
- `source_file_id = src:<deterministic-token>`
- `chunk_id = chunk:<deterministic-token>`

## 14. Approved design consequence
Stable-ID redesign is mandatory before governed vessel/authority/revision-aware retrieval can be fully trusted.

This document approves the specification only. It does not approve implementation in Step 6.

# RAG Metadata Study Closure — 20260731T065913Z

## Study status
Completed read-only metadata architecture study.

## Production baseline
- Project root: `/Users/vladymyrzub/CE_Library/Tools/rag_engine`
- Production RAG path: `/Users/vladymyrzub/CE_Library/.rag_db`
- Collection: `langchain`
- Chunk count: `108685`
- SQLite integrity: `ok`
- Active-write result: WAL=`False`, SHM=`False`, ingest.lock=`False`
- Production file count: `46`
- Production total size: `1071292889` bytes

## Current field count
- Observed production chunk metadata fields: `3`

## Major completeness findings
- Current reliable retrieval filter is limited to `collection`.
- Current reliable citation fields are `source` and `page`.
- Most other metadata is loader-derived and not governed.
- No explicit stable document/version/chunk identity exists in production metadata.

## Current metadata capability
Good enough for coarse scoped retrieval and source-page citation.
Not sufficient for safe vessel separation, authority ranking, revision control, or relationship-driven Chief Engineer workflows.

## Highest-risk gaps
- no stable IDs;
- no vessel/applicability model;
- no authority/governance model;
- no revision/supersession model;
- no relationship model;
- no governed equipment/maker/model taxonomy.

## Proposed architecture option
Target architecture: **Option D** — Chroma plus SQLite registry plus Obsidian graph layer.
Initial implementation boundary: **Option C** core first.

## Recommended v1 scope
Mandatory v1 fields:
`['document_id', 'document_version_id', 'chunk_id', 'source_hash', 'content_hash', 'relative_path', 'page_number', 'scope', 'vessel_name', 'document_type', 'revision', 'status', 'authority_class', 'approval_status', 'verification_status', 'ingestion_timestamp', 'ingestion_version', 'metadata_confidence', 'human_review_status']`

## Re-indexing requirement
- Immediate v1 core: registry-first, no embedding regeneration required.
- Later deterministic chunk-ID / governed chunk metadata phase: likely controlled re-chunk/rebuild required.

## Migration risk
Moderate.
Main risk is identity redesign and authority/applicability review, not raw embedding migration.

## Human-review requirements
Required for authority class, approval status, vessel applicability, supersession, confidentiality, and cross-record relationship validation.

## Obsidian implications
Obsidian should link to governed source and entity IDs. It should remain a derived/human knowledge layer, not the authoritative metadata registry.

## Recommended next implementation step
Define and approve the SQLite document-registry schema, controlled vocabularies, and stable-ID policy before any metadata migration or Chroma rebuild work.

## Protected-file before/after verification
Protected files unchanged: `True`
Protected file differences: `[]`

## Classification
**DESIGN READY WITH OPEN DECISIONS**

## PASS / FAIL / BLOCKED
**PASS**

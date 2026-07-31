# RAG Metadata Migration Impact — 20260731T065913Z

## Migration impact summary
| Topic | Assessment |
| --- | --- |
| Can be added without re-indexing embeddings | Registry-only document-level fields, authority/applicability fields, relationships, and review status can be added in a sidecar registry without regenerating embeddings. |
| Requires metadata-only migration | Backfilling registry records from current `source`, `page`, `collection`, tracker digest, and path analysis. |
| Requires source reprocessing | Fields derived from section headings, structured document numbers, revision extraction, and language detection where not currently stored. |
| Requires OCR | Scanned/image-only sources needing page/heading/document-control extraction. |
| Requires table extraction | Structured spare-parts, PMS tables, certificates, and tabular controlled-document registers. |
| Requires human review | Authority class, approval status, vessel applicability, supersession, relationship validation, and confidentiality flags. |
| Requires stable-ID redesign | Yes. Current Chroma-generated chunk IDs and path-based `source` are insufficient for move-stable governance. |
| Requires chunk regeneration | Only when chunk IDs or chunk-level section coordinates become part of the governed schema and must be deterministic. |
| Requires full Chroma rebuild | Not for the first registry-only phase. Yes for a later phase if deterministic chunk IDs, section coordinates, or chunk-level governed metadata must be persisted into Chroma consistently. |
| Can be introduced incrementally | Yes: registry first, then controlled vocabularies, then relationships, then chunk-level rebuild if needed. |

## Re-indexing assessment
- **Immediate v1 safe core:** does **not** require embedding regeneration if implemented as a structured metadata registry beside Chroma.
- **Deterministic chunk identity and chunk-governed section metadata:** likely require chunk regeneration and a controlled Chroma rebuild later.
- **OCR/table-derived enrichment:** may require source reprocessing for affected document classes only.

## Rollback boundaries
1. Registry schema creation — rollback by dropping or restoring registry database only.
2. Registry population from existing production evidence — rollback by restoring registry backup.
3. Human-reviewed authority/applicability updates — rollback by restoring approved registry snapshot.
4. Deterministic chunk-ID / re-chunk phase — rollback requires restore-tested backup before any Chroma rebuild.

## Architecture boundary assessment
### A. Store everything in Chroma metadata
- Lowest immediate complexity.
- Weak structured governance.
- Poor relationship modeling.
- Harder audit/rollback and controlled vocabulary enforcement.

### B. Chroma plus document manifest files
- Better document control than A.
- Still weak for cross-document relationship queries and transactional governance.

### C. Chroma plus structured SQLite metadata registry
- Best practical v1 core.
- Strong filterability, auditability, rollback, and backup compatibility.
- Keeps Chroma focused on embeddings and chunk retrieval.

### D. Chroma plus SQLite registry plus Obsidian graph layer
- Best long-term architecture.
- Allows governed metadata core plus human knowledge layer plus derived graph links.
- Slightly higher complexity, but clearer separation of authority and notes.

## Recommended implementation path
- Target architecture: **D**
- First implementation boundary: **C**

## Backup implications
Any migration affecting document identity, chunk identity, relationship stores, or Chroma metadata must be preceded by a verified backup and, before major destructive change, a restore-tested backup.

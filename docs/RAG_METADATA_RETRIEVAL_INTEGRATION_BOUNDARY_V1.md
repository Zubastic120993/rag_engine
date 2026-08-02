# RAG Metadata Retrieval Integration Boundary v1

Revision date: 2026-07-31
Status: Approved planning policy for Step 8A

## Purpose
Define how future retrieval may consult the authoritative SQLite registry without violating the approved Chroma boundary or triggering premature Chroma migration/re-indexing.

## Core rule
Retrieval remains two-layered:
- Chroma generates chunk candidates from the current retrieval index.
- SQLite provides governed metadata, review state, and relationship-aware interpretation.

SQLite is authoritative for governed metadata.
Chroma remains authoritative only for retrieval-state metadata currently stored there.

## Current approved starting point
Current production Chroma metadata remains limited to:
- `collection`
- `page`
- `source`

Therefore Step 8A does not approve direct production reliance on non-existent Chroma fields such as:
- `document_version_id`
- `vessel_id`
- `authority_class`
- `verification_status`
- `equipment_id`

## Approved initial retrieval boundary
Before any Chroma migration/re-indexing, registry-aware retrieval may later do only the following:
1. run normal Chroma retrieval by current scope/index behavior
2. map results back to registry records using approved evidence keys such as source path, source hash, page linkage, and document-version mapping receipts
3. use reviewed registry metadata for:
   - citation enrichment
   - authority display
   - approval/review-state display
   - vessel applicability confirmation where mapping is governed and explicit
4. refuse to claim governed metadata when no reliable registry mapping exists

## Not approved in the initial retrieval boundary
Not approved before later phases:
- treating missing registry mapping as permission to invent metadata
- treating stale or copied Chroma metadata as authoritative over SQLite
- applying strict vessel/authority filtering solely from Chroma when those fields are not yet governed there
- bulk copying new production metadata into Chroma in this Step
- re-indexing to force the boundary early

## Retrieval lookup order
Approved future lookup order for the first registry-aware phase:
1. determine query scope using approved routing
2. retrieve Chroma chunk candidates
3. map candidates to registry document/version records where possible
4. apply only reviewed/approved registry constraints
5. enrich citations from registry data when mapping is valid
6. report no-governed-mapping state explicitly when mapping is absent or ambiguous

## Human-review gate in retrieval
Retrieval must not silently elevate:
- `review_required`
- `ambiguous`
- `conflicting`
- `unverified`

to approved operational truth.

If the active registry state for a relevant governed field is not approved/verified:
- show that limitation;
- do not apply the field as a hard trusted constraint unless separately approved.

## Registry/Chroma consistency checks before integration
Before any production registry-aware retrieval rollout, required prechecks include:
- registry schema version valid
- registry integrity and foreign keys clean
- controlled vocabulary validation passes
- mapping receipts exist for the production population batch being used
- no unexplained mismatch between registry evidence keys and retrieval evidence keys

## Citation boundary
Citation may be enriched from the registry only when the mapping is explicit.

Approved enrichment targets later may include:
- `document_id`
- `document_version_id`
- reviewed title
- authority class
- approval status
- effective/revision metadata

But page/source traceability from Chroma must remain preserved.

## Duplicate and moved-file handling in retrieval
Retrieval integration must not assume path-only identity.

Rules:
- moved files must continue to resolve through governed stable IDs where mapping exists;
- duplicates must not be collapsed into one authoritative document without approved identity resolution;
- unresolved duplicate families must remain visible as unresolved.

## Deferred Chroma migration
Chroma migration is explicitly deferred.

No Step 8A approval is granted for:
- copying new governed metadata into production Chroma
- adding deterministic chunk IDs to production Chroma
- rebuilding embeddings
- changing chunk generation

These remain later approved-phase work only.

## Deferred re-indexing
Re-indexing is explicitly deferred.

No production re-indexing is approved until:
- registry population is proven
- mapping receipts are validated
- backup/restore boundary is proven for the combined state
- Chroma migration design is separately approved

## Design restriction
This Step approves the retrieval integration boundary only. It does not approve retrieval code changes in production.

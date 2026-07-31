# ADR 001 — Metadata Registry Architecture

Date: 2026-07-31
Status: Accepted for v1 design

## Context
Step 5 confirmed that current production Chroma metadata contains only three governed retrieval fields in practice:
- `collection`
- `page`
- `source`

The current design is adequate for coarse scope filtering and source-page citation, but it does not provide stable document identity, vessel applicability, authority metadata, revision/supersession logic, or relationship modeling.

A long-term Chief Engineer knowledge system requires governed metadata without turning Chroma into an overloaded authority store.

## Decision
Use SQLite as the authoritative metadata registry, Chroma as the embedding and chunk index, and Obsidian as a derived knowledge layer.

Approved v1 implementation boundary:
- Option C — Chroma plus structured SQLite metadata registry

Approved target architecture after registry stability:
- Option D — SQLite registry plus Obsidian graph layer

## Alternatives considered
### Option A — Store everything in Chroma metadata
Rejected as the v1 authority model.

### Option B — Chroma plus document manifest files
Considered but not chosen as the primary governed architecture.

### Option C — Chroma plus structured SQLite metadata registry
Accepted as the v1 implementation boundary.

### Option D — Chroma plus SQLite registry plus Obsidian graph layer
Accepted as the target architecture, deferred beyond initial registry stabilization.

## Rationale
- Chroma is well suited to embeddings and chunk retrieval.
- SQLite is better suited to governed metadata, relationships, review state, and auditability.
- Obsidian is useful for human knowledge work but should not become the source of truth for source-document metadata.
- Registry-first implementation avoids immediate embedding rebuild.

## Consequences
### Positive
- stable identity becomes possible
- vessel separation becomes governable
- authority and revision metadata become explicit
- relationship modeling becomes feasible
- Chroma metadata can remain compact

### Negative / cost
- added architectural complexity
- future schema design and migration effort
- human-review workflow needed for sensitive fields
- later deterministic chunk-ID phase may still require controlled re-indexing

## Risks
- incorrect logical-document grouping during migration
- weak review controls could approve ambiguous applicability
- Chroma/registry divergence if copied fields are not validated
- confidential relationship details could leak if boundary is not enforced

## Migration impact
Registry-first v1 can begin without immediate embedding rebuild.
A later governed chunk-ID / governed Chroma metadata phase may require re-indexing.

## Backup impact
Any future production migration affecting registry or Chroma metadata must follow verified backup discipline. Any destructive or rebuild phase requires a restore-tested backup boundary.

## Future review conditions
Review this ADR when:
- registry schema is implemented
- query path begins using registry metadata
- deterministic chunk IDs are introduced
- Obsidian projection is proposed for automation

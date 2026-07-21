---
name: rag-engine
description: "Chief Engineer document retrieval policy for CE_Library. Use Hermes tool ce_rag_query with an explicit approved scope. Treat no_coverage as terminal for that scope — never invent answers or silently query another scope."
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [rag, chroma, ce-library, retrieval, maritime, scoped, hermes, ce-rag]
    related_skills: [manual-rag-builder, sire-question-retrieval, procedure-writer-chief-engineer, company-sms-imm-retrieval, document-library-organizer, rag-library-path-governance]
---

# Chief Engineer Document Retrieval

Central Hermes retrieval-policy skill for the local CE_Library scoped Chroma RAG.

**Normal Hermes interface:** tool `ce_rag_query` (plugin `ce-rag`).

Do **not** call the rag-engine CLI for ordinary retrieval. Do **not** access Chroma directly. Do **not** use legacy SQLite indexes under `~/.hermes/rag/` for production answers.

One Chroma database only: `~/CE_Library/.rag_db`. The plugin is **query-only**. Sync, doctor, eval, and reindex are administrative actions and are **not** exposed through `ce_rag_query`.

## Tool: `ce_rag_query`

Invoke the Hermes tool conceptually (not via shell):

| Argument | Required | Rules |
|----------|----------|--------|
| `question` | yes | Non-empty string after trim; max 4000 characters |
| `scope` | yes | One approved scope from the table below |
| `top_k` | no | Integer 1–20 only when fewer/more chunks would genuinely help |

### Result handling

| `status` | `exit_code` | Behaviour |
|----------|-------------|-----------|
| `ok` | **0** | Use `answer`. Preserve `sources` (filenames and pages). Report `scope` (resolved). Distinguish retrieved facts from engineering interpretation. |
| `no_coverage` | **2** | Selected scope lacks sufficient evidence. State that clearly. Do **not** invent an answer. Do **not** silently query another scope. Do **not** use model memory as replacement evidence. |
| `error` | **1** (or other unexpected) | Retrieval failed. Preserve a concise safe error summary. Do **not** convert error to `no_coverage`. Do **not** invent an answer. |

A silent degrade from *cited from library* to *recalled from training* is the failure this stack was built to prevent.

## Canonical scopes

| Hermes scope | Purpose |
|--------------|---------|
| `sms_library` | Company SMS, IMM procedures and company instructions |
| `sire_library` | SIRE and OCIMF material |
| `imo_library` | IMO, statutory and regulatory documents |
| `manual_library` | General maker manuals and technical publications |
| `manual_library_gaschem_europe` | Gaschem Europe vessel-specific manuals |
| `manual_library_gaschem_africa` | Gaschem Africa vessel-specific manuals |
| `ce_wiki` | Chief Engineer notes and curated operational knowledge |

Other scopes (for example `me-c`) only when the task has a defined operational need for that corpus. Do not invent scope names.

Specialist skills should list only the scopes they actually use; this table is the single canonical reference.

## Forbidden implicit fallback

**Forbidden:**

1. Query `manual_library_gaschem_europe`.
2. Receive `no_coverage`.
3. Silently query `manual_library`.
4. Present the general-manual result as vessel-specific.

The same ban applies to any pair of scopes (SMS ↔ IMO, SIRE ↔ SMS, vessel ↔ maker, etc.).

## Deliberate multi-source evidence review

For procedure writing or compliance review, Hermes may intentionally query several sources **separately**, for example:

1. `sms_library`
2. `sire_library`
3. `imo_library`
4. `manual_library`
5. `manual_library_gaschem_europe`

Requirements:

* every scope query must be deliberate and explicit
* results must remain attributed to their individual scope
* `no_coverage` in one scope must remain visible
* another scope must **not** be described as fallback evidence for the failed scope
* conflicting requirements must be reported, not silently reconciled
* source authority and applicability must be assessed separately

Use the term **multi-source evidence review**, not fallback.

If **all** required scopes return `no_coverage`, say the library does not cover the topic. Never synthesize a complete answer from training knowledge for those gaps.

## Source and page preservation

Every `ok` answer must retain returned sources. Prefer document name, path (when present), and page. Do not invent paths or pages. Opening PDFs is allowed only after a validated source path has been returned.

## What this skill does not do

* Does not sync, reindex, rebuild, or migrate the Chroma DB
* Does not expose filesystem or executable parameters to the user
* Does not restore SQLite retrieval as a production path
* Does not grant Hermes direct Chroma access

---

## Maintenance / diagnostic (administrators only)

Not part of normal Hermes retrieval. Use only when the user explicitly requests administration or diagnosis.

Executable (absolute):

`/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/rag-engine`

Examples:

```bash
/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/rag-engine list-scopes --json
/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/rag-engine doctor
/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/rag-engine gaps
/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/rag-engine sync
```

`sync` only after documents change in CE_Library and the user explicitly authorizes an administrative sync. Never trigger sync from a normal retrieval question.

---

## Human-response presentation

This section controls only the human-readable answer shown by Hermes. It does
not change the `ce_rag_query` result or its structured fields. Returned data
must continue to preserve fields such as path, page, collection, distance
(Chroma L2 distance; lower = more relevant), scope, status, exit code, and
timings when present.

### Default response

For an ordinary technical question, show:

1. The direct supported answer.
2. The primary document name and page.
3. One short qualification only when technically necessary.

Example:

**Answer:** <supported value or instruction>.

Source: `<document>.pdf`, p. `<page>`.

Do not normally display:

- absolute filesystem paths
- shell or document-opening commands
- raw JSON
- retrieval timings
- long quotations
- repeated chunks from the same document

### Expanded evidence

Show additional detail when the user asks to:

- show the source or evidence
- quote the document
- provide the full path
- open the document
- compare sources
- provide a detailed or research answer

For expanded evidence, label sources by their role:

- Primary authority
- Supporting evidence
- Additional reference

Do not claim an authority relationship that was not established by the
retrieved evidence. In a deliberate multi-source evidence review, identify
the most directly applicable governing source as primary and keep every
source attributed to its own scope.

### Result status

- `ok`: give the supported answer and source.
- `no_coverage`: state that sufficient evidence was not found in the selected scope.
- `error`: state that retrieval failed.

Do not describe or infer a status that the tool did not return.

### Technical terminology

When a question uses incorrect or ambiguous equipment terminology, correct it
briefly before answering. Answer the likely intended question only when the
retrieved evidence supports it.

### Source deduplication

Combine multiple retrieved chunks from the same document into one document
reference with the relevant page or page range.

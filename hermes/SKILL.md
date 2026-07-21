---
name: rag-engine
description: "Use when querying the local CE_Library scoped Chroma RAG (maker manuals, SMS, SIRE, IMO, wiki, ME-C). Call rag-engine ask --json; treat exit 2 / no_coverage as terminal — never fall back to training knowledge."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [rag, chroma, ce-library, retrieval, maritime, scoped, hermes]
    related_skills: [manual-rag-builder, sire-question-retrieval, procedure-writer-chief-engineer, document-library-organizer, rag-library-path-governance]
---

# rag-engine (Hermes)

Call the local scoped RAG tool. Corpus is the client library (`CE_LIBRARY_ROOT`), not this repo.

Install into Hermes: copy this file to `~/.hermes/skills/documents/rag-engine/SKILL.md` (or symlink). Ensure `rag-engine` is on PATH (e.g. `~/.local/bin/rag-engine` → project `venv/bin/rag-engine`).

## Commands

```bash
rag-engine list-scopes --json
rag-engine ask --scope <scope_or_hermes_alias> --json "<question>"
rag-engine sync
rag-engine gaps
```

## Exit codes (mandatory)

| Code | Meaning | Skill behaviour |
|------|---------|-----------------|
| **0** | Answer from retrieved library chunks (`status=ok`) | Use `answer` + cite `sources`. |
| **2** | No relevant coverage (`status=no_coverage`) | **Terminal.** Report that the library does not cover this. Do **not** fall back to model prior knowledge about MAN/Everllence/ME-C or invent citations. |
| **1** | Tool/runtime error (`status=error`) | Retry once or surface the error; do not answer from memory as if cited. |

A silent degrade from *cited from library* to *recalled from training* is the failure this tool was built to prevent. Exit **2** must look different from exit **0** in the skill output (e.g. `not specified / not in library`).

## JSON contract

```json
{
  "answer": "...",
  "sources": [{"path": "...", "page": 1, "collection": "me-c", "score": 0.42}],
  "scope": "me-c",
  "status": "ok"
}
```

On exit 2, `status` is `no_coverage` (or empty retrieval). Treat as final for that scope.

Coverage gaps are appended to `$RAG_DB_PATH/ask_events.jsonl`. Review with:

```bash
rag-engine gaps
```

Unexpected exit-2s on topics you believe are in the library → missing document or wrong scope. Promote those questions into `eval/questions.json` when you confirm the cause.

## After library PDFs change

```bash
rag-engine sync
```

(Same as `ingest` — hash no-op if unchanged. Prefer this over a schedule so a new manual cannot look like a coverage gap.)

## Scopes

Prefer `rag-engine list-scopes --json` over hardcoding. Hermes routing aliases (`sire_library`, `imo_library`, `sms_library`, …) are accepted as `--scope` values via `scopes.yaml`.

## Multi-scope tasks

If the routing guide requires several libraries, call `ask` once per scope. If **all** return exit 2, say the library does not cover it. If **any** return 0, synthesize only from those cited answers — still no training-data fill for gaps.

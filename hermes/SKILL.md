# rag-engine (Hermes)

Call the local scoped RAG tool. Corpus is the client library (`CE_LIBRARY_ROOT`), not this repo.

## Commands

```bash
rag-engine list-scopes --json
rag-engine ask --scope <scope_or_hermes_alias> --json "<question>"
```

Install once: `pip install -e /path/to/rag_engine` (or ensure `rag-engine` is on PATH).

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

## Scopes

Prefer `rag-engine list-scopes --json` over hardcoding. Hermes routing aliases (`sire_library`, `imo_library`, `sms_library`, …) are accepted as `--scope` values via `scopes.yaml`.

## Multi-scope tasks

If the routing guide requires several libraries, call `ask` once per scope. If **all** return exit 2, say the library does not cover it. If **any** return 0, synthesize only from those cited answers — still no training-data fill for gaps.

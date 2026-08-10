# rag-engine

Local **retrieval-only** tool for scoped RAG over a client document library (not a corpus repo).
The library lives outside this project (`CE_LIBRARY_ROOT`); this package is code only.

**Architecture (schema v4 / ORCH_104+ORCH_106):**

```text
Hermes → ce_rag_query → rag_engine (retrieve evidence) → Hermes-selected model → final answer
```

- `rag_engine` retrieves an evidence package (`sources`, `retrieved_chunks`, `retrieval_context`, …).
- On successful retrieval (`status=ok`), `answer` may be **null by design**.
- Hermes owns natural-language generation and evidence-sufficiency judgment.
- Engine-side OpenAI/Claude/Gemini chat generation is not used on the ask path.

## Install

```bash
python -m venv venv
./venv/bin/pip install -e ".[dev]"
# or: pip install -e . && pip install pytest
# Ollama embeddings retained: mxbai-embed-large
# OpenAI is not required for retrieval / ask (Hermes owns generation)
```

## Config (env)

| Variable | Default | Meaning |
|----------|---------|---------|
| `CE_LIBRARY_ROOT` | `~/CE_Library` | Client document tree |
| `RAG_DB_PATH` | `$CE_LIBRARY_ROOT/.rag_db` | Chroma index |
| `RAG_EMBED_MODEL` | `mxbai-embed-large` | Ollama embedding model for retrieval |
| `RAG_OLLAMA_TIMEOUT` | `300` | Seconds before ask fails with exit 1 |
| `RAG_SUGGEST_SCORE_MAX` | `1.2` | Max Chroma distance for `--suggest-scopes` hits |
| `OPENAI_API_KEY` | optional | Not required for ask; retained for archived generation helpers / doctor info |
| `RAG_LLM_MODEL` | `gpt-5.6-luna` | Archived generation model name (unused on ask path) |
| `RAG_OPENAI_TIMEOUT` | `60` | Archived; unused on ask path |

Scopes live in [`rag_engine/scopes.yaml`](rag_engine/scopes.yaml) — single registry for ingest + CLI.

Generation fallback: **none** (Hermes owns NL answers).
Ollama remains required for embeddings.

### `maker-manuals` vs `vessels`

| Scope | Alias examples | Means |
|-------|----------------|-------|
| `maker-manuals` | `manual_library` | General maker manuals under career engine knowledge / SDS |
| `vessels` | `manual_library_gaschem_europe`, `manual_library_gaschem_africa` | Files under `20_Vessels/` only |

These are **not** the same collection. A vessel alias never silently includes `maker-manuals`. Exit **2** for the requested scope is terminal — use diagnostics (below), not model memory.

## CLI

```bash
./run_tests.sh -q
```

Supported isolated test wrapper:

- sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`;
- clears `PYTHONPATH`, `PYTHONHOME`, and `VIRTUAL_ENV` before invoking the
  local `./venv/bin/python -m pytest`;
- preserves normal pytest exit codes;
- passes any extra pytest arguments through unchanged.

```bash
rag-engine list-scopes --json
rag-engine paths
rag-engine doctor [--json]          # read-only health check
rag-engine explain-scope PATH
rag-engine explain-alias ALIAS      # e.g. manual_library_gaschem_europe → vessels
rag-engine scope-stats [--json]

rag-engine ask --scope me-c --json "max exhaust valve burn-off for 50ME-C"
rag-engine ask --scope sire_library --json "oil mist detection"
rag-engine ask --scope vessels --json --suggest-scopes "OWS-COM automatic stop"
```

### JSON / exit-code contract (`schema_version: 4`)

Every `--json` response includes `"schema_version": 4`. Stdout is **exactly one** JSON document; logs go to stderr.

| Exit | `status` | Meaning |
|------|----------|---------|
| 0 | `ok` | Admissible evidence package retrieved (`sources` / chunks / `retrieval_context`). `answer` is usually `null` — Hermes generates. `coverage=full` means retrieval-package state only, **not** answer completeness or factual sufficiency. |
| 2 | `clarification_required` | Ask backend clarification prompt only; no technical answer yet |
| 2 | `no_coverage` | Terminal — no training-knowledge fallback; `answer` is `null`, `sources` is `[]` |
| 1 | `error` | Tool/runtime/timeout error |

Fields always include `query`, `requested_scope`, `resolved_scope`, `generation_owner` (`hermes`). On `no_coverage`, an optional `hint` may point at diagnostics; with `--suggest-scopes`, the hint may name **other** scopes only when real hits exist (exit code stays 2; `answer` stays null).

Each entry in `sources` has `path`, `page`, `collection`, and `distance`. **`distance` is a Chroma L2 distance: lower means closer/more relevant** — it is not a similarity score. Results are returned nearest-first.

Schema history: **v4** makes ask retrieval-only (Hermes owns NL generation; `answer` null on normal `ok`). **v3** renamed `sources[].score` → `sources[].distance`. **v2** switched generation to plain text and dropped `partial_coverage`. Statuses: `ok`, `clarification_required`, `no_coverage`, `error`, `empty_question`.

### Ingest / gaps

```bash
rag-engine sync                   # after PDFs change (hash no-op if unchanged)
rag-engine ingest --max-new 5
rag-engine gaps                   # exit-2 trail
rag-engine backfill
rag-engine eval [--retrieval-only]
```

Successful sync writes `index_fingerprint.json` beside the DB (embed model, chunking, NFKC). `doctor` FAILs if live config disagrees — silent embed-model drift looks like a content gap otherwise.

## Doctor

```bash
rag-engine doctor
```

Read-only: library/DB paths, scopes/aliases, prefix dirs + indexed docs, generation readiness (informational — Hermes owns NL generation), Ollama embedding readiness, fingerprint match, Chroma open, tracker, stale `Hermes_Library` paths, orphan sources, coverage gap counts, git corpus hygiene. Does **not** reindex or write to Chroma.

## AI Chief Engineer v1 Alpha (local UI)

Small Gradio shell for asking engineering questions without the terminal.
The UI calls **`rag_engine.answer()` only** — it surfaces the **retrieval evidence package**.
Final NL answer generation is Hermes-owned (not OpenAI-in-engine).

```bash
# from repo root, with venv active
# (optional local .env is loaded by app.py via python-dotenv; never commit keys)
python app.py
# → http://127.0.0.1:7861
```

Alpha screen:

- question box + **Ask**
- retrieval package / status (`ok` / `clarification_required` / `no_coverage` / `error`)
- sources panel (document name, viewer page, scope, path, open link + copy text)
- clarification prompt + confirmation input + **Continue** (re-submits with `confirmation_text`)
- health strip: rag_engine reachable, embedding backend available? (OpenAI key informational)
- copy control on the answer/evidence box

No admin mutation / reindex controls in Alpha.

Helpers live in `rag_engine/chief_ui.py`. Launch entrypoint remains `app.py`.

Cited PDFs are clickable via a safe `/file=` route under `CE_LIBRARY_ROOT` only (symlink-resolved, case-insensitive prefix check on macOS). Stored pages are **0-based** (PyPDFLoader); links use `#page=N` with **1-based** viewer pages.

Chrome and Firefox built-in PDF viewers honour `#page=N`. Safari’s viewer is unreliable — a failed jump is usually the browser, not a bad citation.

## Troubleshooting exit 2

1. `rag-engine explain-alias <alias>` — confirm which scope you hit  
2. `rag-engine explain-scope <relative-path>` — where the file is classified and whether it is indexed  
3. `rag-engine scope-stats` — docs/chunks per scope  
4. Optionally re-ask with `--suggest-scopes` (still exit 2; does not answer from another scope)  
5. If the file is missing from the right tree: place it under `CE_LIBRARY_ROOT`, then `rag-engine sync`  
6. Never fill the gap from model memory

## Push safety

This repo must stay **code-only** (no PDFs, no `.rag_db`, no `embedded.json`). Before push:

```bash
git ls-files | grep -iE '\.pdf$|\.rag_db|embedded\.json|chroma|\.sqlite3$'
```

Must print nothing.

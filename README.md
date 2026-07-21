# rag-engine

Local **tool** for scoped RAG over a client document library (not a corpus repo).
The library lives outside this project (`CE_LIBRARY_ROOT`); this package is code only.

## Install

```bash
python -m venv venv
./venv/bin/pip install -e .
# Ollama: mxbai-embed-large + a chat model (default qwen3.5:9b)
```

## Config (env)

| Variable | Default | Meaning |
|----------|---------|---------|
| `CE_LIBRARY_ROOT` | `~/CE_Library` | Client document tree |
| `RAG_DB_PATH` | `$CE_LIBRARY_ROOT/.rag_db` | Chroma index |
| `RAG_EMBED_MODEL` | `mxbai-embed-large` | Embeddings |
| `RAG_LLM_MODEL` | `qwen3.5:9b` | Chat model |

Scopes (Hermes contract) live in [`rag_engine/scopes.yaml`](rag_engine/scopes.yaml) — single registry for ingest + CLI.

## CLI (Hermes interface)

```bash
rag-engine list-scopes --json
rag-engine paths

rag-engine ask --scope me-c --json "max exhaust valve burn-off for 50ME-C"
# → {"answer":"...","sources":[{"path":...,"page":...,"score":...}],"scope":"me-c","status":"ok"|"no_coverage"}

rag-engine ask --scope sire_library "oil mist detection"   # Hermes alias OK
```

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | Answer returned (`status=ok`) |
| 2 | No relevant coverage / model refuse (`status=no_coverage`) |
| 1 | Tool error |

```bash
rag-engine ingest                 # lock-protected, SHA-256 incremental
rag-engine sync                   # same — run after PDFs change (not on a schedule)
rag-engine gaps                   # recent exit-2 / no_coverage (live eval trail)
rag-engine ingest --max-new 5     # crash-safe batches
rag-engine backfill
rag-engine eval
```

Gradio (optional): `python app.py` → http://127.0.0.1:7861

## Push safety

This repo must stay **code-only** (no PDFs, no `.rag_db`, no `embedded.json`). Before push:

```bash
git ls-files | grep -iE '\.pdf$|\.rag_db|embedded\.json|chroma|\.sqlite3$'
```

Must print nothing.

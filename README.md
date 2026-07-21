# Local scoped RAG (CE manuals)

Offline RAG over `~/CE_Library` plus this repo’s `data/` PDFs. One Chroma index, Hermes-style `collection` scopes, shared `answer()` for CLI and Gradio.

## Requirements

- Python 3.12+ venv (`./venv`)
- [Ollama](https://ollama.com) with:
  - `mxbai-embed-large` (embeddings)
  - chat model (default `qwen3.5:9b`, override with `RAG_LLM_MODEL`)

```bash
python -m venv venv
./venv/bin/pip install -r requirements.txt
ollama pull mxbai-embed-large
ollama pull qwen3.5:9b
```

## Index

Store: `~/CE_Library/.rag_db` (sibling of library folders, not under `99_Rules/`).

```bash
# Incremental / resume-safe (SHA-256 keyed)
./venv/bin/python -m rag.ingest

# Long rebuild in crash-safe batches
./venv/bin/python -m rag.reindex_loop

# After changing collection mapping
./venv/bin/python -m rag.backfill_collections
```

Text is NFKC-normalized on ingest **and** query. Chunking is intentionally **800 / 100** for the current index.

## Ask

```bash
./venv/bin/python ask.py --scope me-c "max exhaust valve burn-off for 50ME-C"
./venv/bin/python ask.py --scope sms "bunkering SMS requirements"
./venv/bin/python ask.py "fuel oil sampling"          # whole corpus

./venv/bin/python app.py   # Gradio on http://127.0.0.1:7861
```

Scopes: `me-c`, `sms`, `wiki`, `maker-manuals`, `regulatory`, `inspection`, `vessels`, `career`, `rules`, `other`.

## Eval gate

```bash
./venv/bin/python -m rag.eval_run --retrieval-only
./venv/bin/python -m rag.eval_run
```

Cases in `eval/questions.json` include not-in-library and out-of-scope negatives — the model must refuse, not invent.

## Layout

| Path | Role |
|------|------|
| `rag/config.py` | Paths, models, collection map |
| `rag/text.py` | Shared NFKC normalize |
| `rag/ingest.py` | Hash-keyed PDF ingest |
| `rag/query.py` | `answer(question, scope=None)` |
| `ask.py` / `app.py` | CLI / Gradio |
| `eval/` | Eval questions + last results |

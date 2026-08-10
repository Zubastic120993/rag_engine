# HRM_046 — OpenAI Runtime Migration

## 1. Baseline
- `rag_engine` branch: `main`
- baseline HEAD: `7e68567af09914a7ec3d3bb9cb253e57dc0f8e39`
- `origin/main`: matched baseline before delivery completion
- `orchestrator` reference already verified earlier in this governed run: `master @ 73116fc40fc6fd3f86ac2f5039765e81e9ca1d50`, `origin/master` matched
- delivery mode: finalize only; no retrieval/ranking/embedding/index redesign

## 2. Ollama dependency inventory
Remaining Ollama-linked code paths:
- `rag_engine/query.py` — `OllamaEmbeddings` for retrieval only
- `rag_engine/ingest.py` — `OllamaEmbeddings`
- `rag_engine/backfill_collections.py` — `OllamaEmbeddings`
- `rag_engine/doctor.py` — embedding readiness check only

Removed from normal answer generation:
- `OllamaLLM`
- Qwen default chat generation
- heavy fallback / Ollama generation fallback behavior

## 3. Files modified
Tracked modifications:
- `README.md`
- `pyproject.toml`
- `rag_engine/__init__.py`
- `rag_engine/cli.py`
- `rag_engine/config.py`
- `rag_engine/doctor.py`
- `rag_engine/query.py`
- `rag_engine/scopes.yaml`
- `tests/test_clarification_first_flow.py`
- `tests/test_coverage_states.py`
- `tests/test_f18_retrieval_evidence.py`
- `tests/test_hardening.py`
- `tests/test_ingest_empty_extraction.py`
- `tests/test_ingest_moved_path.py`
- `tests/test_reconcile_path.py`

New intended HRM_046 files:
- `rag_engine/openai_generation.py`
- `tests/test_cli_import_isolation.py`
- `tests/test_openai_generation.py`

## 4. Provider / model
- generation provider: OpenAI
- generation API: OpenAI Responses API
- generation model: `gpt-5.6-luna`
- required environment variable: `OPENAI_API_KEY`
- generation fallback: none

## 5. OpenAI migration summary
Completed implementation state:
- answer generation migrated from Ollama/Qwen to OpenAI Responses API
- default generation model changed to `gpt-5.6-luna`
- generation dependency lazy-loaded in dedicated module
- retrieval path kept separate from generation path
- no Ollama generation fallback retained
- README/config updated to state OpenAI generation and retained Ollama embeddings
- tests added for OpenAI success, missing key, provider failure, secret safety, default model, and CLI import isolation

## 6. Generation boundary
Generation boundary after HRM_046:
- retrieval: existing Chroma + existing embeddings + existing scope/ranking behavior
- synthesis: OpenAI Responses API only
- doctor: distinguishes OpenAI generation readiness from Ollama embedding readiness
- no re-embed, no reindex, no retrieval semantic change

## 7. Remaining Ollama usage
- `mxbai-embed-large` embeddings remain via Ollama
- ingest/backfill continue to use Ollama embeddings
- retrieval embedding timeout remains governed by `RAG_OLLAMA_TIMEOUT`
- no Ollama chat generation path remains in normal ask flow

## 8. Embedding status
- embedding model retained: `mxbai-embed-large`
- existing Chroma/index retained unchanged
- no re-embedding performed
- no rebuild performed
- retrieval behavior intentionally preserved

## 9. CLI / import isolation
Implemented and verified:
- `rag_engine.cli` no longer eagerly imports full query/generation stack for lightweight commands
- lightweight commands tested not to import `rag_engine.query` or `rag_engine.openai_generation`
- foreign Hermes `site-packages` scrub added in `rag_engine/__init__.py`
- doctor cross-process persistence probe now scrubs inherited Python env before subprocess launch

## 10. Normal CLI results
Commands run with normal rag-engine venv:
- `./venv/bin/rag-engine paths` → PASS
- `./venv/bin/rag-engine list-scopes --json` → PASS
- `./venv/bin/rag-engine scope-stats --json` → PASS
- `./venv/bin/rag-engine doctor --skip-ollama --json` → FAIL status, but only because OpenAI generation readiness is false without `OPENAI_API_KEY`

Observed normal doctor result:
- `openai_sdk_available`: true
- `openai_api_key_present`: false
- `openai_generation_ready`: false
- `ollama_embedding_reachable`: skipped
- `embed_model_available`: skipped
- `cross_process_persistence`: true
- no foreign Hermes `pydantic` / `pydantic_core` contamination remained in final doctor result

## 11. Sanitized CLI results
Sanitized control command:
- `env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV ./venv/bin/rag-engine doctor --skip-ollama --json`

Result:
- matched the normal doctor result
- no foreign Hermes `pydantic` / `pydantic_core` contamination observed
- same FAIL reason only: missing `OPENAI_API_KEY`

## 12. Full tests
Validation completed:
- `./run_tests.sh` → PASS (`161 passed`)
- `./venv/bin/python -m compileall rag_engine tests` → PASS
- `git diff --check` → PASS

## 13. Live OpenAI smoke result
Live OpenAI smoke was **not run**.
Reason:
- `OPENAI_API_KEY` not available to rag-engine runtime

Recorded result:
- provider: OpenAI
- model: `gpt-5.6-luna`
- status: `EXTERNAL_API_BLOCKER`
- selected source/page: not executed
- answer produced: no
- citations: no
- latency: not executed
- token usage: not executed

## 14. API-key / security result
- `OPENAI_API_KEY present`: no
- no API key printed
- no API key partially displayed
- provider error handling redacts the API key if present

## 15. Acceptance criteria table
| Criterion | Result |
|---|---|
| OpenAI generation implemented | PASS |
| Default model `gpt-5.6-luna` | PASS |
| Responses API used | PASS |
| No Ollama generation fallback | PASS |
| Ollama embeddings retained | PASS |
| Retrieval unchanged | PASS |
| Clarification-first unchanged | PASS |
| Existing Chroma/index retained | PASS |
| Tests pass | PASS |
| Compileall passes | PASS |
| `git diff --check` passes | PASS |
| Normal lightweight CLI works | PASS |
| Normal doctor contamination issue resolved | PASS |
| Live OpenAI smoke executed | BLOCKED externally (missing key) |

## 16. Limitations
- local doctor remains `FAIL` until `OPENAI_API_KEY` is available, because generation readiness is intentionally reported separately and honestly
- live OpenAI request was not possible without runtime key availability
- fingerprint still reports informational `llm_model_differs` versus stored index fingerprint because the stored fingerprint reflects old generation model while retrieval-critical fields still match

## 17. Rollback plan
If rollback is required:
1. reset the working tree to baseline commit `7e68567af09914a7ec3d3bb9cb253e57dc0f8e39`
2. remove staged HRM_046 changes only
3. restore previous generation configuration and tests from the baseline commit
4. do not touch Chroma, embedded tracker, or CE_Library documents during rollback

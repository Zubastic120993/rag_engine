# ORCH_097 — AI Chief Engineer v1 Alpha Shell

**Classification:** IMPLEMENTATION_READY_FOR_REVIEW  
**Timestamp:** 20260810T095122Z  
**task_id:** ORCH_097  
**mode:** IMPLEMENTATION — AI CHIEF ENGINEER V1 ALPHA SHELL

## 1. Baseline

| Item | Value |
|------|-------|
| rag_engine branch | `main` |
| baseline HEAD (pre-delivery) | `202dc94727795eb1867fc64db0f3e584eb039689` (`feat: migrate answer generation to OpenAI`) |
| origin/main at start | matched baseline |
| orchestrator | reference only; product code delivered in `rag_engine` |
| delivery constraint | do not redesign retrieval/ranking/embeddings; no reindex; no benchmark reopen |

## 2. Files modified / added

### Modified
- `app.py` — rebuilt as AI Chief Engineer Alpha Gradio shell
- `README.md` — launch + Alpha UX documentation

### Added
- `rag_engine/chief_ui.py` — presentation helpers over `rag_engine.answer()` (health, clarification shaping, source formatting)
- `tests/test_chief_ui.py` — lightweight UI/integration tests (mocked engine)
- `audit_reports/ORCH_097_AI_CHIEF_ENGINEER_ALPHA_20260810T095122Z.md` — this report

### Not changed (by design)
- retrieval / ranking / embeddings / index
- `rag_engine/query.py` answer semantics
- OpenAI generation module internals
- admin / sync / ingest controls

## 3. UI structure

Local Gradio Blocks app (`app.py`) titled **AI Chief Engineer**:

1. Health strip + refresh
2. Question textbox
3. **Ask** button
4. Status textbox (`ok` / `clarification_required` / `no_coverage` / `error`)
5. Answer textbox with copy button
6. Clarification group (hidden unless required): prompt + confirmation + **Continue**
7. Sources markdown panel
8. Sources copy textbox (document/page/scope/path)

No admin mutation / reindex / sync controls.

## 4. rag_engine integration

```
User question
  -> Gradio UI (app.py)
  -> rag_engine.chief_ui.ask(...)
  -> rag_engine.query.answer(...)
  -> retrieval + clarification + OpenAI generation (engine-owned)
  -> shaped payload rendered in UI
```

Rules enforced:
- UI never calls OpenAI directly
- keys never stored in UI code; optional local `.env` loaded via `python-dotenv` (`override=False`)
- key values never rendered (presence-only health; display sanitization for provider errors)

## 5. Clarification flow

1. Underspecified technical question → engine returns `clarification_required`
2. UI shows clarification prompt and stores pending question in Gradio state
3. User enters confirmation and clicks **Continue**
4. UI re-calls `answer(pending_question, confirmation_text=...)`
5. If still ambiguous, clarification panel stays open; otherwise answer/sources/status update normally

## 6. Source display

For `status == ok` sources:

- document name (basename)
- viewer page (1-based; stored Chroma page is 0-based)
- scope (`collection`)
- path
- open link via existing `source_open_markdown` / Gradio `/file=` route under `CE_LIBRARY_ROOT`
- copyable plain-text source list

## 7. Health / status behavior

Lightweight Alpha health (`chief_ui.health_snapshot`), not full `doctor`:

| Check | Meaning |
|-------|---------|
| `rag_engine_reachable` | library root + DB path exist; package importable |
| `openai_api_key_configured` | env var present (value never shown) |
| `embedding_backend_available` | Ollama reachable and configured embed model listed |

Ask status mapping:
- engine `ok` / `clarification_required` / `no_coverage` / `error` shown directly
- `empty_question` normalized to display status `error`
- missing key → answer text `OpenAI API key not configured`
- rejected/invalid key provider errors sanitized to a clear non-secret message

## 8. Tests

```bash
./run_tests.sh -q
# 172 passed (includes 11 chief_ui tests)
./venv/bin/python -m compileall -q rag_engine app.py
git diff --check -- app.py rag_engine/chief_ui.py tests/test_chief_ui.py README.md
```

Covered focused cases (mocked `answer()`):
1. explicit M 1.3 → ok + source/page render
2. explicit Yanmar → ok + page render
3. vague torque → clarification_required
4. clarification → confirmation_text forwarded (`MAN`)
5. no_coverage messaging
6. missing OpenAI key messaging + health FAIL
7. provider error status
8. source/page rendering (0-based → p.N+1)
9. invalid API key sanitized (no key fragment in UI)
10. `build_app()` constructs without launch

Live smoke (this environment):
- health: `ready` (key present, Ollama embed available)
- vague torque → `clarification_required` ✓
- clarification + `MAN` → engine `no_coverage` (UI rendered correctly; retrieval outcome is engine-owned)
- explicit M 1.3 / Yanmar → generation `error` with invalid local `.env` key (401) → UI sanitized provider-error path ✓
- no_coverage phrasing without equipment can still hit clarification-first gate (engine behavior; mocked no_coverage covered in tests)

Benchmarks: **not run** (task prohibition).

## 9. Launch command

```bash
cd ~/CE_Library/Tools/rag_engine
source venv/bin/activate   # or ./venv/bin/python
export OPENAI_API_KEY=...  # or rely on ignored local .env
python app.py
# open http://127.0.0.1:7861
```

## 10. Limitations (Alpha)

- Single-user local Gradio only
- No PMS / defect / correspondence modules
- No agent framework / multi-user auth / cloud deploy / analytics
- No admin reindex / sync / mutation controls
- No benchmark UI
- Health checks presence/reachability only (does not validate OpenAI key authenticity)
- Clarification UX is one pending question at a time
- Source open links depend on Gradio `allowed_paths=[library_root()]` and browser PDF viewer `#page=N` support

## 11. Commit hash

Not committed in this delivery (changes remain working tree).  
Baseline HEAD remains `202dc94727795eb1867fc64db0f3e584eb039689`.

## 12. Git status (delivery files)

```
 M README.md
 M app.py
?? rag_engine/chief_ui.py
?? tests/test_chief_ui.py
?? audit_reports/ORCH_097_AI_CHIEF_ENGINEER_ALPHA_20260810T095122Z.md
```

(Other pre-existing untracked audit/backup artifacts remain unrelated.)

## 13. Classification

**IMPLEMENTATION_READY_FOR_REVIEW**

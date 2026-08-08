# Session A execution report — 20260808T055946Z

## Preconditions
- `rag_engine` branch: `main`
- Local HEAD: `85928b9c5deb1d4c4a842e0d0d85aeea48c2a3c1`
- `origin/main`: `85928b9c5deb1d4c4a842e0d0d85aeea48c2a3c1`
- Tracked/staged changes before production write: none detected.
- Pre-existing untracked/ignored artefacts present and left untouched.
- Production target confirmed: `/Users/vladymyrzub/CE_Library/.rag_db`
- Retrieval floor confirmed unchanged: `0.38`
- Env override affecting floor: none detected.
- Backup readiness: current pre-run store hashes matched the restore-verified backup state recorded in `CMT_007`.

## Pilot
- Selected derivative: `00_Career/03_Engine_Knowledge/Yanmar_6EY22/Yanmar_6EY22ALW_Spare_Parts_List_YZJ2021-1391_Eng_03895-03898_OCR.pdf`
- Canonical original: `00_Career/03_Engine_Knowledge/Yanmar_6EY22/Yanmar_6EY22ALW_Spare_Parts_List_YZJ2021-1391_Eng_03895-03898.pdf`
- Original SHA-256 before/after: `e3d8a2702fcecf225c4f3ed79ca9f0ce7e6a1e936f70ee1aa56af0aee1219c77`
- Derivative SHA-256: `0281f3271d763a87d3a5767d51336272faa0da0c07cc965f8b2ab614f7a2e7f9`
- Chunks added: `234`
- Stored metadata on pilot chunks: `machine_transcribed=true`, `authority_rank=7`, canonical `source` = original PDF, `raw_source` = `_OCR.pdf` derivative.
- Pilot tracker↔Chroma reconcile: `234` tracker chunk IDs, `234` Chroma rows by `raw_source`, `234` Chroma rows by canonical `source`.

## Batch
- Remaining approved derivatives indexed: `53`
- Success: `53`
- Zero-chunk: `0`
- Failed: `0`
- Skipped by manifest: `X.MLC-SDE010.pdf` (digital-signature issue; no `_OCR.pdf` derivative)
- OCR-derived chunks after full Session A: `4505` across `54` distinct `_OCR.pdf` sources.

## Production inventory
### Before Session A
- `chroma.sqlite3` SHA-256: `1cd76681dbf742353773b90b9b740ed9ed26c35119cc9676bb37071219ee5ad0`
- `embedded.json` SHA-256: `11fe381e94049859983e9115b183b3fb5a372d8c19004c41b98825478c0ef5b2`
- `ask_events.jsonl` lines: `332`
- Tracker-backed embeddings: `108685`
- Tracker entries: `1673`
- OCR-derived sources indexed: `0`
- OCR-derived chunks indexed: `0`
- Historical non-tracker Chroma rows already present before Session A: `1`
  - row type: `doctor persistence probe`
  - metadata: `probe=true`
  - created: `2026-07-22`
  - not associated with Session A
  - not associated with OCR
  - not tracked in `embedded.json`

### After Session A
- `chroma.sqlite3` SHA-256: `061408ce23beee3e3d5b3b11c54e39d77d684fd662d26ea0c07885ba9222b3aa`
- `embedded.json` SHA-256: `eac64839158e80a984048b26657d062e2d53be8ff65de48f4f240a84486453d0`
- `ask_events.jsonl` lines: `386`
- Tracker-backed embeddings after Session A OCR ingest: `113190`
- Total Chroma embedding rows present: `113191`
- Tracker entries: `1727`
- OCR-derived sources indexed: `54`
- OCR-derived chunks indexed: `4505`
- Session A created exactly `4505` OCR chunk rows.
- The higher total Chroma row count results from the single historical doctor probe row above, not from additional Session A ingest.

## Distance capture
- Scope used: `maker-manuals`
- Set A: `47` gold-only positive questions from the five OCR_001 manuals.
- Set B: `6` questions = `GQ-006` gold negative class + `5` explicit maker-manual absence controls.
- Retrieval floor remained unchanged at `0.38`.
- No Session B work performed.
- Logged `ask_events.jsonl` increase during Session A evidence capture: `54` events.
  - `53` events = distance-capture queries recorded in `SESSION_A_DISTANCE_RESULTS_20260808T055946Z.json`
  - `1` event = internal CLI verification query logged at `2026-08-08T05:57:35.740103+00:00`
  - Logging behaviour was expected.
  - The discrepancy was in report arithmetic, not in implementation.

## Independent verification
- HRM_003 independently verified that Session A OCR ingest reconciled exactly.
- Tracker reconciliation passed for all `54` OCR derivatives and all `4505` OCR chunk rows.
- Canonical mapping reconciliation passed: OCR derivatives resolved to original PDFs and derivative `raw_source` traceability was preserved.
- Distance capture reconciliation passed: Set A = `47`, Set B = `6`, and required JSON fields were present.
- Two report arithmetic issues were corrected:
  - total Chroma row count vs tracker-backed OCR ingest count
  - `ask_events.jsonl` increase vs distance-capture query count
- HRM_003 was strictly read-only.
- No production data changed after Session A.
- No production data changed during HRM_003.

## Evidence artifacts
- Batch per-file results JSON: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/SESSION_A_BATCH_RESULTS_20260808T055946Z.json`
- Raw distance results JSON: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/SESSION_A_DISTANCE_RESULTS_20260808T055946Z.json`
- This report: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/SESSION_A_EXECUTION_REPORT_20260808T055946Z.md`

## Classification
- Implementation: `COMPLETE`
- Evidence package: `AMENDED AFTER INDEPENDENT VERIFICATION`

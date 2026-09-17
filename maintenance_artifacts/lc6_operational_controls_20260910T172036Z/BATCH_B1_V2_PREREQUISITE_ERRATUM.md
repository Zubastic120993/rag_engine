# Batch B1 V2 Prerequisite Forward Erratum

Date: 2026-09-11

Historical report preserved unchanged:
`BATCH_B1_PREREQUISITE_V2_REPORT.json`

Historical report SHA-256:
`a4c8cb96b5c2c83d0f5e1e3f330b9dd1a8fbb790f278869f4ad0722e38701731`

## Correction scope

This erratum records additive remediation only inside:
`/Users/vladymyrzub/CE_Library/Tools/rag_engine/maintenance_artifacts/lc6_operational_controls_20260910T172036Z`

Corrected items:

1. Process detection no longer treats a command-line path mention alone as writer evidence.
2. The current inspection process and known wrapper/ancestor chain are excluded by PID identity.
3. Independent ingest, repair, executor, Chroma writer, write-FD holder, or ambiguous path match without reader/writer proof fails closed.
4. Certified baseline canonical source-content inventory excludes `.orphan_repair_disposable_clone.json` as marker evidence.
5. Certified baseline source-content inventory requires exactly 19 rows under `inventory-row-v1` and checksum `b189d622935faf6a0aa14830a80fa73af85c49f2d965bb83cdfb23acdb4f992a`.
6. Certified baseline marker evidence is retained separately with SHA-256 `e148d9b83bbcff31d24f85ad461387457efc994138ff62003aafc2733284d2f1`.

No Batch B1 rerun was performed by this erratum.

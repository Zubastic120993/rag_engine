# RAG Restore Validation — Step 3

UTC timestamp: `20260728T114008Z`
Status: **PASS**
Recoverability classification: **RESTORE PROVEN WITH RESTRICTIONS**

## Restore source and destination
- Backup source: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/backups/rag_db_backup_20260728T112214Z`
- Backup data source: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/backups/rag_db_backup_20260728T112214Z/data`
- Restore destination: `/private/tmp/rag_engine_restore_test/restored_db`
- Step 2 report: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/audit_reports/RAG_BACKUP_CREATION_20260728T112214Z.md`

## Isolation method
- Dedicated temporary validation script under `/private/tmp/rag_engine_restore_test/scripts/`.
- Isolated shell via `env -i` with controlled `HOME`, `TMPDIR`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, and empty `PYTHONPATH`.
- Explicit path opening only; no normal CLI call that could resolve to production `.rag_db`.

## Exact runtime environment
- Python executable: `/Users/vladymyrzub/CE_Library/Tools/rag_engine/venv/bin/python`
- HOME: `/private/tmp/rag_engine_restore_test/temp_home`
- TMPDIR: `/private/tmp/rag_engine_restore_test`
- XDG_CACHE_HOME: `/private/tmp/rag_engine_restore_test/temp_cache`
- XDG_CONFIG_HOME: `/private/tmp/rag_engine_restore_test/temp_config`
- PYTHONPATH: `''`

## Phase A — Pre-restore safety baseline
- Backup readable: `True`
- Backup writable by current user: `False`
- Backup file count: `46`
- Backup total size: `1071292889` bytes
- BACKUP_HASHES validation: `True`
- SHA256SUMS validation: `True`
- Backup SQLite integrity: `ok`
- Production active-write detected: `False`
- Production WAL present before restore: `False`
- Production SHM present before restore: `False`
- Production ingest.lock present before restore: `False`
- Free space under /private/tmp: `545434984448` bytes
- 2x backup size safety margin satisfied: `True`

## Production baseline before restore
- Production file count: `46`
- Production total size: `1071292889` bytes
- Production collection rows: `[{'id': 'e42037ad-ddc7-46c3-aaea-53adb388606b', 'name': 'langchain'}]`
- Production tracker digest count: `1673`
- Production tracker path count: `1747`
- Production orphan rows: `[{'segment_id': '5df5781f-6ede-4906-9546-f9418c3fcfc5', 'n': 1}]`
- Production SQLite integrity: `ok`

## Phase B — Restore copy verification
- Restore file count: `46`
- Restore total size: `1071292889` bytes
- Restore directory count: `2`
- Relative path list match: `True`
- File count match: `True`
- Directory count match: `True`
- Total size match: `True`
- File hash match: `True`

## Phase C/D — SQLite and Chroma validation on restored copy
- Restored SQLite integrity: `ok`
- Restored SQLite tables: `['collection_metadata', 'collections', 'databases', 'embedding_fulltext_search', 'embedding_fulltext_search_config', 'embedding_fulltext_search_content', 'embedding_fulltext_search_data', 'embedding_fulltext_search_docsize', 'embedding_fulltext_search_idx', 'embedding_metadata', 'embeddings', 'embeddings_queue', 'embeddings_queue_config', 'maintenance_log', 'max_seq_id', 'migrations', 'segment_metadata', 'segments', 'tenants']`
- Restored table counts: `{'collection_metadata': 0, 'collections': 1, 'databases': 1, 'embedding_fulltext_search': 108686, 'embedding_fulltext_search_config': 1, 'embedding_fulltext_search_content': 108686, 'embedding_fulltext_search_data': 51700, 'embedding_fulltext_search_docsize': 108686, 'embedding_fulltext_search_idx': 16685, 'embedding_metadata': 434742, 'embeddings': 108686, 'embeddings_queue': 233, 'embeddings_queue_config': 1, 'maintenance_log': 0, 'max_seq_id': 3, 'migrations': 15, 'segment_metadata': 0, 'segments': 2, 'tenants': 1}`
- Restored orphan rows: `[{'segment_id': '5df5781f-6ede-4906-9546-f9418c3fcfc5', 'n': 1}]`
- Visible collections: `['langchain']`
- Expected collection present: `True`
- Restored chunk count: `108685`
- Restored sample IDs: `['8dfaf476-0288-42b5-8fd2-110273503c45', '52eda227-496b-45cb-9461-4ef96b6b2488', 'bd453286-7739-46a7-b73e-58d83d6931ec', '25c743d2-4217-4ca9-a113-f9cdbed3bf48', '83fa0713-80b4-4da3-bf87-f370833d1784']`
- Embedding dimension sample: `1024`
- Vector segment accessible: `True`

## Metadata sampling
- Deterministic sample count: `20`
- Sample comparison match: `True`
- Sample mismatches: `0`

## Phase E — Retrieval comparison
- Query count executed: `10`
- Restored repeated-query determinism: `True`

### exact identifier
- Query: `CORR-SER-ICAF-MAN-002`
- Scope: `None`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['c944fe15-ee37-4cea-b791-7065742c0b6e', '41904b85-366a-4218-b209-10ab6d712ea3', 'd95b239d-f55c-47c3-88ca-403bb20e8fc6', '3b005f9c-1d22-4b5e-aa7a-466a25743a6f', '954b7ded-56e5-4352-9a69-8c9f114b81e3']`
- Production top-K IDs: `['c944fe15-ee37-4cea-b791-7065742c0b6e', '41904b85-366a-4218-b209-10ab6d712ea3', 'd95b239d-f55c-47c3-88ca-403bb20e8fc6', '3b005f9c-1d22-4b5e-aa7a-466a25743a6f', '954b7ded-56e5-4352-9a69-8c9f114b81e3']`
- Restored source paths: `['20_Vessels/Gaschem_Europe/04_Tasks/DEF_ICAF/CORR-SER-ICAF-MAN-002.pdf', '00_Career/03_Engine_Knowledge/ICAF/CORR-SER-ICAF-MAN-001.pdf', '20_Vessels/Gaschem_Europe/04_Tasks/DEF_ICAF/Manual ICAF 16209 YZJ2021-1391 JIANGSU YANGZI-MI V01-421930-5 (1).pdf', '20_Vessels/Gaschem_Europe/04_Tasks/DEF_ICAF/CORR-SER-ICAF-MAN-002.pdf', '00_Career/03_Engine_Knowledge/ICAF/CORR-SER-ICAF-MAN-001.pdf']`
- Production source paths: `['20_Vessels/Gaschem_Europe/04_Tasks/DEF_ICAF/CORR-SER-ICAF-MAN-002.pdf', '00_Career/03_Engine_Knowledge/ICAF/CORR-SER-ICAF-MAN-001.pdf', '20_Vessels/Gaschem_Europe/04_Tasks/DEF_ICAF/Manual ICAF 16209 YZJ2021-1391 JIANGSU YANGZI-MI V01-421930-5 (1).pdf', '20_Vessels/Gaschem_Europe/04_Tasks/DEF_ICAF/CORR-SER-ICAF-MAN-002.pdf', '00_Career/03_Engine_Knowledge/ICAF/CORR-SER-ICAF-MAN-001.pdf']`
- Restored page metadata: `[2, 2, 11, 1, 54]`
- Production page metadata: `[2, 2, 11, 1, 54]`
- Score differences: `[]`

### exact numeric
- Query: `9012745 02_1`
- Scope: `None`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['085ca519-cdb6-492a-9900-32106fa00bd3', '9d750ad3-0f59-4c5f-a5cd-35a102690530', '69710b1e-012f-40af-b3c5-04857a6489f0', 'f1387769-b4ef-40b0-83c6-89f1d9e47723', 'e2c6f312-8889-4f7c-8bf9-3df1ea881c8f']`
- Production top-K IDs: `['085ca519-cdb6-492a-9900-32106fa00bd3', '9d750ad3-0f59-4c5f-a5cd-35a102690530', '69710b1e-012f-40af-b3c5-04857a6489f0', 'f1387769-b4ef-40b0-83c6-89f1d9e47723', 'e2c6f312-8889-4f7c-8bf9-3df1ea881c8f']`
- Restored source paths: `['00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/Operation/ASTM_Tables/NBS_Circular_C410_National_Standard_Petroleum_Oil_Tables.pdf', '00_Career/03_Engine_Knowledge/Operation/ASTM_Tables/NBS_Circular_C410_National_Standard_Petroleum_Oil_Tables.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Instruction_Book/VOLUME II.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf']`
- Production source paths: `['00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/Operation/ASTM_Tables/NBS_Circular_C410_National_Standard_Petroleum_Oil_Tables.pdf', '00_Career/03_Engine_Knowledge/Operation/ASTM_Tables/NBS_Circular_C410_National_Standard_Petroleum_Oil_Tables.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Instruction_Book/VOLUME II.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf']`
- Restored page metadata: `[1205, 131, 128, 397, 802]`
- Production page metadata: `[1205, 131, 128, 397, 802]`
- Score differences: `[]`

### maker/model
- Query: `Yanmar 6EY22(A)LWS`
- Scope: `None`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['434076c9-8aaf-47c1-a09e-477ae3f64532', '1c171eb1-6170-48c6-972f-11fe3f054185', '19e8b21d-0853-4462-9e2e-e8676ec25698', '23695c11-2a49-443e-92bb-5e7cead2ba5f', '549bf4cb-4e26-4ddc-8637-ecc4aa2ebc7d']`
- Production top-K IDs: `['434076c9-8aaf-47c1-a09e-477ae3f64532', '1c171eb1-6170-48c6-972f-11fe3f054185', '19e8b21d-0853-4462-9e2e-e8676ec25698', '23695c11-2a49-443e-92bb-5e7cead2ba5f', '549bf4cb-4e26-4ddc-8637-ecc4aa2ebc7d']`
- Restored source paths: `['00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '20_Vessels/Gaschem_Europe/10_Reference/Different/Cooling Fan/(8000-1-00300)YWF.A4S-300B-5DⅠA00  A0-CN(EN)规格书(20241230).pdf']`
- Production source paths: `['00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '20_Vessels/Gaschem_Europe/10_Reference/Different/Cooling Fan/(8000-1-00300)YWF.A4S-300B-5DⅠA00  A0-CN(EN)规格书(20241230).pdf']`
- Restored page metadata: `[2, 552, 72, 173, 0]`
- Production page metadata: `[2, 552, 72, 173, 0]`
- Score differences: `[]`

### vessel-specific
- Query: `Gaschem Europe`
- Scope: `vessels`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['c768c3aa-0b7d-4d06-9a58-9ffd9d117d32', '5a7c5284-6a0b-4d9a-acd9-4c464c7c515a', 'f5e78642-1055-4941-9d4a-aa84354dd4c2', 'e1cad4b5-2d45-4a09-9683-f795570567b3', 'f8cc1436-35b7-40f8-be6b-1e53ec0dd389']`
- Production top-K IDs: `['c768c3aa-0b7d-4d06-9a58-9ffd9d117d32', '5a7c5284-6a0b-4d9a-acd9-4c464c7c515a', 'f5e78642-1055-4941-9d4a-aa84354dd4c2', 'e1cad4b5-2d45-4a09-9683-f795570567b3', 'f8cc1436-35b7-40f8-be6b-1e53ec0dd389']`
- Restored source paths: `['20_Vessels/Gaschem_Europe/02_Certificates/Calibration_Certificates/4027086_original.pdf', '20_Vessels/Gaschem_Europe/02_Certificates/Calibration_Certificates/4024789_original.pdf', '20_Vessels/Gaschem_Europe/07_Bunkering_Fuel/Fuel_Changeover_SOPs/HGC_OFFICIAL_ME_HFO_MGO_Changeover_Procedure_GC_Europe_2023-12-10_SEARCHABLE.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Accomodation Fan 02.01.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Fixed Gas Detection System 02.16.pdf']`
- Production source paths: `['20_Vessels/Gaschem_Europe/02_Certificates/Calibration_Certificates/4027086_original.pdf', '20_Vessels/Gaschem_Europe/02_Certificates/Calibration_Certificates/4024789_original.pdf', '20_Vessels/Gaschem_Europe/07_Bunkering_Fuel/Fuel_Changeover_SOPs/HGC_OFFICIAL_ME_HFO_MGO_Changeover_Procedure_GC_Europe_2023-12-10_SEARCHABLE.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Accomodation Fan 02.01.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Fixed Gas Detection System 02.16.pdf']`
- Restored page metadata: `[0, 0, 0, 0, 0]`
- Production page metadata: `[0, 0, 0, 0, 0]`
- Score differences: `[]`

### service-letter
- Query: `service letter`
- Scope: `None`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['fa5fc9fe-133e-41c4-9570-fcd31b50fe31', '5b122040-f4e3-4a2d-bff3-4de576adbae9', '9892a103-df6e-4d18-bbf2-c69987ceec6d', '9adc9f92-0327-4800-a5fd-bfbf9e064267', '1df56991-3d07-4047-b9db-5b626176cfdf']`
- Production top-K IDs: `['fa5fc9fe-133e-41c4-9570-fcd31b50fe31', '5b122040-f4e3-4a2d-bff3-4de576adbae9', '9892a103-df6e-4d18-bbf2-c69987ceec6d', '9adc9f92-0327-4800-a5fd-bfbf9e064267', '1df56991-3d07-4047-b9db-5b626176cfdf']`
- Restored source paths: `['00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2024-760.pdf', '00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2018-660.pdf', '00_Career/03_Engine_Knowledge/Training/Kurs/Day 1/2_Cylinder Condition_LGIP Owners Forum_Dec 2022.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/VOL1_small.pdf', '00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2021-717.pdf']`
- Production source paths: `['00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2024-760.pdf', '00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2018-660.pdf', '00_Career/03_Engine_Knowledge/Training/Kurs/Day 1/2_Cylinder Condition_LGIP Owners Forum_Dec 2022.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/VOL1_small.pdf', '00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2021-717.pdf']`
- Restored page metadata: `[1, 0, 31, 23, 0]`
- Production page metadata: `[1, 0, 31, 23, 0]`
- Score differences: `[]`

### page/citation
- Query: `Vision III Oil Mist Detection System page 6`
- Scope: `None`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['7dd6e654-aec8-4d87-9708-8ab53120d0ff', '1f6bbcce-3844-4770-b189-689c4d3f86f8', 'd2705997-08b9-49bb-80f9-1ef0fd716af9', '43c773eb-2352-4995-baa1-99771ead61b1', '8f49db88-678d-4214-af25-12a13e9daaf3']`
- Production top-K IDs: `['7dd6e654-aec8-4d87-9708-8ab53120d0ff', '1f6bbcce-3844-4770-b189-689c4d3f86f8', 'd2705997-08b9-49bb-80f9-1ef0fd716af9', '43c773eb-2352-4995-baa1-99771ead61b1', '8f49db88-678d-4214-af25-12a13e9daaf3']`
- Restored source paths: `['00_Career/01_Class_Rules/IACS_and_Other/UR_M67_Rev2.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/01_Class_Rules/IACS_and_Other/UR_M67_Rev2.pdf', '00_Career/02_Statutory/SIRE_OCIMF/SIRE_2_0/SIRE-2.0-Question-Library-Part-2-Chapters-8-to-12-Version-1.0-January-2022.pdf', '10_Company/Hartmann/SMS_IMM/X.VIR-E001B.pdf']`
- Production source paths: `['00_Career/01_Class_Rules/IACS_and_Other/UR_M67_Rev2.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/01_Class_Rules/IACS_and_Other/UR_M67_Rev2.pdf', '00_Career/02_Statutory/SIRE_OCIMF/SIRE_2_0/SIRE-2.0-Question-Library-Part-2-Chapters-8-to-12-Version-1.0-January-2022.pdf', '10_Company/Hartmann/SMS_IMM/X.VIR-E001B.pdf']`
- Restored page metadata: `[4, 319, 3, 526, 526]`
- Production page metadata: `[4, 319, 3, 526, 526]`
- Score differences: `[]`

### no-answer
- Query: `ZXQ-VOID-QUERY-987654321`
- Scope: `None`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['6695fc24-3489-4d97-8224-827db2f19a9a', 'ed072f7a-0e8c-4117-9f31-2d01502bd06b', 'e2c6f312-8889-4f7c-8bf9-3df1ea881c8f', '56cf4e75-880d-4a3a-a2a4-94c92ba550b5', '9b8567fd-7dd5-405a-a092-4a66ebdaae2f']`
- Production top-K IDs: `['6695fc24-3489-4d97-8224-827db2f19a9a', 'ed072f7a-0e8c-4117-9f31-2d01502bd06b', 'e2c6f312-8889-4f7c-8bf9-3df1ea881c8f', '56cf4e75-880d-4a3a-a2a4-94c92ba550b5', '9b8567fd-7dd5-405a-a092-4a66ebdaae2f']`
- Restored source paths: `['00_Career/03_Engine_Knowledge/Operation/ASTM_Tables/NBS_Circular_C410_National_Standard_Petroleum_Oil_Tables.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/Series_Drawings/Boiler_As_Built.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf']`
- Production source paths: `['00_Career/03_Engine_Knowledge/Operation/ASTM_Tables/NBS_Circular_C410_National_Standard_Petroleum_Oil_Tables.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/Series_Drawings/Boiler_As_Built.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf']`
- Restored page metadata: `[49, 755, 802, 177, 774]`
- Production page metadata: `[49, 755, 802, 177, 774]`
- Score differences: `[]`

### ambiguous cross-vessel
- Query: `Gaschem Africa Gaschem Europe manual`
- Scope: `vessels`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['e1cad4b5-2d45-4a09-9683-f795570567b3', 'f8cc1436-35b7-40f8-be6b-1e53ec0dd389', 'ac878c62-516e-4651-a696-bf58a9f19794', '6caaa31e-e89c-40b1-a1b8-a880596d22d9', 'c768c3aa-0b7d-4d06-9a58-9ffd9d117d32']`
- Production top-K IDs: `['e1cad4b5-2d45-4a09-9683-f795570567b3', 'f8cc1436-35b7-40f8-be6b-1e53ec0dd389', 'ac878c62-516e-4651-a696-bf58a9f19794', '6caaa31e-e89c-40b1-a1b8-a880596d22d9', 'c768c3aa-0b7d-4d06-9a58-9ffd9d117d32']`
- Restored source paths: `['20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Accomodation Fan 02.01.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Fixed Gas Detection System 02.16.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Emergency POwer Source -Emergency Batteries 02.08.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Emergency Trips - Ventilation & FO-LO pumps 02.10.pdf', '20_Vessels/Gaschem_Europe/02_Certificates/Calibration_Certificates/4027086_original.pdf']`
- Production source paths: `['20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Accomodation Fan 02.01.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Fixed Gas Detection System 02.16.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Emergency POwer Source -Emergency Batteries 02.08.pdf', '20_Vessels/Gaschem_Africa/05_PMS/Critical_Spares/Critical Spares/Emergency Trips - Ventilation & FO-LO pumps 02.10.pdf', '20_Vessels/Gaschem_Europe/02_Certificates/Calibration_Certificates/4027086_original.pdf']`
- Restored page metadata: `[0, 0, 0, 0, 0]`
- Production page metadata: `[0, 0, 0, 0, 0]`
- Score differences: `[]`

### multi-term technical
- Query: `fuel conditioning module alarms and fault finding`
- Scope: `None`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['9e648ac6-9f64-4619-8c3d-b156634acb32', 'c635640e-2608-47f8-b99f-18ff05cef482', '3dafcd5f-2cb7-449d-b6b9-abd895809f98', '10709711-f327-47ea-a863-df7628b87cfa', '43c08eca-5d01-4f13-be67-36fe100ce1db']`
- Production top-K IDs: `['9e648ac6-9f64-4619-8c3d-b156634acb32', 'c635640e-2608-47f8-b99f-18ff05cef482', '3dafcd5f-2cb7-449d-b6b9-abd895809f98', '10709711-f327-47ea-a863-df7628b87cfa', '43c08eca-5d01-4f13-be67-36fe100ce1db']`
- Restored source paths: `['00_Career/03_Engine_Knowledge/Separator_AlfaLaval_P615/50_586660_02_V6_Alarms P.pdf', '00_Career/03_Engine_Knowledge/Separator_AlfaLaval_S926/50_584614_02_v7 alarms and fault finding.pdf', '20_Vessels/Gaschem_Europe/01_Manuals/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/1.5 S8 9013009 02_1 Alarms and Fault Finding.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/FORUM_Two_Stroke_Operation/AlarmsReference_1609_LGI.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/FORUM_Two_Stroke_Operation/AlarmsReference_1609_LGI.pdf']`
- Production source paths: `['00_Career/03_Engine_Knowledge/Separator_AlfaLaval_P615/50_586660_02_V6_Alarms P.pdf', '00_Career/03_Engine_Knowledge/Separator_AlfaLaval_S926/50_584614_02_v7 alarms and fault finding.pdf', '20_Vessels/Gaschem_Europe/01_Manuals/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/1.5 S8 9013009 02_1 Alarms and Fault Finding.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/FORUM_Two_Stroke_Operation/AlarmsReference_1609_LGI.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/FORUM_Two_Stroke_Operation/AlarmsReference_1609_LGI.pdf']`
- Restored page metadata: `[0, 0, 0, 98, 292]`
- Production page metadata: `[0, 0, 0, 98, 292]`
- Score differences: `[]`

### repeated determinism check
- Query: `Yanmar 6EY22(A)LWS`
- Scope: `None`
- PASS: `True`
- Order matches: `True`
- Set matches: `True`
- Restored top-K IDs: `['434076c9-8aaf-47c1-a09e-477ae3f64532', '1c171eb1-6170-48c6-972f-11fe3f054185', '19e8b21d-0853-4462-9e2e-e8676ec25698', '23695c11-2a49-443e-92bb-5e7cead2ba5f', '549bf4cb-4e26-4ddc-8637-ecc4aa2ebc7d']`
- Production top-K IDs: `['434076c9-8aaf-47c1-a09e-477ae3f64532', '1c171eb1-6170-48c6-972f-11fe3f054185', '19e8b21d-0853-4462-9e2e-e8676ec25698', '23695c11-2a49-443e-92bb-5e7cead2ba5f', '549bf4cb-4e26-4ddc-8637-ecc4aa2ebc7d']`
- Restored source paths: `['00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '20_Vessels/Gaschem_Europe/10_Reference/Different/Cooling Fan/(8000-1-00300)YWF.A4S-300B-5DⅠA00  A0-CN(EN)规格书(20241230).pdf']`
- Production source paths: `['00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/FITTING and ACC.pdf', '00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0CH50-M00231_en.pdf', '20_Vessels/Gaschem_Europe/10_Reference/Different/Cooling Fan/(8000-1-00300)YWF.A4S-300B-5DⅠA00  A0-CN(EN)规格书(20241230).pdf']`
- Restored page metadata: `[2, 552, 72, 173, 0]`
- Production page metadata: `[2, 552, 72, 173, 0]`
- Score differences: `[]`

## Phase F — Restore independence test
- Restore root resolved path: `/private/tmp/rag_engine_restore_test/restored_db`
- Production .rag_db references during restore-only open-file scan: `[]`
- Production .rag_db path hits in runtime outputs: `0`
- Independence note: Original document source paths under /Users/vladymyrzub/CE_Library may appear in metadata. Production .rag_db path must not be used as restore runtime path.

## Phase G — Production non-modification verification
- Production hashes unchanged: `True`
- Production file count unchanged: `True`
- Production total size unchanged: `True`
- Production SQLite integrity unchanged: `True`
- Production tracker counts unchanged: `True`
- Project git status unchanged: `True`
- eval/last_results.json unchanged: `True`
- Production WAL present after test: `False`
- Production SHM present after test: `False`

## Phase H — Recovery timing
- Backup verification time: `13.524` s
- Restore copy time: `3.562` s
- Database validation time: `6.722` s
- Retrieval validation time: `2.657` s
- Total recovery validation time: `32.904` s

## Known orphan row baseline
- Known orphan segment id: `5df5781f-6ede-4906-9546-f9418c3fcfc5`
- Orphan rows observed after restore: `[{'segment_id': '5df5781f-6ede-4906-9546-f9418c3fcfc5', 'n': 1}]`

## scopes.yaml isolation limitation
- Normal CLI isolation was intentionally not used because this validation was executed through an explicit temporary script with hardcoded restore paths and a clean environment.
- This is treated as a remaining restriction before Step 4.

## Cleanup status
- Temporary restore preserved pending review

## Restrictions remaining before Step 4
- None beyond the temporary-script isolation requirement documented above.

## Errors
- None.

## PASS / FAIL / BLOCKED
**PASS**

## Final recoverability statement
**RESTORE PROVEN WITH RESTRICTIONS**

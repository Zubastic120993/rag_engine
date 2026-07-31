# RAG Metadata Gap Analysis — 20260731T065913Z

## Operational question gap analysis
| Operational question | Current capability | Why / missing fields |
| --- | --- | --- |
| Show only Gaschem Europe documents | Unsafe due to ambiguity | No dedicated vessel field; current `collection=vessels` mixes Gaschem Europe and Gaschem Africa. Filtering by `source` path prefix is ad hoc, not governed. |
| Exclude Gaschem Africa records | Unsafe due to ambiguity | No canonical vessel-applicability metadata or negative vessel filter. |
| Find maker instructions for a specific equipment model | Supported partially | Possible by semantic retrieval and path hints, but no governed `maker` or `model` field exists. |
| Prefer vessel-specific approved procedures over generic manuals | Supported partially | Current system can separate `vessels` vs `maker-manuals` collections, but lacks approval/authority fields and revision control. |
| Identify the latest valid revision | Unsupported | No canonical `revision`, `effective_date`, or `status` field. |
| Exclude superseded documents | Unsupported | No `superseded_by`, `supersedes`, or controlled document-status model. |
| Find all documents affecting a particular component | Supported partially | Possible only by text/path search; no governed component/equipment taxonomy. |
| Link a defect to PMS job, requisition, correspondence, report, and photo | Unsupported | No relationship IDs or cross-record entity model. |
| Find documents applicable during manoeuvring | Unsupported | No `operational_phase` field. |
| Find documents concerning gas mode only | Unsupported | No `operational_mode` field. |
| Find safety-critical instructions | Unsupported | No `safety_criticality` or authority risk metadata. |
| Separate statutory authority from maker recommendation | Unsafe due to ambiguity | Current `collection` helps at high level but no authoritative ranking or document-type governance exists. |
| Separate company requirement from onboard practice | Unsafe due to ambiguity | No `authority_class`, `approval_status`, or note-vs-source metadata in Chroma. |
| Identify whether a statement came from an official source, correspondence, or user note | Unsafe due to ambiguity | Can sometimes be inferred from path, but not guaranteed by explicit metadata. |
| Filter by vessel, sister vessel, fleet-wide, or generic applicability | Unsupported | No explicit applicability fields. |
| Find all evidence related to a historical failure | Unsupported | No event/defect relationship graph or stable event identifiers. |
| Find all relevant service letters for a model | Supported partially | Service-letter archive path helps, but no model linkage field exists. |
| Identify effective dates and superseded versions | Unsupported | No controlled revision/effective-date metadata. |
| Link Obsidian notes to authoritative source pages | Supported partially | Possible manually through source sections, but no stable source-note relationship metadata. |
| Support stable citations after a file is moved | Supported partially | Possible only via explicit reconcile workflow; no path-independent stable document identity currently exposed. |

## Major completeness findings
- Observed production chunk-metadata fields: `3`
- Current reliable filter field in normal retrieval path: `collection` only.
- Current reliable citation fields: `source` and `page`.
- Current loader-derived PDF metadata fields are opportunistic rather than governed.
- No explicit document-control, authority, applicability, or relationship model exists in chunk metadata.

## Highest-risk metadata gaps
- No stable document/version/chunk identity inside production chunk metadata.
- No explicit vessel applicability model; current vessel separation is path-derived and unsafe for fleet filtering.
- No authority/governance fields to rank statutory, company, vessel-approved, maker, correspondence, or note sources safely.
- No revision/effective-date/supersession model.
- No relationship model linking defects, PMS, requisitions, correspondence, photos, and notes.
- No governed equipment, system, maker, or model taxonomy.

## Current metadata capability summary
**What exists and works today**
- Scope routing at collection level.
- Source-path and page traceability for citations.
- Digest-keyed tracker state for file-byte identity outside retrieval responses.
- Basic build fingerprint and ask-event provenance.

**What cannot currently be answered safely from metadata alone**
- vessel-specific filtering inside the shared `vessels` scope;
- authority ranking across statutory/company/vessel/maker/note sources;
- latest-valid revision detection;
- superseded-document exclusion;
- stable cross-record relationship retrieval;
- stable citations after path changes without a special reconcile action.

## Representative class implications
Deterministic sampled classes found in the library structure:
{
  "Photo or media-linked record": ".rag_db/intake_renders/05〈薬注装置CPI〉S833-080-9010(英)_p006-06.png",
  "Requisition or spare-parts record": ".rag_db/intake_renders/80_9035353_02_v4 Spare parts catalogue_p001-01.png",
  "Technical correspondence": ".rag_db/intake_renders/CORR-SER-ICAF-MAN-002_p002-02.png",
  "Drawing or diagram": ".rag_db/intake_renders/Final drawing for YZJ2021-1391 water mist system_p004-004.png",
  "Inspection/work report": "00_Career/01_Class_Rules/DNV/DNV_Maritime_Forecast_to_2050_2025_report.pdf",
  "Certificate or statutory document": "00_Career/02_Statutory/Flag_State/ClassNK_TEC-1236_EEBD_Panama_Flag.pdf",
  "Maker instruction manual": "00_Career/03_Engine_Knowledge/BWMS/General User Manual V2.6.6.0-YZJ2021-1391.pdf",
  "MAN service letter": "00_Career/03_Engine_Knowledge/Service_Letters_Four_Stroke/sl2026-783.pdf",
  "Handover document": "00_Career/04_Templates/Handover/CE_Handover_Air_Conditioning.docx",
  "PMS or maintenance record": "00_Career/04_Templates/PMS_Remarks/Africa_examples/Job Cards/PMS_Remark_HP_SCR_Standby_Heater_Check.docx",
  "Defect report": "00_Career/04_Templates/PMS_Remarks/OWS_Touch_panel_Defect.rtf",
  "Safety data sheet": "00_Career/07_SDS_Datasheets/00_Index/chemical_sds_status_index.csv",
  "Company/SMS instruction": "10_Company/Hartmann/SMS_IMM/000000.pdf",
  "Vessel-specific operating procedure": "20_Vessels/Gaschem_Africa/06_Letters/OMD24_Operation_Procedure_Updated.docx",
  "Obsidian or CE Wiki note": "90_CE_Wiki/00_CE_Wiki_Quality_Checklist.md"
}

These samples show that useful metadata is currently dispersed across:
- folder hierarchy;
- filename patterns;
- PDF loader document-info fields;
- Markdown governance notes;
- tracker sidecar state;
- human interpretation.

This dispersion is the core reason current metadata support is partial or unsafe for Chief Engineer operational filtering.

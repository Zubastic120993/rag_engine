"""Authority ranking and OCR/citation normalization for retrieval."""

from __future__ import annotations

from pathlib import PurePosixPath

OCR_SUFFIX = "_ocr.pdf"

DOC_TYPE_OPERATION_MANUAL = "operation_manual"
DOC_TYPE_SPARE_PARTS = "spare_parts_catalogue"
DOC_TYPE_MAKER_MANUAL = "maker_manual"
DOC_TYPE_SERVICE_LETTER = "service_letter"
DOC_TYPE_TRAINING = "training"
DOC_TYPE_REFERENCE = "reference"
DOC_TYPE_DRAWING = "drawing_set"
DOC_TYPE_NOTE = "note"

# Lower number = stronger citation authority.
RANK_REGULATORY = 1
RANK_COMPANY = 2
RANK_MAKER = 3
RANK_VESSEL = 4
RANK_REFERENCE = 5
RANK_NOTE = 6
RANK_MACHINE = 7


def _norm(source: str | None) -> str:
    return str(source or "").strip().replace("\\", "/")


def canonical_source_path(source: str | None) -> str:
    src = _norm(source)
    if not src:
        return ""
    p = PurePosixPath(src)
    name = p.name
    lower = name.lower()
    if lower.endswith(OCR_SUFFIX):
        base = name[: -len(OCR_SUFFIX)] + ".pdf"
        return str(p.with_name(base))
    return src


def is_machine_transcribed_source(source: str | None) -> bool:
    name = PurePosixPath(_norm(source)).name.lower()
    return bool(name) and name.endswith(OCR_SUFFIX)


def _document_class_rank(source: str | None) -> int:
    src = _norm(source)
    low = src.lower()
    if not low:
        return RANK_REFERENCE
    if low.startswith("00_career/02_statutory/") or low.startswith("00_career/01_class_rules/"):
        return RANK_REGULATORY
    if low.startswith("10_company/"):
        return RANK_COMPANY
    if low.startswith("90_ce_wiki/"):
        return RANK_NOTE
    if low.startswith("20_vessels/"):
        if "/01_manuals/" in low or "/10_reference/" in low:
            return RANK_MAKER
        return RANK_VESSEL
    if low.startswith("00_career/03_engine_knowledge/"):
        if "/training/" in low or "/forum_" in low:
            return RANK_REFERENCE
        return RANK_MAKER
    if low.startswith("00_career/07_sds_datasheets/"):
        return RANK_REFERENCE
    return RANK_REFERENCE


def authority_rank_for_source(source: str | None) -> int:
    src = _norm(source)
    if is_machine_transcribed_source(src):
        return RANK_MACHINE
    return _document_class_rank(src)


def canonical_authority_rank_for_source(source: str | None) -> int:
    return _document_class_rank(canonical_source_path(source) or _norm(source))


def authority_family_for_source(source: str | None) -> str:
    src = canonical_source_path(source) or _norm(source)
    if not src:
        return ""
    path = PurePosixPath(src)
    parts = path.parts
    low = src.lower()
    if low.startswith("00_career/03_engine_knowledge/") and len(parts) >= 3:
        family = parts[2]
        if family == "Training" and len(parts) >= 4:
            return f"Training/{parts[3]}"
        return family
    if low.startswith("00_career/07_sds_datasheets/"):
        return "00_Career/07_SDS_Datasheets"
    if low.startswith("10_company/") and len(parts) >= 2:
        return parts[1]
    if low.startswith("20_vessels/") and len(parts) >= 3:
        return "/".join(parts[:3])
    return parts[0] if parts else ""


def document_type_for_source(source: str | None) -> str:
    src = canonical_source_path(source) or _norm(source)
    low = src.lower()
    if not low:
        return DOC_TYPE_REFERENCE
    if low.startswith("90_ce_wiki/"):
        return DOC_TYPE_NOTE
    if "/service_letters_" in low or "/service_letter" in low:
        return DOC_TYPE_SERVICE_LETTER
    if "/training/" in low or "/forum_" in low:
        return DOC_TYPE_TRAINING
    if "spare parts" in low or "spare_parts" in low or "parts list" in low or "parts_list" in low or "catalogue" in low or "catalog" in low:
        return DOC_TYPE_SPARE_PARTS
    if "/series_drawings/" in low or "drawings" in low:
        return DOC_TYPE_DRAWING
    if low.startswith("00_career/07_sds_datasheets/") or "/10_reference/" in low or "param list" in low or "reference" in low:
        return DOC_TYPE_REFERENCE
    if "operation manual" in low or "instruction manual" in low or "operating, maintenance manual" in low:
        return DOC_TYPE_OPERATION_MANUAL
    if low.startswith("00_career/03_engine_knowledge/") or low.startswith("20_vessels/"):
        return DOC_TYPE_MAKER_MANUAL
    return DOC_TYPE_REFERENCE


def document_type_rank_for_source(source: str | None) -> int:
    doc_type = document_type_for_source(source)
    return {
        DOC_TYPE_OPERATION_MANUAL: 1,
        DOC_TYPE_SPARE_PARTS: 1,
        DOC_TYPE_MAKER_MANUAL: 2,
        DOC_TYPE_SERVICE_LETTER: 3,
        DOC_TYPE_TRAINING: 4,
        DOC_TYPE_REFERENCE: 5,
        DOC_TYPE_DRAWING: 6,
        DOC_TYPE_NOTE: 7,
    }.get(doc_type, 5)


def enrich_metadata(metadata: dict | None) -> dict:
    meta = dict(metadata or {})
    source = _norm(meta.get("source"))
    raw_source = _norm(meta.get("raw_source")) or source
    canonical = canonical_source_path(raw_source)
    meta["source"] = canonical or source
    if raw_source and canonical and canonical != raw_source:
        meta["raw_source"] = raw_source
    meta["authority_rank"] = authority_rank_for_source(raw_source)
    meta["canonical_authority_rank"] = canonical_authority_rank_for_source(raw_source)
    meta["document_type"] = document_type_for_source(raw_source)
    meta["document_type_rank"] = document_type_rank_for_source(raw_source)
    meta["authority_family"] = authority_family_for_source(raw_source)
    meta["machine_transcribed"] = is_machine_transcribed_source(raw_source)
    return meta

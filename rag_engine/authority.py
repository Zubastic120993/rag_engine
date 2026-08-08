"""Authority ranking and OCR/citation normalization for retrieval."""

from __future__ import annotations

from pathlib import PurePosixPath

OCR_SUFFIX = "_ocr.pdf"

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
    meta["machine_transcribed"] = is_machine_transcribed_source(raw_source)
    return meta

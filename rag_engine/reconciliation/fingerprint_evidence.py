"""Historical chunking fingerprint / extractor evidence classification.

Critical rule: current/default chunking settings are NOT historical settings.
Stable chunk_id derivation requires an explicitly provided historical fingerprint.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from rag_engine.reconciliation.models import (
    ExtractorVersionStatus,
    FingerprintEvidenceStatus,
)


def classify_fingerprint_evidence(
    *,
    explicit_historical_fingerprint: str | None = None,
    index_fingerprint: Mapping[str, Any] | None = None,
    allow_cohort_inference: bool = False,
) -> dict[str, Any]:
    """Classify whether a historical chunking_fingerprint is proven.

    ``index_fingerprint.json`` stores embed/chunk_size/overlap/normalization but
    is **not** the stable-id-v1 chunking_contract fingerprint (missing separators,
    min/max chars, extractor, extractor_version, identity_scheme_version).

    Therefore presence of index_fingerprint alone ⇒ UNKNOWN (or AMBIGUOUS if
    conflicting signals), never KNOWN_EXACT via current defaults.
    """
    if explicit_historical_fingerprint:
        fp = str(explicit_historical_fingerprint).strip().lower()
        if len(fp) == 64 and all(c in "0123456789abcdef" for c in fp):
            return {
                "status": FingerprintEvidenceStatus.KNOWN_EXACT.value,
                "fingerprint": fp,
                "basis": "explicit_historical_fingerprint",
                "used_current_defaults": False,
            }
        return {
            "status": FingerprintEvidenceStatus.AMBIGUOUS.value,
            "fingerprint": None,
            "basis": "explicit_fingerprint_malformed",
            "used_current_defaults": False,
        }

    if index_fingerprint:
        # Partial cohort signal only — insufficient for stable chunk_id.
        if allow_cohort_inference:
            return {
                "status": FingerprintEvidenceStatus.KNOWN_BY_COHORT.value,
                "fingerprint": None,
                "basis": "index_fingerprint_partial_cohort_not_stable_id_contract",
                "index_fingerprint": dict(index_fingerprint),
                "used_current_defaults": False,
                "note": (
                    "index_fingerprint.json is not the Spec §7.1 chunking_fingerprint; "
                    "stable chunk_id remains unresolved"
                ),
            }
        return {
            "status": FingerprintEvidenceStatus.UNKNOWN.value,
            "fingerprint": None,
            "basis": "index_fingerprint_insufficient_for_stable_chunk_id",
            "index_fingerprint": {
                k: index_fingerprint.get(k)
                for k in (
                    "embed_model",
                    "chunk_size",
                    "chunk_overlap",
                    "normalization",
                    "llm_model",
                )
            },
            "used_current_defaults": False,
        }

    return {
        "status": FingerprintEvidenceStatus.UNKNOWN.value,
        "fingerprint": None,
        "basis": "no_historical_fingerprint_evidence",
        "used_current_defaults": False,
    }


def classify_extractor_version(
    *,
    explicit_extractor_version: str | None = None,
    index_fingerprint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if explicit_extractor_version:
        return {
            "status": ExtractorVersionStatus.KNOWN.value,
            "extractor_version": explicit_extractor_version,
            "impact": "none_for_tracker_chroma_join",
        }
    if index_fingerprint:
        return {
            "status": ExtractorVersionStatus.PARTIAL.value,
            "extractor_version": None,
            "impact": (
                "does_not_block_tracker_chroma_id_join; "
                "blocks_stable_chunk_id_without_explicit_contract"
            ),
        }
    return {
        "status": ExtractorVersionStatus.UNKNOWN.value,
        "extractor_version": None,
        "impact": (
            "does_not_block_tracker_chroma_id_join; "
            "blocks_stable_chunk_id_derivation"
        ),
    }


def read_index_fingerprint_readonly(path: str | Path) -> dict[str, Any] | None:
    """Read index_fingerprint.json read-only; return None if missing/invalid."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None

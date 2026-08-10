from __future__ import annotations

import sys
from pathlib import Path

from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine import query
from rag_engine.config import load_registry


class _FakeDoc:
    def __init__(self, source: str, page: int = 1, collection: str = "maker-manuals"):
        self.metadata = {"source": source, "page": page, "collection": collection}
        self.page_content = "chunk"


def _clear_scope_registry_cache() -> None:
    load_registry.cache_clear()


def test_sources_report_original_pdf_and_machine_transcribed_for_ocr_hit():
    pairs = [((_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/manual_OCR.pdf")), 0.25)]

    sources = query._sources_from_pairs(pairs)

    assert sources == [
        {
            "path": "00_Career/03_Engine_Knowledge/Yanmar_6EY22/manual.pdf",
            "page": 2,
            "page_index": 1,
            "collection": "maker-manuals",
            "distance": 0.25,
            "authority_rank": 7,
            "machine_transcribed": True,
        }
    ]


def test_retrieve_with_scores_keeps_hits_beyond_score_floor_for_reranking(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Training/a.pdf"), 0.41),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Training/b.pdf"), 0.48),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.40)

    pairs = query.retrieve_with_scores("some question")

    assert [distance for _doc, distance in pairs] == [0.41, 0.48]


def test_retrieve_with_scores_and_diagnostics_preserves_prefloor_candidates(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf",
                page=10,
            ),
            0.559094,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22(A)LWS(H.F.O.／MET)_0AE22-EN0100 May.2023-10.pdf",
                page=20,
            ),
            0.559508,
        ),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    pairs, diagnostics = query.retrieve_with_scores_and_diagnostics(
        "some question", scope="maker-manuals", k=5
    )

    assert [round(distance, 6) for _doc, distance in pairs] == [0.559094, 0.559508]
    assert diagnostics["raw_count"] == 2
    assert diagnostics["post_admissibility_count"] == 2
    assert diagnostics["post_scope_count"] == 2
    assert diagnostics["post_rerank_count"] == 2
    assert diagnostics["post_dedupe_count"] == 2
    assert diagnostics["gate"] is None


def test_final_confidence_gate_allows_supported_parallel_manual(monkeypatch):
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf",
                page=20,
            ),
            0.448822,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf",
                page=21,
            ),
            0.4489,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf",
                page=10,
            ),
            0.44895,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics={
            "score_floor": 0.38,
            "best_raw_distance": 0.448182,
            "raw_count": 3,
            "post_admissibility_count": 3,
            "post_scope_count": 3,
            "post_rerank_count": 3,
            "post_dedupe_count": 3,
        },
    )

    assert len(retained) == 3
    assert diagnostics["final_retained_count"] == 3
    assert diagnostics["final_confidence_passed"] is True
    assert diagnostics["top_source_support"] == 2
    assert diagnostics["top_family_support"] == 3


def test_final_confidence_gate_rejects_mixed_non_authority_candidates(monkeypatch):
    pairs = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Training/guide.pdf", page=1), 0.455559),
        (
            _FakeDoc(
                "00_Career/07_SDS_Datasheets/02_Manuals/DECKMA_OMD-24_Series_Instruction_Manual_EN_R13_251028.pdf",
                page=2,
            ),
            0.470001,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics={
            "score_floor": 0.38,
            "best_raw_distance": 0.455559,
            "raw_count": 2,
            "post_admissibility_count": 2,
            "post_scope_count": 2,
            "post_rerank_count": 2,
            "post_dedupe_count": 2,
        },
    )

    assert retained == []
    assert diagnostics["final_retained_count"] == 0
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["top_document_type"] == "training"
    assert diagnostics["top_canonical_authority_rank"] == 5


def test_maker_manuals_scope_excludes_service_literature(monkeypatch):
    _clear_scope_registry_cache()
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2013-577.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/Manual_A.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", scope="maker-manuals", k=2)

    assert len(pairs) == 1
    assert pairs[0][0].metadata["source"].endswith("Manual_A.pdf")
    assert pairs[0][0].metadata["document_type"] == "maker_manual"


def test_maker_manuals_scope_excludes_training(monkeypatch):
    _clear_scope_registry_cache()
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Training/guide.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/Manual_A.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", scope="maker-manuals", k=2)

    assert len(pairs) == 1
    assert pairs[0][0].metadata["source"].endswith("Manual_A.pdf")
    assert pairs[0][0].metadata["document_type"] == "maker_manual"


def test_maker_manuals_scope_excludes_sds_reference(monkeypatch):
    _clear_scope_registry_cache()
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/07_SDS_Datasheets/02_Manuals/DECKMA_OMD-24_Series_Instruction_Manual_EN_R13_251028.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/Manual_A.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", scope="maker-manuals", k=2)

    assert len(pairs) == 1
    assert pairs[0][0].metadata["source"].endswith("Manual_A.pdf")
    assert pairs[0][0].metadata["document_type"] == "maker_manual"


def test_maker_manuals_scope_excludes_unrelated_reference_family(monkeypatch):
    _clear_scope_registry_cache()
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/Param list.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/Manual_A.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", scope="maker-manuals", k=2)

    assert len(pairs) == 1
    assert pairs[0][0].metadata["source"].endswith("Manual_A.pdf")
    assert pairs[0][0].metadata["document_type"] == "maker_manual"


def test_scope_filter_negative_regression_unscoped_search_keeps_candidates(monkeypatch):
    _clear_scope_registry_cache()
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Training/guide.pdf"), 0.21),
        (_FakeDoc("00_Career/07_SDS_Datasheets/02_Manuals/DECKMA_OMD-24_Series_Instruction_Manual_EN_R13_251028.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", scope=None, k=2)

    assert len(pairs) == 2
    assert pairs[0][0].metadata["document_type"] == "training"
    assert pairs[1][0].metadata["document_type"] == "reference"


def test_retrieve_with_scores_prefers_higher_authority_rank(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Training/guide.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/VOL1_small.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=2)

    assert pairs[0][0].metadata["source"].endswith("VOL1_small.pdf")
    assert pairs[0][0].metadata["authority_rank"] == 3
    assert pairs[0][0].metadata["document_type"] == "maker_manual"
    assert pairs[1][0].metadata["authority_rank"] == 5
    assert pairs[1][0].metadata["document_type"] == "training"


def test_retrieve_with_scores_prefers_ocr_manual_authority_over_reference_within_same_band(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Training/guide.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/manual_OCR.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=2)

    assert pairs[0][0].metadata["source"].endswith("manual.pdf")
    assert pairs[0][0].metadata["authority_rank"] == 7
    assert pairs[0][0].metadata["canonical_authority_rank"] == 3
    assert pairs[0][0].metadata["document_type"] == "maker_manual"
    assert pairs[0][0].metadata["machine_transcribed"] is True
    assert pairs[1][0].metadata["authority_rank"] == 5
    assert pairs[1][0].metadata["document_type"] == "training"


def test_retrieve_with_scores_preserves_raw_source_for_ocr_candidate(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/manual_OCR.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 1)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 1)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=1)

    assert pairs[0][0].metadata["source"].endswith("manual.pdf")
    assert pairs[0][0].metadata["raw_source"].endswith("manual_OCR.pdf")
    assert pairs[0][0].metadata["machine_transcribed"] is True
    assert pairs[0][0].metadata["authority_family"] == "Yanmar_6EY22"


def test_retrieve_with_scores_prefers_text_native_over_ocr_for_same_document(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/manual_OCR.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/manual.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=2)

    assert len(pairs) == 1
    assert pairs[0][0].metadata["source"].endswith("manual.pdf")
    assert pairs[0][0].metadata["authority_rank"] == 3
    assert pairs[0][0].metadata["machine_transcribed"] is False


def test_retrieve_with_scores_prefers_maker_manual_over_service_letter(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2013-577.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22(A)LWS(H.F.O.／MET)_0AE22-EN0100 May.2023-10.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=2)

    assert pairs[0][0].metadata["document_type"] == "operation_manual"
    assert pairs[1][0].metadata["document_type"] == "service_letter"


def test_retrieve_with_scores_prefers_maker_manual_over_reference(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/07_SDS_Datasheets/02_Manuals/DECKMA_OMD-24_Series_Instruction_Manual_EN_R13_251028.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22(A)LWS(H.F.O.／MET)_0AE22-EN0100 May.2023-10.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=2)

    assert pairs[0][0].metadata["document_type"] == "operation_manual"
    assert pairs[1][0].metadata["document_type"] == "reference"


def test_retrieve_with_scores_prefers_supported_authority_family_within_same_type(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/OWS_RWO/Manual_A.pdf"), 0.21),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/Manual_B.pdf"), 0.22),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/Manual_C.pdf"), 0.23),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 3)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 3)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=3)

    assert pairs[0][0].metadata["authority_family"] == "Yanmar_6EY22"
    assert pairs[1][0].metadata["authority_family"] == "Yanmar_6EY22"
    assert pairs[2][0].metadata["authority_family"] == "OWS_RWO"


def test_retrieve_with_scores_prefers_better_supported_parallel_manual_within_same_family(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf", page=10), 0.211),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf", page=20), 0.212),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf", page=21), 0.213),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf", page=22), 0.214),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf", page=11), 0.245),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=5)

    assert pairs[0][0].metadata["source"].endswith("0ASCR-EN0054 Sep.2024-0.pdf")
    assert pairs[0][0].metadata["authority_family"] == "Yanmar_6EY22"
    unique_sources = []
    for doc, _distance in pairs:
        source = doc.metadata["source"]
        if source not in unique_sources:
            unique_sources.append(source)
    assert unique_sources[:2] == [
        "00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf",
        "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf",
    ]


def test_retrieve_with_scores_keeps_closer_parallel_manual_ahead_across_bands(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf", page=10), 0.219),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf", page=11), 0.239),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf", page=20), 0.251),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf", page=21), 0.252),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf", page=22), 0.253),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=5)

    assert pairs[0][0].metadata["source"].endswith("0ASCR-EN0051_20210823.pdf")
    unique_sources = []
    for doc, _distance in pairs:
        source = doc.metadata["source"]
        if source not in unique_sources:
            unique_sources.append(source)
    assert unique_sources[:2] == [
        "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR-(A)L_0ASCR-EN0051_20210823.pdf",
        "00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054 Sep.2024-0.pdf",
    ]


def test_retrieve_with_scores_document_type_does_not_override_much_closer_hit(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Training/guide.pdf"), 0.05),
        (_FakeDoc("00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22(A)LWS(H.F.O.／MET)_0AE22-EN0100 May.2023-10.pdf"), 0.24),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.30)

    pairs = query.retrieve_with_scores("some question", k=2)

    assert pairs[0][0].metadata["document_type"] == "training"
    assert pairs[1][0].metadata["document_type"] == "operation_manual"


def test_retrieve_with_scores_keeps_much_closer_hit_ahead_of_farther_authority(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (_FakeDoc("00_Career/03_Engine_Knowledge/Training/guide.pdf"), 0.05),
        (_FakeDoc("10_Company/policy.pdf", collection="sms"), 0.37),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 2)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 2)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    pairs = query.retrieve_with_scores("some question", k=2)

    assert pairs[0][0].metadata["source"].endswith("guide.pdf")
    assert pairs[0][0].metadata["authority_rank"] == 5
    assert pairs[1][0].metadata["authority_rank"] == 2


def test_answer_reports_final_confidence_gate_when_reranked_hits_are_insufficient(monkeypatch):
    monkeypatch.setattr(
        query,
        "retrieve_with_scores_and_diagnostics",
        lambda *args, **kwargs: (
            [(_FakeDoc("00_Career/03_Engine_Knowledge/Training/guide.pdf"), 0.455559)],
            {
                "score_floor": 0.38,
                "best_raw_distance": 0.455559,
                "raw_count": 1,
                "post_admissibility_count": 1,
                "post_scope_count": 1,
                "post_rerank_count": 1,
                "post_dedupe_count": 1,
                "final_retained_count": 0,
                "gate": "final_confidence_failed",
                "final_confidence_passed": False,
            },
        ),
    )

    r = query.answer("some question", scope="maker-manuals")

    assert r.status == "no_coverage"
    assert r.gate == "final_confidence_failed"
    assert r.retrieval_evidence[0]["path"].endswith("guide.pdf")
    assert r.to_json()["sources"] == []

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine import query


class _FakeDoc:
    def __init__(
        self,
        source: str,
        *,
        page: int = 1,
        collection: str = "maker-manuals",
        content: str = "chunk",
        raw_source: str | None = None,
        chunk_id: str | None = None,
    ):
        self.metadata = {"source": source, "page": page, "collection": collection}
        if raw_source is not None:
            self.metadata["raw_source"] = raw_source
        if chunk_id is not None:
            self.metadata["chunk_id"] = chunk_id
        self.page_content = content


def test_exact_technical_token_brings_lexical_only_candidate_into_merged_pool():
    vector_pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Training/generic.pdf",
                chunk_id="v1",
                content="General tightening notes.",
            ),
            0.22,
        )
    ]
    lexical_pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3_ocr.pdf",
                chunk_id="l1",
                raw_source="00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3_ocr.pdf",
                content="Table 7 M42 thread dimensions for casing.",
            ),
            {
                "lexical_score": 120,
                "lexical_exact_hits": ["m42"],
                "lexical_phrase_hits": ["m42 thread"],
                "heading_match": True,
            },
        )
    ]

    merged, diagnostics = query._merge_hybrid_candidates(vector_pairs, lexical_pairs)

    assert diagnostics["vector_raw_count"] == 1
    assert diagnostics["lexical_raw_count"] == 1
    assert diagnostics["merged_raw_count"] == 2
    assert any(doc.metadata["candidate_origin"] == "lexical" for doc, _ in merged)
    assert any(doc.metadata["lexical_exact_hits"] == ["m42"] for doc, _ in merged)


def test_generic_token_alone_cannot_dominate():
    vector_pairs = [
        (
            _FakeDoc(
                "20_Vessels/Gaschem_Europe/01_Manuals/02_Auxiliary_Engines/manual.pdf",
                chunk_id="v1",
                content="Specific instruction for governor adjustment.",
            ),
            0.20,
        )
    ]
    lexical_pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Service_Letters_MAN_Archive/sl2012-557.pdf",
                chunk_id="l1",
                content="Torque torque torque.",
            ),
            {
                "lexical_score": 1,
                "lexical_exact_hits": [],
                "lexical_phrase_hits": [],
                "heading_match": False,
            },
        )
    ]

    merged, _diagnostics = query._merge_hybrid_candidates(vector_pairs, lexical_pairs)

    assert merged[0][0].metadata["chunk_id"] == "v1"
    assert merged[0][0].metadata["candidate_origin"] == "vector"


def test_vector_and_lexical_duplicate_becomes_origin_both():
    shared = _FakeDoc(
        "20_Vessels/Gaschem_Europe/01_Manuals/02_Auxiliary_Engines/manual.pdf",
        chunk_id="same-1",
        content="M42 thread table.",
    )
    vector_pairs = [(shared, 0.24)]
    lexical_pairs = [
        (
            _FakeDoc(
                "20_Vessels/Gaschem_Europe/01_Manuals/02_Auxiliary_Engines/manual.pdf",
                chunk_id="same-1",
                content="M42 thread table.",
            ),
            {
                "lexical_score": 120,
                "lexical_exact_hits": ["m42"],
                "lexical_phrase_hits": ["m42 thread"],
                "heading_match": True,
            },
        )
    ]

    merged, _diagnostics = query._merge_hybrid_candidates(vector_pairs, lexical_pairs)

    assert len(merged) == 1
    doc, distance = merged[0]
    assert distance == 0.24
    assert doc.metadata["candidate_origin"] == "both"
    assert doc.metadata["lexical_score"] == 120
    assert doc.metadata["vector_distance"] == 0.24


def test_deterministic_merge_ordering_prefers_both_then_exact_then_vector_distance():
    vector_pairs = [
        (_FakeDoc("20_Vessels/A.pdf", chunk_id="v-only", content="pump manual"), 0.10),
        (_FakeDoc("20_Vessels/B.pdf", chunk_id="both", content="M42 table"), 0.30),
    ]
    lexical_pairs = [
        (
            _FakeDoc("20_Vessels/B.pdf", chunk_id="both", content="M42 table"),
            {"lexical_score": 50, "lexical_exact_hits": ["m42"], "lexical_phrase_hits": [], "heading_match": False},
        ),
        (
            _FakeDoc("20_Vessels/C.pdf", chunk_id="lex-exact", content="M42 dimensions"),
            {"lexical_score": 80, "lexical_exact_hits": ["m42"], "lexical_phrase_hits": [], "heading_match": False},
        ),
    ]

    merged, _diagnostics = query._merge_hybrid_candidates(vector_pairs, lexical_pairs)

    assert [doc.metadata["chunk_id"] for doc, _ in merged] == ["both", "lex-exact", "v-only"]


def test_retrieve_with_scores_and_diagnostics_exposes_provenance(monkeypatch):
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Yanmar_6EY22/manual.pdf",
                chunk_id="v1",
                content="governor adjustment",
            ),
            0.21,
        )
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())
    monkeypatch.setattr(
        query,
        "_lexical_retrieve",
        lambda question, scope=None, k=None: [
            (
                _FakeDoc(
                    "00_Career/03_Engine_Knowledge/Yanmar_6EY22/manual.pdf",
                    chunk_id="v1",
                    content="M42 thread table",
                ),
                {
                    "lexical_score": 120,
                    "lexical_exact_hits": ["m42"],
                    "lexical_phrase_hits": ["m42 thread"],
                    "heading_match": True,
                },
            )
        ],
    )

    pairs, diagnostics = query.retrieve_with_scores_and_diagnostics("What is M42 thread?", scope="maker-manuals", k=5)

    assert diagnostics["vector_raw_count"] == 1
    assert diagnostics["lexical_raw_count"] == 1
    assert diagnostics["merged_raw_count"] == 1
    assert diagnostics["candidate_origins"] == {"both": 1}
    assert pairs[0][0].metadata["candidate_origin"] == "both"
    assert pairs[0][0].metadata["lexical_exact_hits"] == ["m42"]

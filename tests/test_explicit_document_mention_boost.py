"""ORCH_101 / final hardening: explicit document-mention ranking boost."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine import query


class _FakeDoc:
    def __init__(self, source: str, page: int = 1, collection: str = "me-c"):
        self.metadata = {"source": source, "page": page, "collection": collection}
        self.page_content = "chunk"


def test_extract_explicit_document_mentions_manual_id_and_pdf():
    mentions = query.extract_explicit_document_mentions(
        "In M 1.3, what is the M42 tightening torque from Manual Foo.pdf?"
    )
    assert any(m.lower() == "m 1.3" for m in mentions)
    assert any(m.lower() == "manual foo.pdf" for m in mentions)


def test_extract_pdf_does_not_absorb_leading_prose():
    assert query.extract_explicit_document_mentions("see Manual.pdf please") == (
        "Manual.pdf",
    )
    assert query.extract_explicit_document_mentions("check M 1.3.pdf") == (
        "M 1.3",
        "M 1.3.pdf",
    )
    assert query.extract_explicit_document_mentions(
        "refer to 'Main Engine Manual.pdf'"
    ) == ("Main Engine Manual.pdf",)
    assert query.extract_explicit_document_mentions("according to VOL II.PDF") == (
        "VOL II.PDF",
    )


def test_extract_pdf_handles_punctuation_and_case():
    assert query.extract_explicit_document_mentions("open (Foo.PDF).") == ("Foo.PDF",)
    assert query.extract_explicit_document_mentions('see "Bar.pdf", then stop') == (
        "Bar.pdf",
    )


def test_source_matches_explicit_mention_for_m13_basename():
    src = "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf"
    assert query.source_matches_explicit_mention(src, "M 1.3")
    assert query.source_matches_explicit_mention(src, "M 1.3.pdf")
    assert not query.source_matches_explicit_mention(
        "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/G50ME-C-LGIP-HPSCR.pdf",
        "M 1.3",
    )


def test_source_matches_rejects_short_stem_and_near_misses():
    assert not query.source_matches_explicit_mention("dir/M.pdf", "M 1.3")
    assert not query.source_matches_explicit_mention("dir/XM 1.3.pdf", "M 1.3")
    assert not query.source_matches_explicit_mention("dir/M 1.30.pdf", "M 1.3")
    assert not query.source_matches_explicit_mention(
        "dir/Another Manual.pdf", "Manual.pdf"
    )
    assert query.source_matches_explicit_mention("dir/Manual.pdf", "Manual.pdf")
    assert query.source_matches_explicit_mention(
        "dir/Main Engine Manual.pdf", "Main Engine Manual.pdf"
    )


def test_retrieve_prefers_explicitly_named_manual_over_closer_generic_torque_table(
    monkeypatch,
):
    """Named-manual class: question names M 1.3; prefer that source even if
    another MAN torque table embeds slightly closer."""
    named = _FakeDoc(
        "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf",
        page=39,
    )
    generic = _FakeDoc(
        "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/G50ME-C-LGIP-HPSCR.pdf",
        page=90,
    )
    db = MagicMock()
    # Generic slightly closer in embedding space (matches live baseline).
    db.similarity_search_with_score.return_value = [
        (generic, 0.43),
        (named, 0.46),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())

    pairs, diag = query.retrieve_with_scores_and_diagnostics(
        "In M 1.3, what is the M42 tightening torque?",
        scope="me-c",
        k=2,
    )
    assert pairs
    top_src = pairs[0][0].metadata["source"]
    assert "M 1.3.pdf" in top_src
    assert diag.get("explicit_document_mentions")


def test_retrieve_without_explicit_mention_keeps_closer_hit(monkeypatch):
    named = _FakeDoc(
        "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf",
        page=39,
    )
    generic = _FakeDoc(
        "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/G50ME-C-LGIP-HPSCR.pdf",
        page=90,
    )
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (generic, 0.43),
        (named, 0.46),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())

    pairs = query.retrieve_with_scores(
        "What is the M42 tightening torque?",
        scope="me-c",
        k=2,
    )
    assert "G50ME-C-LGIP-HPSCR.pdf" in pairs[0][0].metadata["source"]


def test_retrieve_pdf_filename_mention_boosts_exact_basename(monkeypatch):
    target = _FakeDoc("library/Manual.pdf", page=2)
    other = _FakeDoc("library/Another Manual.pdf", page=2)
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (other, 0.40),
        (target, 0.45),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())

    pairs, diag = query.retrieve_with_scores_and_diagnostics(
        "see Manual.pdf please",
        scope="me-c",
        k=2,
    )
    assert pairs[0][0].metadata["source"].endswith("Manual.pdf")
    assert not pairs[0][0].metadata["source"].endswith("Another Manual.pdf")
    assert diag.get("explicit_document_mentions") == ["Manual.pdf"]


def test_two_explicit_filenames_boost_both_deterministically(monkeypatch):
    a = _FakeDoc("library/Alpha.pdf", page=1)
    b = _FakeDoc("library/Beta.pdf", page=1)
    c = _FakeDoc("library/Gamma.pdf", page=1)
    db = MagicMock()
    # Gamma closest, then Beta, then Alpha — mentions Alpha.pdf and Beta.pdf.
    db.similarity_search_with_score.return_value = [
        (c, 0.30),
        (b, 0.40),
        (a, 0.50),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())

    pairs, diag = query.retrieve_with_scores_and_diagnostics(
        "compare Alpha.pdf with Beta.pdf",
        scope="me-c",
        k=3,
    )
    sources = [p[0].metadata["source"] for p in pairs]
    # Both mentioned files outrank Gamma; among boosted hits, closer distance wins.
    assert sources[:2] == ["library/Beta.pdf", "library/Alpha.pdf"]
    assert sources[2] == "library/Gamma.pdf"
    mentions = diag.get("explicit_document_mentions") or []
    assert "Alpha.pdf" in mentions and "Beta.pdf" in mentions


def test_similar_candidate_names_do_not_cross_boost(monkeypatch):
    exact = _FakeDoc("library/M 1.3.pdf", page=1)
    near = _FakeDoc("library/M 1.30.pdf", page=1)
    short = _FakeDoc("library/M.pdf", page=1)
    db = MagicMock()
    db.similarity_search_with_score.return_value = [
        (near, 0.31),
        (short, 0.32),
        (exact, 0.55),
    ]
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 5)
    monkeypatch.setattr(query, "_run_with_timeout", lambda fn, timeout=None: fn())

    pairs = query.retrieve_with_scores(
        "In M 1.3, what is the torque?",
        scope="me-c",
        k=3,
    )
    basenames = [p[0].metadata["source"].rsplit("/", 1)[-1] for p in pairs]
    assert basenames[0] == "M 1.3.pdf"
    assert "M 1.30.pdf" in basenames[1:] or "M.pdf" in basenames[1:]

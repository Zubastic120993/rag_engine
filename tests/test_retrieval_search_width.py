"""F-17: retrieve_with_scores must query wide but truncate to k before
returning — the wide candidate set must never reach callers (sources, LLM
context, conservative-success check)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rag_engine.query as query


class _FakeDoc:
    def __init__(self, i: int):
        self.metadata = {"source": f"doc-{i}.pdf", "page": 0, "collection": "other"}
        self.page_content = f"chunk {i}"


@pytest.fixture()
def fake_db(monkeypatch):
    calls = []

    def similarity_search_with_score(q, k, filter=None):  # noqa: A002
        calls.append({"k": k, "filter": filter})
        return [(_FakeDoc(i), float(i) / 1000.0) for i in range(k)]

    db = MagicMock()
    db.similarity_search_with_score.side_effect = similarity_search_with_score
    monkeypatch.setattr(query, "_get_db", lambda: db)
    monkeypatch.setattr(query, "default_k", lambda: 5)
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 400)
    return calls


def test_retrieve_with_scores_queries_wide_but_returns_k(fake_db):
    pairs = query.retrieve_with_scores("some question")
    assert len(pairs) == 5  # default_k(), never the search width
    assert fake_db[0]["k"] == 400  # the actual Chroma call went wide


def test_retrieve_with_scores_truncates_explicit_k_too(fake_db):
    pairs = query.retrieve_with_scores("some question", k=3)
    assert len(pairs) == 3
    assert fake_db[0]["k"] == 400  # still widened, even though caller asked for 3


def test_retrieve_with_scores_search_width_narrower_than_k_still_returns_k(fake_db, monkeypatch):
    # If a caller ever asks for more than the configured search width, the
    # actual Chroma query must widen to at least k, not silently cap below it.
    monkeypatch.setattr(query, "retrieval_search_width", lambda: 10)
    pairs = query.retrieve_with_scores("some question", k=50)
    assert len(pairs) == 50
    assert fake_db[0]["k"] == 50

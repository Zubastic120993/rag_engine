from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rag_engine.query import answer


class _FakeDoc:
    def __init__(self, source: str, page: int, collection: str, content: str = "Relevant text."):
        self.metadata = {"source": source, "page": page, "collection": collection}
        self.page_content = content


def _pairs(*rows: tuple[str, int, str, float, str]):
    built = []
    for source, page, collection, distance, content in rows:
        built.append((_FakeDoc(source, page, collection, content), distance))
    return built


def _diag(raw_count: int | None = None) -> dict:
    return {
        "score_floor": 0.38,
        "best_raw_distance": 0.2,
        "raw_count": raw_count or 1,
        "post_admissibility_count": raw_count or 1,
        "post_scope_count": raw_count or 1,
        "post_rerank_count": raw_count or 1,
        "post_dedupe_count": raw_count or 1,
        "final_retained_count": raw_count or 1,
        "final_confidence_passed": True,
        "gate": None,
    }


def _confirmation(scope: str, family: str) -> dict:
    return {
        "confirmed_engine_family": family,
        "confirmed_manual_family": family,
        "confirmed_scope": scope,
        "invalidated_alternatives": ["other-family"],
        "provenance": "CURRENT_TURN_EXPLICIT_CONFIRMATION",
    }


def test_explicit_man_query_routes_directly_without_clarification(monkeypatch):
    me_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf",
            42,
            "me-c",
            0.12,
            "M42 tightening torque is 900 Nm.",
        )
    )
    calls: list[str | None] = []

    def fake_retrieve(question, scope=None, k=None):
        calls.append(scope)
        return me_pairs, _diag()

    monkeypatch.setattr("rag_engine.query.retrieve_with_scores_and_diagnostics", fake_retrieve)
    monkeypatch.setattr("rag_engine.query._invoke_llm", lambda *args, **kwargs: "M42 tightening torque is 900 Nm.")

    result = answer("In M 1.3, what is the M42 tightening torque?")

    assert result.status == "ok"
    assert result.resolved_scope == "me-c"
    assert calls == ["me-c"]
    assert result.retrieval_diagnostics["context_router"]["clarification_issued"] is False
    assert result.retrieval_diagnostics["context_router"]["discovery_ran"] is False


def test_vague_multi_family_query_clarifies_before_answer(monkeypatch):
    broad_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf",
            42,
            "me-c",
            0.12,
            "M42 tightening torque is 900 Nm.",
        ),
        (
            "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22.pdf",
            73,
            "maker-manuals",
            0.14,
            "Specified bolt tightening torque is 320 Nm.",
        ),
    )

    monkeypatch.setattr(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        lambda question, scope=None, k=None: (broad_pairs, _diag(raw_count=2)),
    )
    llm = MagicMock()
    monkeypatch.setattr("rag_engine.query._invoke_llm", llm)

    result = answer("What is the torque?")

    assert result.status == "clarification_required"
    assert result.answer is not None
    assert "more than one equipment family" in result.answer
    assert result.sources == []
    assert result.resolved_scope is None
    assert llm.call_count == 0
    assert result.retrieval_diagnostics["context_router"]["clarification_issued"] is True
    assert result.retrieval_diagnostics["context_router"]["discovery_ran"] is True
    assert len(result.retrieval_diagnostics["context_router"]["plausible_families"]) == 2
    assert result.retrieval_diagnostics["context_router"]["preconfirmation_candidate_reuse"] is False


def test_explicit_yanmar_query_routes_directly_without_clarification(monkeypatch):
    yanmar_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22.pdf",
            73,
            "maker-manuals",
            0.11,
            "Specified bolt tightening torque is 320 Nm.",
        )
    )
    calls: list[str | None] = []

    def fake_retrieve(question, scope=None, k=None):
        calls.append(scope)
        return yanmar_pairs, _diag()

    monkeypatch.setattr("rag_engine.query.retrieve_with_scores_and_diagnostics", fake_retrieve)
    monkeypatch.setattr("rag_engine.query._invoke_llm", lambda *args, **kwargs: "Specified bolt tightening torque is 320 Nm.")

    result = answer("For Yanmar 6EY22, what is the tightening torque?")

    assert result.status == "ok"
    assert result.resolved_scope == "maker-manuals"
    assert calls == ["maker-manuals"]
    assert result.retrieval_diagnostics["context_router"]["clarification_issued"] is False


def test_confirmation_routes_man_with_fresh_constrained_retrieval(monkeypatch):
    broad_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf",
            42,
            "me-c",
            0.12,
            "M42 tightening torque is 900 Nm.",
        ),
        (
            "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22.pdf",
            73,
            "maker-manuals",
            0.14,
            "Specified bolt tightening torque is 320 Nm.",
        ),
    )
    me_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf",
            42,
            "me-c",
            0.12,
            "M42 tightening torque is 900 Nm.",
        )
    )
    calls: list[str | None] = []
    results = iter([(broad_pairs, _diag(raw_count=2)), (me_pairs, _diag())])

    def fake_retrieve(question, scope=None, k=None):
        calls.append(scope)
        return next(results)

    monkeypatch.setattr("rag_engine.query.retrieve_with_scores_and_diagnostics", fake_retrieve)
    monkeypatch.setattr("rag_engine.query._invoke_llm", lambda *args, **kwargs: "M42 tightening torque is 900 Nm.")

    first = answer("What is the torque?")
    second = answer(
        "What is the torque?",
        confirmation_object=_confirmation("me-c", "MAN_G50ME-C_LGIP"),
    )

    assert first.status == "clarification_required"
    assert second.status == "ok"
    assert second.resolved_scope == "me-c"
    assert calls == [None, "me-c"]
    assert second.sources[0]["collection"] == "me-c"
    assert second.retrieval_diagnostics["context_router"]["confirmation_applied"] is True
    assert second.retrieval_diagnostics["context_router"]["fresh_retrieval_ran"] is True
    assert second.retrieval_diagnostics["context_router"]["preconfirmation_candidate_reuse"] is False


def test_confirmation_routes_yanmar_with_fresh_constrained_retrieval(monkeypatch):
    broad_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf",
            42,
            "me-c",
            0.12,
            "M42 tightening torque is 900 Nm.",
        ),
        (
            "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22.pdf",
            73,
            "maker-manuals",
            0.14,
            "Specified bolt tightening torque is 320 Nm.",
        ),
    )
    yanmar_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22.pdf",
            73,
            "maker-manuals",
            0.14,
            "Specified bolt tightening torque is 320 Nm.",
        )
    )
    calls: list[str | None] = []
    results = iter([(broad_pairs, _diag(raw_count=2)), (yanmar_pairs, _diag())])

    def fake_retrieve(question, scope=None, k=None):
        calls.append(scope)
        return next(results)

    monkeypatch.setattr("rag_engine.query.retrieve_with_scores_and_diagnostics", fake_retrieve)
    monkeypatch.setattr("rag_engine.query._invoke_llm", lambda *args, **kwargs: "Specified bolt tightening torque is 320 Nm.")

    first = answer("What is the torque?")
    second = answer(
        "What is the torque?",
        confirmation_object=_confirmation("maker-manuals", "Yanmar_6EY22"),
    )

    assert first.status == "clarification_required"
    assert second.status == "ok"
    assert second.resolved_scope == "maker-manuals"
    assert calls == [None, "maker-manuals"]
    assert second.sources[0]["collection"] == "maker-manuals"
    assert second.retrieval_diagnostics["context_router"]["confirmation_applied"] is True
    assert second.retrieval_diagnostics["context_router"]["fresh_retrieval_ran"] is True


def test_stale_prior_context_does_not_silently_route_ambiguous_query(monkeypatch):
    broad_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf",
            42,
            "me-c",
            0.12,
            "M42 tightening torque is 900 Nm.",
        ),
        (
            "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22.pdf",
            73,
            "maker-manuals",
            0.14,
            "Specified bolt tightening torque is 320 Nm.",
        ),
    )
    stale_context = {
        "current_session_id": "s1",
        "current_turn_ref": "t1",
        "context_state": "STALE_CONTEXT",
        "fields": [
            {
                "field_name": "active_engine_family",
                "value": "MAN_G50ME-C_LGIP",
                "provenance": "SESSION_VERIFIED",
                "stale": True,
            }
        ],
    }
    monkeypatch.setattr(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        lambda question, scope=None, k=None: (broad_pairs, _diag(raw_count=2)),
    )
    llm = MagicMock()
    monkeypatch.setattr("rag_engine.query._invoke_llm", llm)

    result = answer("What is the torque?", context_state=stale_context)

    assert result.status == "clarification_required"
    assert result.resolved_scope is None
    assert llm.call_count == 0
    assert result.retrieval_diagnostics["context_router"]["discovery_ran"] is True


def test_conflicting_context_requires_clarification_without_silent_route(monkeypatch):
    conflicting_context = {
        "current_session_id": "s1",
        "current_turn_ref": "t1",
        "context_state": "CONFLICTED_CONTEXT",
        "fields": [
            {
                "field_name": "active_engine_family",
                "value": "MAN_G50ME-C_LGIP",
                "provenance": "SESSION_VERIFIED",
                "conflict": True,
            },
            {
                "field_name": "active_engine_family",
                "value": "Yanmar_6EY22",
                "provenance": "SESSION_VERIFIED",
                "conflict": True,
            },
        ],
    }
    retrieve = MagicMock()
    llm = MagicMock()
    monkeypatch.setattr("rag_engine.query.retrieve_with_scores_and_diagnostics", retrieve)
    monkeypatch.setattr("rag_engine.query._invoke_llm", llm)

    result = answer("What is the torque?", context_state=conflicting_context)

    assert result.status == "clarification_required"
    assert result.resolved_scope is None
    assert retrieve.call_count == 0
    assert llm.call_count == 0
    assert result.retrieval_diagnostics["context_router"]["clarification_issued"] is True


def test_explicit_current_turn_override_of_stale_context_routes_directly(monkeypatch):
    yanmar_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22.pdf",
            73,
            "maker-manuals",
            0.14,
            "Specified bolt tightening torque is 320 Nm.",
        )
    )
    stale_context = {
        "current_session_id": "s1",
        "current_turn_ref": "t1",
        "context_state": "STALE_CONTEXT",
        "fields": [
            {
                "field_name": "active_engine_family",
                "value": "MAN_G50ME-C_LGIP",
                "provenance": "SESSION_VERIFIED",
                "stale": True,
            }
        ],
    }
    calls: list[str | None] = []

    def fake_retrieve(question, scope=None, k=None):
        calls.append(scope)
        return yanmar_pairs, _diag()

    monkeypatch.setattr("rag_engine.query.retrieve_with_scores_and_diagnostics", fake_retrieve)
    monkeypatch.setattr("rag_engine.query._invoke_llm", lambda *args, **kwargs: "Specified bolt tightening torque is 320 Nm.")

    result = answer(
        "For Yanmar 6EY22, what is the tightening torque?",
        context_state=stale_context,
    )

    assert result.status == "ok"
    assert result.resolved_scope == "maker-manuals"
    assert calls == ["maker-manuals"]


def test_confirmation_with_no_acceptable_constrained_evidence_fails_closed(monkeypatch):
    calls: list[str | None] = []

    def fake_retrieve(question, scope=None, k=None):
        calls.append(scope)
        return [], {**_diag(raw_count=0), "raw_count": 0, "final_retained_count": 0}

    monkeypatch.setattr("rag_engine.query.retrieve_with_scores_and_diagnostics", fake_retrieve)
    llm = MagicMock()
    monkeypatch.setattr("rag_engine.query._invoke_llm", llm)

    result = answer(
        "What is the torque?",
        confirmation_object=_confirmation("me-c", "MAN_G50ME-C_LGIP"),
    )

    assert result.status == "no_coverage"
    assert result.gate == "authority_verification_failed"
    assert result.resolved_scope == "me-c"
    assert calls == ["me-c"]
    assert llm.call_count == 0
    assert result.retrieval_diagnostics["context_router"]["fresh_retrieval_ran"] is True
    assert result.retrieval_diagnostics["context_router"]["preconfirmation_candidate_reuse"] is False


def test_existing_direct_scope_behavior_is_preserved(monkeypatch):
    yanmar_pairs = _pairs(
        (
            "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_6EY22.pdf",
            73,
            "maker-manuals",
            0.14,
            "Specified bolt tightening torque is 320 Nm.",
        )
    )
    calls: list[str | None] = []

    def fake_retrieve(question, scope=None, k=None):
        calls.append(scope)
        return yanmar_pairs, _diag()

    monkeypatch.setattr("rag_engine.query.retrieve_with_scores_and_diagnostics", fake_retrieve)
    monkeypatch.setattr("rag_engine.query._invoke_llm", lambda *args, **kwargs: "Specified bolt tightening torque is 320 Nm.")

    result = answer("What is the tightening torque?", scope="maker-manuals")

    assert result.status == "ok"
    assert result.resolved_scope == "maker-manuals"
    assert calls == ["maker-manuals"]
    assert result.retrieval_diagnostics["context_router"]["route"]["routing_mode"] == "EXTERNAL_SCOPE"

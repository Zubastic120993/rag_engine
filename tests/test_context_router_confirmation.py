from __future__ import annotations

import json
from pathlib import Path

from rag_engine.context_router import parse_question_evidence, route_with_discovery

FIXTURE_PATH = Path(
    "/Users/vladymyrzub/Projects/ai-engineering-orchestrator/eval/context_router_confirmation_fixtures_v1.json"
)


def _load_fixtures() -> list[dict]:
    payload = json.loads(FIXTURE_PATH.read_text())
    assert payload["totals"]["fixtures"] == 8
    return payload["fixtures"]


def _route_fixture(fixture: dict) -> dict:
    return route_with_discovery(
        question_evidence=parse_question_evidence(fixture["question_text"]),
        context_state={
            "current_session_id": "confirmation-session",
            "current_turn_ref": fixture["fixture_id"],
            "context_state": fixture.get("layer_a_context_state", "NO_CONTEXT"),
            "fields": fixture.get("stale_context_fields", []),
        },
        discovery_state=fixture.get("discovery_result"),
        prior_process_state=fixture.get("prior_process_state"),
        confirmation_object=fixture.get("confirmation_object"),
        constrained_retrieval_result=fixture.get("constrained_retrieval_result"),
    )


def test_layer_b_confirmation_fixtures_match_expected_decisions():
    fixtures = _load_fixtures()

    for fixture in fixtures:
        decision = _route_fixture(fixture)
        assert decision["decision"] == fixture["expected_decision"], fixture["fixture_id"]
        assert decision["process_state"] == fixture["expected_process_state"], fixture["fixture_id"]
        assert decision["technical_answer_allowed"] == fixture["expected_technical_answer_allowed"], fixture[
            "fixture_id"
        ]
        assert decision["reason_code"] == fixture["expected_reason_code"], fixture["fixture_id"]
        assert decision["selected_scope"] == fixture.get("expected_scope"), fixture["fixture_id"]
        assert decision["clarification_required"] == fixture.get(
            "expected_clarification_required", False
        ), fixture["fixture_id"]
        assert decision["constrained_retrieval_required"] == fixture.get(
            "expected_constrained_retrieval_required", False
        ), fixture["fixture_id"]


def test_layer_b_hard_safety_gates_match_spec_014():
    fixtures = _load_fixtures()
    decisions = {_fixture["fixture_id"]: _route_fixture(_fixture) for _fixture in fixtures}

    ambiguous_ids = {"CONF-001", "CONF-002", "CONF-005"}
    confirmation_ids = {"CONF-006", "CONF-007"}

    assert all(decisions[fixture_id]["decision"] == "CLARIFY_MULTI_FAMILY" for fixture_id in ambiguous_ids)
    assert all(decisions[fixture_id]["technical_answer_allowed"] is False for fixture_id in ambiguous_ids)
    assert all(decisions[fixture_id]["clarification_required"] is True for fixture_id in ambiguous_ids)

    assert all(decisions[fixture_id]["decision"] == "CONFIRMED_CONSTRAINED_RETRIEVAL" for fixture_id in confirmation_ids)
    assert all(decisions[fixture_id]["technical_answer_allowed"] is False for fixture_id in confirmation_ids)
    assert all(decisions[fixture_id]["constrained_retrieval_required"] is True for fixture_id in confirmation_ids)
    assert all(decisions[fixture_id]["explicit_confirmation"] is True for fixture_id in confirmation_ids)
    assert all(decisions[fixture_id]["reuse_preconfirmation_candidates"] is False for fixture_id in confirmation_ids)

    assert decisions["CONF-008"]["decision"] == "FAIL_CLOSED_AFTER_CONFIRMATION"
    assert decisions["CONF-008"]["technical_answer_allowed"] is False
    assert decisions["CONF-008"]["reuse_preconfirmation_candidates"] is False

    assert decisions["CONF-003"]["decision"] == "ROUTE"
    assert decisions["CONF-003"]["clarification_required"] is False
    assert decisions["CONF-004"]["decision"] == "ROUTE"
    assert decisions["CONF-004"]["clarification_required"] is False

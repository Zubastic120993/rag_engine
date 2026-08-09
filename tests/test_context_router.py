from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from rag_engine.context_router import parse_question_evidence, route_context

FIXTURE_PATH = Path(
    "/Users/vladymyrzub/Projects/ai-engineering-orchestrator/eval/context_router_fixtures_v1.json"
)


def _load_fixtures() -> list[dict]:
    payload = json.loads(FIXTURE_PATH.read_text())
    assert payload["totals"]["fixtures"] == 240
    return payload["fixtures"]


def _route_fixture(fixture: dict) -> dict:
    return route_context(
        question_evidence=parse_question_evidence(fixture["question_text"]),
        context_state=fixture["context_input"],
    )


def test_layer_a_fixture_pack_matches_expected_decisions():
    fixtures = _load_fixtures()

    for fixture in fixtures:
        decision = _route_fixture(fixture)
        assert decision["decision"] == fixture["expected_decision"], fixture["fixture_id"]
        assert decision["selected_scope"] == fixture["expected_scope"], fixture["fixture_id"]
        assert decision["routing_mode"] == fixture["expected_routing_mode"], fixture["fixture_id"]
        assert decision["explicit_override"] == fixture["expected_override_status"], fixture["fixture_id"]
        assert decision["reason_code"] == fixture["expected_reason_code"], fixture["fixture_id"]
        assert decision["invalidated_fields"] == fixture["expected_invalidated_fields"], fixture["fixture_id"]
        assert decision["fields_used"] == fixture["expected_fields_used"], fixture["fixture_id"]
        assert decision["clarification_prompt_class"] == fixture["expected_clarification_prompt_class"], fixture[
            "fixture_id"
        ]
        assert bool(decision["stale_fields"]) == fixture["expected_stale_status"], fixture["fixture_id"]
        assert bool(decision["conflict_fields"]) == fixture["expected_conflict_status"], fixture["fixture_id"]


def test_layer_a_fixture_metrics_match_spec_014():
    fixtures = _load_fixtures()
    decisions = [_route_fixture(fixture) for fixture in fixtures]

    wrong_scope = sum(
        1
        for fixture, decision in zip(fixtures, decisions, strict=True)
        if decision["selected_scope"] is not None and decision["selected_scope"] != fixture["expected_scope"]
    )
    explicit_override = sum(1 for decision in decisions if decision["explicit_override"])
    unresolved_conflict_abstention = sum(
        1
        for fixture, decision in zip(fixtures, decisions, strict=True)
        if fixture["variant"] == "D.CONFLICTING_CONTEXT" and decision["decision"] == "ABSTAIN_CLARIFY"
    )
    stale_silent_route_error = sum(
        1
        for fixture, decision in zip(fixtures, decisions, strict=True)
        if fixture["variant"] == "C.STALE_WRONG_CONTEXT"
        and fixture["expected_decision"] != "ROUTE"
        and decision["decision"] == "ROUTE"
    )
    standalone_correct = sum(
        1
        for fixture, decision in zip(fixtures, decisions, strict=True)
        if fixture["variant"] == "A.STANDALONE"
        and fixture["expected_decision"] == "ROUTE"
        and decision["selected_scope"] == fixture["expected_scope"]
    )
    correct_context = sum(
        1
        for fixture, decision in zip(fixtures, decisions, strict=True)
        if fixture["variant"] == "B.CORRECT_CONTEXT" and decision["selected_scope"] == fixture["expected_scope"]
    )

    assert len(fixtures) == 240
    assert wrong_scope == 0
    assert explicit_override == 57
    assert unresolved_conflict_abstention == 48
    assert stale_silent_route_error == 0
    assert standalone_correct == 9
    assert correct_context == 48

    routing_modes = Counter(decision["routing_mode"] for decision in decisions)
    assert routing_modes == Counter(
        {
            "CURRENT_TURN_OVERRIDE": 57,
            "CONTEXT_VERIFIED": 48,
            "ABSTAIN_CONFLICT": 48,
            "ABSTAIN_NO_CONTEXT": 39,
            "ABSTAIN_STALE_CONTEXT": 39,
            "STANDALONE_EXPLICIT": 9,
        }
    )

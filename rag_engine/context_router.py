from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

SUPPORTED_SCOPES = {"me-c", "maker-manuals"}
PROVENANCE_PRECEDENCE = {
    "CURRENT_TURN_EXPLICIT": 5,
    "CURRENT_TURN_EXPLICIT_CONFIRMATION": 5,
    "PRIOR_TURN_EXPLICIT": 4,
    "SESSION_VERIFIED": 3,
    "PROFILE_VERIFIED": 2,
    "CONVERSATION_INFERRED": 1,
}

LAYER_A_ROUTING_MODES = {
    "STANDALONE_EXPLICIT",
    "CONTEXT_VERIFIED",
    "CURRENT_TURN_OVERRIDE",
    "ABSTAIN_NO_CONTEXT",
    "ABSTAIN_STALE_CONTEXT",
    "ABSTAIN_CONFLICT",
    "ABSTAIN_INFERRED_ONLY",
}

QUESTION_SCOPE_RULES: tuple[tuple[str, str], ...] = (
    (r"\bm\s*1\.3\b", "me-c"),
    (r"\bg50me-c\b", "me-c"),
    (r"\bme-c\b", "me-c"),
    (r"\bmet18src\b", "maker-manuals"),
    (r"\byanmar\b", "maker-manuals"),
    (r"\b6ey22\b", "maker-manuals"),
    (r"\by22scr\b", "maker-manuals"),
    (r"\bsootfiredetect\b", "maker-manuals"),
    (r"\bdamperallclose\b", "maker-manuals"),
    (r'"catalyst no install"', "maker-manuals"),
    (r"\brpmsensorfail\b", "maker-manuals"),
    (r"\btank1temph\b", "maker-manuals"),
    (r"\btank1templ\b", "maker-manuals"),
    (r"\btank1levell\b", "maker-manuals"),
    (r"\burea tank 1\b", "maker-manuals"),
)

CONTEXT_VALUE_SCOPE_RULES: tuple[tuple[str, str], ...] = (
    ("MAN G50ME-C", "me-c"),
    ("M 1.3", "me-c"),
    ("main engine", "me-c"),
    ("Yanmar", "maker-manuals"),
    ("MET18SRC", "maker-manuals"),
    ("SCR", "maker-manuals"),
    ("6EY22", "maker-manuals"),
)

ROUTING_FIELD_NAMES = {
    "active_manual_family",
    "active_engine_family",
    "active_scope",
    "active_equipment",
}


@dataclass(frozen=True)
class ParsedQuestionEvidence:
    question_text: str
    explicit_scope: str | None
    explicit_anchor_type: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_text": self.question_text,
            "explicit_scope": self.explicit_scope,
            "explicit_anchor_type": self.explicit_anchor_type,
        }


def parse_question_evidence(question_text: str) -> dict[str, Any]:
    normalized = question_text.strip()
    lowered = normalized.lower()
    for pattern, scope in QUESTION_SCOPE_RULES:
        if re.search(pattern, lowered):
            return ParsedQuestionEvidence(
                question_text=normalized,
                explicit_scope=scope,
                explicit_anchor_type="EXPLICIT_EQUIPMENT_FAMILY",
            ).to_dict()
    return ParsedQuestionEvidence(
        question_text=normalized,
        explicit_scope=None,
        explicit_anchor_type=None,
    ).to_dict()


def route_context(
    question_evidence: dict[str, Any],
    context_state: dict[str, Any],
    clarification_suppressed: bool = False,
) -> dict[str, Any]:
    fields = [dict(field) for field in context_state.get("fields", [])]
    context_label = context_state.get("context_state", "NO_CONTEXT")
    explicit_scope = question_evidence.get("explicit_scope")

    current_turn_fields = [field for field in fields if _is_current_turn_explicit(field)]
    stale_fields = [field for field in fields if field.get("stale")]
    conflict_fields = [field for field in fields if field.get("conflict")]
    active_fields = [field for field in fields if not field.get("stale")]

    if current_turn_fields:
        current_scope = _resolve_unique_scope(current_turn_fields)
        invalidated = _invalidated_field_names(fields, keep_scope=current_scope, stale_only=True)
        return _decision(
            decision="ROUTE",
            selected_scope=current_scope,
            routing_mode="CURRENT_TURN_OVERRIDE",
            fields_used=[field["field_name"] for field in current_turn_fields],
            provenance_used=_unique(field.get("provenance") for field in current_turn_fields),
            explicit_override=True,
            invalidated_fields=invalidated,
            stale_fields=[field["field_name"] for field in stale_fields],
            conflict_fields=[field["field_name"] for field in conflict_fields if field.get("conflict")],
            reason_code="OVERRIDE_APPLIED",
            clarification_prompt_class=None,
        )

    if context_label == "CONFLICTED_CONTEXT" or _has_equal_authority_conflict(active_fields):
        return _decision(
            decision="ABSTAIN_CLARIFY",
            selected_scope=None,
            routing_mode="ABSTAIN_CONFLICT",
            fields_used=[],
            provenance_used=[],
            explicit_override=False,
            invalidated_fields=[],
            stale_fields=[],
            conflict_fields=[field["field_name"] for field in conflict_fields or active_fields if field.get("conflict")],
            reason_code="CONFLICT",
            clarification_prompt_class=_clarification_prompt(clarification_suppressed),
        )

    if context_label == "STALE_CONTEXT" or stale_fields:
        if explicit_scope in SUPPORTED_SCOPES:
            return _decision(
                decision="ROUTE",
                selected_scope=explicit_scope,
                routing_mode="CURRENT_TURN_OVERRIDE",
                fields_used=["question_text"],
                provenance_used=["CURRENT_TURN_EXPLICIT"],
                explicit_override=True,
                invalidated_fields=[field["field_name"] for field in stale_fields if field["field_name"] in ROUTING_FIELD_NAMES],
                stale_fields=[field["field_name"] for field in stale_fields],
                conflict_fields=[],
                reason_code="OVERRIDE_APPLIED",
                clarification_prompt_class=None,
            )
        return _decision(
            decision=_abstain_decision(clarification_suppressed),
            selected_scope=None,
            routing_mode="ABSTAIN_STALE_CONTEXT",
            fields_used=[],
            provenance_used=[],
            explicit_override=False,
            invalidated_fields=[field["field_name"] for field in stale_fields if field["field_name"] in ROUTING_FIELD_NAMES],
            stale_fields=[field["field_name"] for field in stale_fields],
            conflict_fields=[],
            reason_code="STALE_CONTEXT",
            clarification_prompt_class=_clarification_prompt(clarification_suppressed),
        )

    if context_label == "INFERRED_CONTEXT":
        return _decision(
            decision=_abstain_decision(clarification_suppressed),
            selected_scope=None,
            routing_mode="ABSTAIN_INFERRED_ONLY",
            fields_used=[],
            provenance_used=[],
            explicit_override=False,
            invalidated_fields=[],
            stale_fields=[],
            conflict_fields=[],
            reason_code="INSUFFICIENT_PROVENANCE",
            clarification_prompt_class=_clarification_prompt(clarification_suppressed),
        )

    active_routing_fields = [field for field in active_fields if field.get("field_name") in ROUTING_FIELD_NAMES]
    resolved_scope = _resolve_unique_scope(active_routing_fields)
    if resolved_scope in SUPPORTED_SCOPES:
        if explicit_scope in SUPPORTED_SCOPES and explicit_scope != resolved_scope:
            return _decision(
                decision="ROUTE",
                selected_scope=explicit_scope,
                routing_mode="CURRENT_TURN_OVERRIDE",
                fields_used=["question_text"],
                provenance_used=["CURRENT_TURN_EXPLICIT"],
                explicit_override=True,
                invalidated_fields=[
                    field["field_name"]
                    for field in active_routing_fields
                    if _field_scope(field) in SUPPORTED_SCOPES and _field_scope(field) != explicit_scope
                ],
                stale_fields=[
                    field["field_name"]
                    for field in active_routing_fields
                    if _field_scope(field) in SUPPORTED_SCOPES and _field_scope(field) != explicit_scope
                ],
                conflict_fields=[],
                reason_code="OVERRIDE_APPLIED",
                clarification_prompt_class=None,
            )
        return _decision(
            decision="ROUTE",
            selected_scope=resolved_scope,
            routing_mode="CONTEXT_VERIFIED",
            fields_used=[field["field_name"] for field in active_routing_fields],
            provenance_used=_unique(field.get("provenance") for field in active_routing_fields),
            explicit_override=False,
            invalidated_fields=[],
            stale_fields=[],
            conflict_fields=[],
            reason_code="VERIFIED_CONTEXT",
            clarification_prompt_class=None,
        )

    if explicit_scope in SUPPORTED_SCOPES:
        return _decision(
            decision="ROUTE",
            selected_scope=explicit_scope,
            routing_mode="STANDALONE_EXPLICIT",
            fields_used=["question_text"],
            provenance_used=["CURRENT_TURN_EXPLICIT"],
            explicit_override=False,
            invalidated_fields=[],
            stale_fields=[],
            conflict_fields=[],
            reason_code="EXPLICIT_EQUIPMENT_FAMILY",
            clarification_prompt_class=None,
        )

    return _decision(
        decision=_abstain_decision(clarification_suppressed),
        selected_scope=None,
        routing_mode="ABSTAIN_NO_CONTEXT",
        fields_used=[],
        provenance_used=[],
        explicit_override=False,
        invalidated_fields=[],
        stale_fields=[],
        conflict_fields=[],
        reason_code="NO_CONTEXT",
        clarification_prompt_class=_clarification_prompt(clarification_suppressed),
    )


def route_with_discovery(
    question_evidence: dict[str, Any],
    context_state: dict[str, Any],
    discovery_state: dict[str, Any] | None = None,
    prior_process_state: str | None = None,
    confirmation_object: dict[str, Any] | None = None,
    constrained_retrieval_result: dict[str, Any] | None = None,
    clarification_suppressed: bool = False,
) -> dict[str, Any]:
    if constrained_retrieval_result is not None:
        failed = (
            constrained_retrieval_result.get("status") == "FAILED"
            or constrained_retrieval_result.get("authority_verification") == "FAILED"
            or not constrained_retrieval_result.get("usable_evidence", False)
        )
        if failed:
            return {
                **route_context(question_evidence, context_state, clarification_suppressed),
                "decision": "FAIL_CLOSED_AFTER_CONFIRMATION",
                "selected_scope": None,
                "process_state": "FAIL_CLOSED",
                "technical_answer_allowed": False,
                "reason_code": "AUTHORITY_VERIFICATION_FAILED",
                "clarification_required": False,
                "constrained_retrieval_required": True,
                "explicit_confirmation": True,
                "reuse_preconfirmation_candidates": False,
            }

    if confirmation_object is not None:
        return _decision(
            decision="CONFIRMED_CONSTRAINED_RETRIEVAL",
            selected_scope=confirmation_object.get("confirmed_scope"),
            routing_mode="CURRENT_TURN_OVERRIDE",
            fields_used=[
                field_name
                for field_name in (
                    "confirmed_equipment",
                    "confirmed_engine_family",
                    "confirmed_manual_family",
                    "confirmed_scope",
                )
                if confirmation_object.get(field_name)
            ],
            provenance_used=[confirmation_object.get("provenance", "CURRENT_TURN_EXPLICIT_CONFIRMATION")],
            explicit_override=False,
            invalidated_fields=list(confirmation_object.get("invalidated_alternatives", [])),
            stale_fields=[],
            conflict_fields=[],
            reason_code="CONFIRMATION_APPLIED",
            clarification_prompt_class=None,
            process_state="CONSTRAINED_RETRIEVAL_REQUIRED",
            technical_answer_allowed=False,
            clarification_required=False,
            constrained_retrieval_required=True,
            explicit_confirmation=True,
            reuse_preconfirmation_candidates=False,
        )

    plausible = _materially_plausible_families(discovery_state)
    if len(plausible) >= 2:
        return _decision(
            decision="CLARIFY_MULTI_FAMILY",
            selected_scope=None,
            routing_mode="ABSTAIN_CONFLICT",
            fields_used=[],
            provenance_used=[],
            explicit_override=False,
            invalidated_fields=[],
            stale_fields=[field.get("field_name") for field in context_state.get("fields", []) if field.get("stale")],
            conflict_fields=[],
            reason_code="DISCOVERY_AMBIGUOUS_FAMILIES",
            clarification_prompt_class=_clarification_prompt(clarification_suppressed),
            process_state="AMBIGUOUS_RESULTS",
            technical_answer_allowed=False,
            clarification_required=True,
            constrained_retrieval_required=False,
            explicit_confirmation=False,
            reuse_preconfirmation_candidates=False,
        )

    if len(plausible) == 1:
        family = plausible[0]
        explicit_scope = question_evidence.get("explicit_scope") or family.get("implied_scope")
        return _decision(
            decision="ROUTE",
            selected_scope=explicit_scope,
            routing_mode=(
                "CURRENT_TURN_OVERRIDE"
                if context_state.get("context_state") == "CURRENT_TURN_OVERRIDE"
                else "STANDALONE_EXPLICIT"
            ),
            fields_used=["question_text"],
            provenance_used=["CURRENT_TURN_EXPLICIT"],
            explicit_override=False,
            invalidated_fields=[],
            stale_fields=[],
            conflict_fields=[],
            reason_code=question_evidence.get("explicit_anchor_type") or "EXPLICIT_EQUIPMENT_FAMILY",
            clarification_prompt_class=None,
            process_state="DISCOVERY_UNAMBIGUOUS",
            technical_answer_allowed=True,
            clarification_required=False,
            constrained_retrieval_required=False,
            explicit_confirmation=False,
            reuse_preconfirmation_candidates=False,
        )

    return _decision(
        decision=_abstain_decision(clarification_suppressed),
        selected_scope=None,
        routing_mode="ABSTAIN_NO_CONTEXT",
        fields_used=[],
        provenance_used=[],
        explicit_override=False,
        invalidated_fields=[],
        stale_fields=[],
        conflict_fields=[],
        reason_code="NO_CONTEXT",
        clarification_prompt_class=_clarification_prompt(clarification_suppressed),
        process_state="DISCOVERY_REQUIRED",
        technical_answer_allowed=False,
        clarification_required=not clarification_suppressed,
        constrained_retrieval_required=False,
        explicit_confirmation=False,
        reuse_preconfirmation_candidates=False,
    )


def _decision(
    *,
    decision: str,
    selected_scope: str | None,
    routing_mode: str,
    fields_used: list[str],
    provenance_used: list[str],
    explicit_override: bool,
    invalidated_fields: list[str],
    stale_fields: list[str],
    conflict_fields: list[str],
    reason_code: str,
    clarification_prompt_class: str | None,
    process_state: str | None = None,
    technical_answer_allowed: bool | None = None,
    clarification_required: bool | None = None,
    constrained_retrieval_required: bool | None = None,
    explicit_confirmation: bool = False,
    reuse_preconfirmation_candidates: bool | None = None,
) -> dict[str, Any]:
    payload = {
        "decision": decision,
        "selected_scope": selected_scope,
        "routing_mode": routing_mode,
        "fields_used": fields_used,
        "provenance_used": provenance_used,
        "explicit_override": explicit_override,
        "invalidated_fields": invalidated_fields,
        "stale_fields": stale_fields,
        "conflict_fields": conflict_fields,
        "reason_code": reason_code,
        "clarification_prompt_class": clarification_prompt_class,
    }
    if process_state is not None:
        payload["process_state"] = process_state
    if technical_answer_allowed is not None:
        payload["technical_answer_allowed"] = technical_answer_allowed
    if clarification_required is not None:
        payload["clarification_required"] = clarification_required
    if constrained_retrieval_required is not None:
        payload["constrained_retrieval_required"] = constrained_retrieval_required
    payload["explicit_confirmation"] = explicit_confirmation
    if reuse_preconfirmation_candidates is not None:
        payload["reuse_preconfirmation_candidates"] = reuse_preconfirmation_candidates
    return payload


def _resolve_unique_scope(fields: list[dict[str, Any]]) -> str | None:
    scopes = {_field_scope(field) for field in fields}
    scopes.discard(None)
    if len(scopes) == 1:
        return next(iter(scopes))
    return None


def _field_scope(field: dict[str, Any]) -> str | None:
    field_name = field.get("field_name")
    value = str(field.get("value", ""))
    if field_name == "active_scope" and value in SUPPORTED_SCOPES:
        return value
    for token, scope in CONTEXT_VALUE_SCOPE_RULES:
        if token.lower() in value.lower():
            return scope
    return None


def _is_current_turn_explicit(field: dict[str, Any]) -> bool:
    return field.get("provenance") in {"CURRENT_TURN_EXPLICIT", "CURRENT_TURN_EXPLICIT_CONFIRMATION"} or field.get(
        "field_state"
    ) == "CURRENT_TURN_OVERRIDE"


def _invalidated_field_names(
    fields: list[dict[str, Any]],
    *,
    keep_scope: str | None,
    stale_only: bool = False,
) -> list[str]:
    invalidated: list[str] = []
    for field in fields:
        if field.get("field_name") not in ROUTING_FIELD_NAMES:
            continue
        if stale_only and not field.get("stale"):
            continue
        field_scope = _field_scope(field)
        if keep_scope is None or field_scope != keep_scope:
            invalidated.append(field["field_name"])
    return invalidated


def _has_equal_authority_conflict(fields: list[dict[str, Any]]) -> bool:
    scoped_fields = [field for field in fields if _field_scope(field) in SUPPORTED_SCOPES]
    if len(scoped_fields) < 2:
        return False
    top_precedence = max(PROVENANCE_PRECEDENCE.get(str(field.get("provenance") or ""), 0) for field in scoped_fields)
    top_scopes = {
        _field_scope(field)
        for field in scoped_fields
        if PROVENANCE_PRECEDENCE.get(str(field.get("provenance") or ""), 0) == top_precedence
    }
    return len(top_scopes) > 1


def _materially_plausible_families(discovery_state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not discovery_state:
        return []
    plausible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for family in discovery_state.get("materially_plausible_families", []):
        family_name = str(family.get("family", "")).strip()
        if not family_name or family_name in seen:
            continue
        if not all(
            family.get(flag)
            for flag in ("authority_eligible", "survived_controls", "value_plausible")
        ):
            continue
        seen.add(family_name)
        plausible.append(family)
    return plausible


def _clarification_prompt(suppressed: bool) -> str | None:
    if suppressed:
        return None
    return "REQUEST_MANUAL_OR_ENGINE_FAMILY"


def _abstain_decision(suppressed: bool) -> str:
    return "ABSTAIN_NO_PROMPT" if suppressed else "ABSTAIN_CLARIFY"


def _unique(values: Any) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value is None or value in result:
            continue
        result.append(value)
    return result


__all__ = [
    "LAYER_A_ROUTING_MODES",
    "PROVENANCE_PRECEDENCE",
    "SUPPORTED_SCOPES",
    "parse_question_evidence",
    "route_context",
    "route_with_discovery",
]

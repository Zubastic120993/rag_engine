# -*- coding: utf-8 -*-
"""B9I: query-evidence admissibility - discriminating anchors required.

Coherent wrong-equipment / fictional-entity packages that only share weak
machinery vocabulary must not earn coverage=full.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine import query


class _FakeDoc:
    def __init__(
        self,
        source: str,
        page: int = 1,
        collection: str = "maker-manuals",
        content: str = "chunk",
        **extra,
    ):
        self.metadata = {
            "source": source,
            "page": page,
            "collection": collection,
            **extra,
        }
        self.page_content = content


def _diag(**overrides):
    base = {
        "score_floor": 0.38,
        "best_raw_distance": 0.55,
        "raw_count": 5,
        "post_admissibility_count": 5,
        "post_scope_count": 5,
        "post_rerank_count": 5,
        "post_dedupe_count": 5,
    }
    base.update(overrides)
    return base


def _man_exhaust_pairs():
    src = (
        "00_Career/03_Engine_Knowledge/MAN_G60ME-C/"
        "MAN_B&W_G60ME-C_Technical_Documentation.pdf"
    )
    return [
        (
            _FakeDoc(
                src,
                page=347,
                content="Grind the exhaust valve seat according to MAN Energy Solutions instructions.",
            ),
            0.50,
        ),
        (
            _FakeDoc(
                src,
                page=348,
                content="Exhaust valve grinding and seat truing-up procedure for the engine.",
            ),
            0.51,
        ),
    ]


def _crew_pairs():
    src = "00_Career/02_Statutory/Port_State_Guidance/CFR-2016-title46-vol4.pdf"
    return [
        (
            _FakeDoc(
                src,
                page=377,
                collection="regulatory",
                content="Crew list and cabin assignment requirements for passenger vessels.",
            ),
            0.71,
        ),
        (
            _FakeDoc(
                src,
                page=383,
                collection="regulatory",
                content="Maintain an accurate crew list for overnight accommodations.",
            ),
            0.72,
        ),
    ]


def _turbo_pairs():
    src = "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/M 1.3.pdf"
    return [
        (
            _FakeDoc(
                src,
                page=148,
                content="Turbocharger spare part number list and impeller designation table.",
            ),
            0.69,
        ),
        (
            _FakeDoc(
                src,
                page=239,
                content="Turbocharger impeller used on this engine; spare parts catalogue.",
            ),
            0.70,
        ),
    ]


# ---------------------------------------------------------------------------
# Exact B9 regressions (must not hard-code pass via string equality in gate)
# ---------------------------------------------------------------------------


def test_b9_q20_fictional_crew_coherent_package_fails(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    retained, diagnostics = query._apply_final_confidence_gate(
        _crew_pairs(),
        diagnostics=_diag(best_raw_distance=0.71),
        question=(
            "What is the exact crew list and cabin assignment for "
            "MV Fictional Horizon on 1 January 2099?"
        ),
    )
    assert retained == []
    assert diagnostics["coherent_support"] is True
    assert diagnostics["strong_distance"] is False
    assert diagnostics["topical_agreement"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"
    adm = diagnostics["query_admissibility"]
    assert "fictional" in adm["discriminating_anchors"] or "horizon" in adm["discriminating_anchors"]
    assert adm["query_support_sufficient"] is False


def test_b9_q21_fictional_zx9000_coherent_package_fails(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    retained, diagnostics = query._apply_final_confidence_gate(
        _turbo_pairs(),
        diagnostics=_diag(best_raw_distance=0.69),
        question=(
            "What is the spare part number for the ZX-9000 quantum turbocharger "
            "impeller used on Gaschem Europa?"
        ),
    )
    assert retained == []
    assert diagnostics["final_confidence_passed"] is False
    adm = diagnostics["query_admissibility"]
    assert any(a in adm["discriminating_anchors"] for a in ("zx-9000", "9000", "quantum", "gaschem", "europa"))
    assert adm["query_support_sufficient"] is False


def test_b9_q23_wartsila_man_exhaust_package_fails(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    retained, diagnostics = query._apply_final_confidence_gate(
        _man_exhaust_pairs(),
        diagnostics=_diag(best_raw_distance=0.50),
        question="Wartsila dual-fuel DF engine exhaust valve seat grinding procedure",
    )
    assert retained == []
    assert diagnostics["coherent_support"] is True
    assert diagnostics["topical_agreement_with_coherent_support"] is False
    assert diagnostics["final_confidence_passed"] is False
    adm = diagnostics["query_admissibility"]
    assert "wartsila" in adm["discriminating_anchors"]
    assert "wartsila" not in adm["matched_discriminating_anchors"]


def test_b9_q20_answer_path_no_full_coverage(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = _crew_pairs()
    diag = _diag(best_raw_distance=0.71, raw_count=400)
    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        return_value=(pairs, diag),
    ):
        result = query.answer(
            "What is the exact crew list and cabin assignment for "
            "MV Fictional Horizon on 1 January 2099?"
        )
    assert result.status == "no_coverage"
    assert result.coverage == "none"
    assert not (result.status == "ok" and result.coverage == "full")


def test_b9_q21_answer_path_no_full_coverage(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = _turbo_pairs()
    diag = _diag(best_raw_distance=0.69, raw_count=400)
    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        return_value=(pairs, diag),
    ):
        result = query.answer(
            "What is the spare part number for the ZX-9000 quantum turbocharger "
            "impeller used on Gaschem Europa?"
        )
    assert result.status == "no_coverage"
    assert result.coverage == "none"


def test_b9_q23_answer_path_no_full_coverage(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = _man_exhaust_pairs()
    diag = _diag(best_raw_distance=0.50, raw_count=400)
    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        return_value=(pairs, diag),
    ):
        result = query.answer(
            "Wartsila dual-fuel DF engine exhaust valve seat grinding procedure"
        )
    assert result.status == "no_coverage"
    assert result.coverage == "none"


# ---------------------------------------------------------------------------
# Generalized adversarial / synthetic cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,content,should_pass",
    [
        (
            "AlphaPrime XP-77 injector calibration procedure",
            "Generic injector calibration procedure for the engine fuel system.",
            False,
        ),
        (
            "AlphaPrime XP-77 injector calibration procedure",
            "AlphaPrime XP-77 injector calibration procedure and torque table.",
            True,
        ),
        (
            "NeoForge NF-12 bearing clearance check",
            "Bearing clearance check procedure for main engine bearings.",
            False,
        ),
        (
            "NeoForge NF-12 bearing clearance check",
            "NeoForge NF-12 bearing clearance check values.",
            True,
        ),
        (
            "Wartsila dual-fuel exhaust valve grinding",
            "MAN Energy Solutions exhaust valve grinding and seat procedure.",
            False,
        ),
        (
            "Wartsila dual-fuel exhaust valve grinding",
            "Wartsila dual-fuel exhaust valve grinding procedure.",
            True,
        ),
        (
            "Sulzer RTA exhaust valve overhaul torque",
            "MAN G60ME-C exhaust valve overhaul torque table.",
            False,
        ),
        (
            "ZX-9000 quantum turbocharger impeller spare",
            "Turbocharger impeller spare part number list for this engine.",
            False,
        ),
        (
            "MV Phantom Star crew list cabin assignment 2099",
            "Crew list and cabin assignment requirements for vessels.",
            False,
        ),
        (
            "Yanmar SCR dosing valve inspection",
            "Yanmar SCR dosing valve inspection procedure and settings.",
            True,
        ),
    ],
)
def test_discriminating_overlap_matrix(monkeypatch, question, content, should_pass):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    src = "00_Career/03_Engine_Knowledge/SomeMaker/manual.pdf"
    if "Yanmar" in content or "Yanmar" in question:
        src = "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION.pdf"
    if "MAN" in content:
        src = "00_Career/03_Engine_Knowledge/MAN_G60ME-C/doc.pdf"
    pairs = [
        (_FakeDoc(src, page=1, content=content), 0.45),
        (_FakeDoc(src, page=2, content=content + " Additional coherent support."), 0.46),
    ]
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs, diagnostics=_diag(best_raw_distance=0.45), question=question
    )
    if should_pass:
        assert diagnostics["final_confidence_passed"] is True
        assert retained
    else:
        assert diagnostics["final_confidence_passed"] is False
        assert retained == []


@pytest.mark.parametrize(
    "token,expect_disc",
    [
        ("wartsila", True),
        ("zx-9000", True),
        ("9000", True),
        ("dual-fuel", True),
        ("fictional", True),
        ("g50me", True),
        ("exhaust", False),
        ("grinding", False),
        ("engine", False),
        ("crew", False),
        ("turbocharger", False),
        ("spare", False),
        ("valve", False),  # not an anchor usually; if present not disc
        ("ows-com", True),
        ("yanmar", True),
    ],
)
def test_discriminating_anchor_classifier(token, expect_disc):
    assert query._is_discriminating_anchor(token) is expect_disc


def test_weak_only_query_cannot_earn_topical_agreement():
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/General/op.pdf",
                content="Exhaust valve seat grinding procedure for the engine.",
            ),
            0.45,
        )
    ]
    assert query._query_topical_agreement("exhaust valve seat grinding", pairs) is False


def test_correct_maker_with_discriminating_overlap_passes(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/x.pdf",
                content="MAN G50ME-C exhaust valve seat grinding procedure.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/x.pdf",
                content="Additional G50ME-C grinding notes.",
            ),
            0.46,
        ),
    ]
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="MAN G50ME-C exhaust valve seat grinding procedure",
    )
    assert diagnostics["final_confidence_passed"] is True
    assert retained


def test_generic_query_without_disc_requires_strong_distance(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/General/op.pdf",
                content="Press stop to halt automatic mode.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/General/op.pdf",
                content="Emergency stop while running automatic.",
            ),
            0.46,
        ),
    ]
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="automatic stop procedure",
    )
    assert diagnostics["strong_distance"] is False
    assert retained == []


def test_strong_distance_still_passes_without_topical(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/General/op.pdf",
                content="Press the stop button in automatic mode.",
            ),
            0.25,
        )
    ]
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.25),
        question="automatic stop procedure",
    )
    assert diagnostics["strong_distance"] is True
    assert diagnostics["final_confidence_passed"] is True
    assert retained


def test_coverage_full_requires_final_admissibility(monkeypatch):
    """coverage=full must not be emitted when final confidence fails."""
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = _man_exhaust_pairs()
    diag = _diag(best_raw_distance=0.50, raw_count=10)
    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        return_value=(pairs, diag),
    ):
        result = query.answer(
            "Wartsila dual-fuel DF engine exhaust valve seat grinding procedure"
        )
    assert result.coverage != "full"
    assert result.retrieval_diagnostics.get("final_confidence_passed") is False


def test_ok_full_answer_null_is_legitimate_orch104_when_admissible(monkeypatch):
    """ORCH_104: ok+full+answer=null remains valid when evidence is admissible."""
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/manual.pdf",
                content="OWS-COM automatic stop and recirculation control description.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/manual.pdf",
                content="During recirculation the OWS-COM pump switches off automatically.",
            ),
            0.46,
        ),
    ]
    diag = _diag(best_raw_distance=0.45, raw_count=10)
    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        return_value=(pairs, diag),
    ):
        result = query.answer("OWS-COM automatic stop recirculation", scope="maker-manuals")
    assert result.status == "ok"
    assert result.coverage == "full"
    assert result.answer is None


def test_score_floor_is_diagnostic_component_of_strong_distance(monkeypatch):
    """score_floor gates strong_distance only; not a global hard reject alone."""
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = _man_exhaust_pairs()
    # Above floor: strong_distance false; discriminating fail => reject
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.50, score_floor=0.38),
        question="Wartsila dual-fuel exhaust valve grinding",
    )
    assert diagnostics["strong_distance"] is False
    assert diagnostics["score_floor"] == 0.38
    assert retained == []


def test_coherent_wrong_cluster_generic_overlap_fails(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G60ME-C/a.pdf",
                content="Engine exhaust grinding seat procedure chapter.",
            ),
            0.48,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G60ME-C/a.pdf",
                content="More engine exhaust grinding seat notes.",
            ),
            0.49,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G60ME-C/b.pdf",
                content="Engine exhaust grinding coherent sibling.",
            ),
            0.50,
        ),
    ]
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.48),
        question="HyperDrive HD-9000 exhaust valve seat grinding",
    )
    assert diagnostics["coherent_support"] is True
    assert diagnostics["final_confidence_passed"] is False
    assert retained == []


def test_mixed_correct_wrong_uses_coherent_subset_only(monkeypatch):
    """Discriminating overlap must come from coherent support, not hitchhiker."""
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G60ME-C/generic.pdf",
                content="Exhaust valve seat grinding for the engine.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G60ME-C/generic.pdf",
                content="More exhaust grinding seat notes.",
            ),
            0.46,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Wartsila_DF/manual.pdf",
                content="Wartsila dual-fuel exhaust valve seat grinding procedure.",
            ),
            0.70,
        ),
    ]
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="Wartsila dual-fuel exhaust valve seat grinding",
    )
    # Coherent support is MAN same-source; Wartsila hitchhiker must not lend topicality.
    assert diagnostics["top_source_support"] >= 2
    assert diagnostics["topical_agreement_with_coherent_support"] is False
    assert retained == []


def test_no_hardcoded_b9_query_strings_in_gate_source():
    src = Path(query.__file__).read_text(encoding="utf-8")
    for banned in (
        "Fictional Horizon",
        "ZX-9000",
        "Wartsila dual-fuel DF engine exhaust valve seat grinding procedure",
        "Gaschem Europa",
    ):
        assert banned not in src


def test_query_admissibility_diagnostics_shape(monkeypatch):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    _, diagnostics = query._apply_final_confidence_gate(
        _man_exhaust_pairs(),
        diagnostics=_diag(best_raw_distance=0.50),
        question="Wartsila dual-fuel exhaust grinding",
    )
    adm = diagnostics["query_admissibility"]
    assert set(adm) >= {
        "query_anchors",
        "discriminating_anchors",
        "matched_anchors",
        "matched_discriminating_anchors",
        "query_support_sufficient",
        "explicit_contradiction",
    }


# Expand to >=120 with parametrized synthetic families
_FICTIONAL_MAKERS = [
    "AstraDrive",
    "BoltWave",
    "CryoSpin",
    "DeltaForge",
    "EchoMarine",
    "FluxGear",
    "GyroNova",
    "HelioPump",
    "IonStack",
    "JadeTurbine",
    "KiloValve",
    "LumenGear",
    "MiraDrive",
    "NovaSeal",
    "OrionPump",
    "PulseGear",
    "QuantumHull",  # word quantum alone is disc; compound ok
    "RotorLynx",
    "SigmaBlade",
    "TitanInjector",
]


@pytest.mark.parametrize("maker", _FICTIONAL_MAKERS)
def test_fictional_maker_generic_overlap_fails(monkeypatch, maker):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/m.pdf",
                content="Engine exhaust valve grinding seat spare turbocharger procedure.",
            ),
            0.50,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/m.pdf",
                content="More engine exhaust grinding seat turbocharger spare notes.",
            ),
            0.51,
        ),
    ]
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.50),
        question=f"{maker} exhaust valve seat grinding procedure",
    )
    assert retained == []
    assert diagnostics["final_confidence_passed"] is False


@pytest.mark.parametrize("model", [f"ZX-{n}000" for n in range(1, 21)])
def test_fictional_model_codes_fail_on_generic_turbo_docs(monkeypatch, model):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    retained, diagnostics = query._apply_final_confidence_gate(
        _turbo_pairs(),
        diagnostics=_diag(best_raw_distance=0.69),
        question=f"{model} quantum turbocharger impeller spare part number",
    )
    assert retained == []
    assert diagnostics["final_confidence_passed"] is False


@pytest.mark.parametrize(
    "question",
    [
        "crew list cabin assignment MV Mythic Wave 2099",
        "crew list cabin assignment MV Ghost Carrier 2088",
        "crew list cabin assignment MV Paper Ship 2077",
        "crew list cabin assignment SS Imaginary Isle 2066",
        "crew list cabin assignment MV NotARealBoat 2055",
    ],
)
def test_fictional_vessel_crew_queries_fail(monkeypatch, question):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    retained, diagnostics = query._apply_final_confidence_gate(
        _crew_pairs(),
        diagnostics=_diag(best_raw_distance=0.71),
        question=question,
    )
    assert retained == []


@pytest.mark.parametrize(
    "path_family",
    [
        "MAN_G50ME-C_LGIP",
        "MAN_G60ME-C",
        "Yanmar_6EY22",
        "OWS_RWO",
    ],
)
def test_path_family_alone_without_disc_overlap_fails_for_foreign_maker(
    monkeypatch, path_family
):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    src = f"00_Career/03_Engine_Knowledge/{path_family}/manual.pdf"
    pairs = [
        (_FakeDoc(src, content="Engine exhaust grinding seat procedure."), 0.48),
        (_FakeDoc(src, content="Engine exhaust grinding seat chapter two."), 0.49),
    ]
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.48),
        question="Wartsila dual-fuel exhaust valve seat grinding",
    )
    assert retained == []
    assert diagnostics["final_confidence_passed"] is False


def test_frozen_b9_query_set_present():
    audit = sorted(
        Path.home().glob(".hermes/audits/rag_metadata_provenance_phase_b9i_*")
    )[-1]
    frozen = json.loads((audit / "fixtures" / "B9I_FROZEN_QUERY_SET.json").read_text())
    ids = {q["id"] for q in frozen["queries"]}
    assert {"Q20", "Q21", "Q23", "Q09", "Q10", "Q13"} <= ids


# Positive control unit: discriminating overlap present
@pytest.mark.parametrize(
    "question,needle",
    [
        ("OWS-COM automatic stop recirculation", "OWS-COM"),
        ("Yanmar SCR dosing valve inspection", "Yanmar"),
        ("MEPC 307 EGR bleed NOx", "MEPC"),
        ("SIRE 2.0 oil mist detection", "SIRE"),
        ("Hartmann SMS bunkering requirements", "Hartmann"),
        ("G50ME-C lubricating oil temperature", "G50ME-C"),
        ("MARPOL Annex VI fuel oil sampling", "MARPOL"),
    ],
)
def test_positive_control_discriminating_present_in_matching_evidence(
    monkeypatch, question, needle
):
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    src = "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/manual.pdf"
    if "Yanmar" in needle:
        src = "00_Career/03_Engine_Knowledge/Yanmar_6EY22/op.pdf"
    if "MEPC" in needle or "MARPOL" in needle:
        src = "00_Career/02_Statutory/MARPOL/doc.pdf"
    if "SIRE" in needle:
        src = "00_Career/02_Statutory/SIRE_OCIMF/doc.pdf"
    if "Hartmann" in needle:
        src = "10_Company/Hartmann/SMS_IMM/C.01.00.pdf"
    if "G50ME" in needle:
        src = "00_Career/03_Engine_Knowledge/MAN_G50ME-C_LGIP/Manual/m.pdf"
    pairs = [
        (_FakeDoc(src, content=f"{needle} relevant procedure text with detail."), 0.40),
        (_FakeDoc(src, content=f"More {needle} coherent supporting text."), 0.41),
    ]
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs, diagnostics=_diag(best_raw_distance=0.40), question=question
    )
    assert diagnostics["topical_agreement"] is True or diagnostics["strong_distance"]
    # Authority may vary; topical path should see discriminating overlap.
    assert query._query_topical_agreement(question, pairs) is True

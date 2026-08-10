"""Regression: coherent-but-off-topic hits must not pass final confidence.

ORCH_108 — vessels OWS-COM probe was returning ok/coverage=full from FCM /
sewage / incinerator stop-procedure chunks that never mentioned OWS.

Follow-up: topical agreement is bound to the coherent-support /
top-authority evidence subset (not any retained hitchhiker hit).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_engine import query


class _FakeDoc:
    def __init__(
        self,
        source: str,
        page: int = 1,
        collection: str = "vessels",
        content: str = "chunk",
    ):
        self.metadata = {"source": source, "page": page, "collection": collection}
        self.page_content = content


@pytest.fixture()
def scopes_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = {
        "defaults": {
            "library_root_env": "CE_LIBRARY_ROOT",
            "library_root_default": str(tmp_path / "lib"),
            "db_path_env": "RAG_DB_PATH",
            "db_path_default": None,
            "embed_model_env": "RAG_EMBED_MODEL",
            "embed_model_default": "mxbai-embed-large",
            "llm_model_env": "RAG_LLM_MODEL",
            "llm_model_default": "gpt-5.6-luna",
            "llm_fallback_model_env": "RAG_LLM_FALLBACK_MODEL",
            "llm_fallback_model_default": "qwen3.5:9b",
            "llm_num_ctx_env": "RAG_LLM_NUM_CTX",
            "llm_num_ctx_default": 8192,
            "llm_num_predict_env": "RAG_LLM_NUM_PREDICT",
            "llm_num_predict_default": 1024,
            "chunk_size": 800,
            "chunk_overlap": 100,
            "default_k": 5,
        },
        "scopes": {
            "vessels": {
                "description": "Vessels",
                "hermes_aliases": ["manual_library_gaschem_europe"],
                "path_prefixes": ["20_Vessels/"],
            },
            "maker-manuals": {
                "description": "Maker",
                "hermes_aliases": ["manual_library"],
                "path_prefixes": ["00_Career/03_Engine_Knowledge/"],
            },
            "other": {"description": "Other", "hermes_aliases": [], "path_prefixes": []},
        },
        "prefix_order": ["vessels", "maker-manuals"],
    }
    lib = tmp_path / "lib"
    lib.mkdir()
    path = tmp_path / "scopes.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    monkeypatch.setenv("CE_LIBRARY_ROOT", str(lib))
    monkeypatch.setenv("RAG_DB_PATH", str(tmp_path / "db"))
    (tmp_path / "db").mkdir()

    import rag_engine.config as cfg

    monkeypatch.setattr(cfg, "SCOPES_FILE", path)
    cfg.load_registry.cache_clear()
    yield path
    cfg.load_registry.cache_clear()


def _diag(**overrides):
    base = {
        "score_floor": 0.38,
        "best_raw_distance": 0.5677,
        "raw_count": 5,
        "post_admissibility_count": 5,
        "post_scope_count": 5,
        "post_rerank_count": 5,
        "post_dedupe_count": 5,
    }
    base.update(overrides)
    return base


def test_b_coherent_same_family_wrong_topic_fails_confidence(monkeypatch):
    """Coherent authoritative family + stop/auto vocabulary must not pass."""
    family = "20_Vessels/Gaschem_Europe/01_Manuals"
    pairs = [
        (
            _FakeDoc(
                f"{family}/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/ops.pdf",
                page=32,
                content="Tap stop button on the HMI screen; stop the pumps manually.",
            ),
            0.5677,
        ),
        (
            _FakeDoc(
                f"{family}/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/ops.pdf",
                page=51,
                content="Emergency stop button activated while system running in manual mode.",
            ),
            0.6378,
        ),
        (
            _FakeDoc(
                f"{family}/10_Sewage_Treatment_Plant/3.2_Manual_CS-BIO_Rev6.1.pdf",
                page=105,
                content="does not operate in automatic mode; overflow alarm; stop wastewater feed.",
            ),
            0.6766,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(),
        question="OWS-COM automatic stop recirculation",
    )

    assert retained == []
    assert diagnostics["coherent_support"] is True
    assert diagnostics["strong_distance"] is False
    assert diagnostics["topical_agreement"] is False
    assert diagnostics["topical_agreement_with_coherent_support"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_a_hitchhiker_anchor_outside_coherent_support_fails(monkeypatch):
    """A: Coherent off-topic source + unrelated retained OWS hit must FAIL."""
    family = "20_Vessels/Gaschem_Europe/01_Manuals"
    pairs = [
        (
            _FakeDoc(
                f"{family}/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/ops.pdf",
                page=32,
                content="Tap stop button on the HMI screen; stop the pumps manually.",
            ),
            0.5677,
        ),
        (
            _FakeDoc(
                f"{family}/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/ops.pdf",
                page=51,
                content="Emergency stop button activated while system running in manual mode.",
            ),
            0.6378,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/unrelated_hitchhiker.pdf",
                page=1,
                collection="maker-manuals",
                content="The OWS-COM separator is described in this unrelated retained chunk.",
            ),
            0.70,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(),
        question="OWS-COM automatic stop recirculation",
    )

    assert retained == []
    assert diagnostics["coherent_support"] is True
    assert diagnostics["strong_distance"] is False
    assert diagnostics["topical_agreement_with_coherent_support"] is False
    assert diagnostics["topical_agreement"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_a_broad_vessel_family_cross_document_topical_fails(monkeypatch):
    """D: 1× FCM off-topic + 1× OWS topical in same vessel 01_Manuals => FAIL."""
    family = "20_Vessels/Gaschem_Europe/01_Manuals"
    pairs = [
        (
            _FakeDoc(
                f"{family}/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/ops.pdf",
                page=32,
                content="Tap stop button on the HMI screen; stop the pumps manually.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                f"{family}/12_OWS/OWS_COM_Operating_Manual.pdf",
                page=10,
                content="OWS-COM automatic stop and recirculation control description.",
            ),
            0.50,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert retained == []
    assert diagnostics["top_source_support"] < 2
    assert diagnostics["top_family_support"] >= 2
    assert diagnostics["family_coherence_eligible"] is False
    assert diagnostics["coherent_support"] is False
    assert diagnostics["strong_distance"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_a_company_sms_family_hitchhiker_fails(monkeypatch):
    """A: Company SMS off-topic top + topical sibling => MUST FAIL."""
    pairs = [
        (
            _FakeDoc(
                "10_Company/SMS/Procedures/generic_stop.pdf",
                page=1,
                collection="other",
                content="Press the emergency stop button.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "10_Company/SMS/Procedures/OWS_COM_note.pdf",
                page=2,
                collection="other",
                content="OWS-COM automatic stop recirculation control.",
            ),
            0.50,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert retained == []
    assert diagnostics["top_source_support"] < 2
    assert diagnostics["top_family_support"] >= 2
    assert diagnostics["top_authority_family"] == "SMS"
    assert diagnostics["family_coherence_eligible"] is False
    assert diagnostics["coherent_support"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_b_company_forms_family_hitchhiker_fails(monkeypatch):
    """B: Company Forms off-topic top + topical sibling => MUST FAIL."""
    pairs = [
        (
            _FakeDoc(
                "10_Company/Forms/generic.pdf",
                page=1,
                collection="other",
                content="Press the emergency stop button.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "10_Company/Forms/ows_form.pdf",
                page=2,
                collection="other",
                content="OWS-COM automatic stop recirculation checklist.",
            ),
            0.50,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert retained == []
    assert diagnostics["top_authority_family"] == "Forms"
    assert diagnostics["family_coherence_eligible"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_c_statutory_career_fallback_family_hitchhiker_fails(monkeypatch):
    """C: Unrelated statutory/class docs sharing 00_Career => MUST FAIL."""
    pairs = [
        (
            _FakeDoc(
                "00_Career/02_Statutory/IMO/generic_fire.pdf",
                page=1,
                collection="other",
                content="General emergency stop guidance.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/01_Class_Rules/DNV/ows_related.pdf",
                page=2,
                collection="other",
                content="OWS-COM automatic stop recirculation class note.",
            ),
            0.50,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert retained == []
    assert diagnostics["top_authority_family"] == "00_Career"
    assert diagnostics["top_family_support"] >= 2
    assert diagnostics["family_coherence_eligible"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_e_vessel_reference_bucket_family_ineligible(monkeypatch):
    """E: Vessel 10_Reference organizational bucket is not family-coherence eligible."""
    family = "20_Vessels/Gaschem_Europe/10_Reference"
    pairs = [
        (
            _FakeDoc(f"{family}/generic_ref.pdf", page=1, content="Press stop button."),
            0.45,
        ),
        (
            _FakeDoc(
                f"{family}/ows_ref.pdf",
                page=2,
                content="OWS-COM automatic stop recirculation.",
            ),
            0.50,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert diagnostics["top_family_support"] >= 2
    assert diagnostics["family_coherence_eligible"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert retained == []


def test_f_sds_family_confidence_eligibility_false(monkeypatch):
    """F: SDS organizational family is not confidence-allowlisted."""
    from rag_engine.authority import authority_family_counts_for_confidence

    sds_a = "00_Career/07_SDS_Datasheets/02_Manuals/chemical_A.pdf"
    assert authority_family_counts_for_confidence(sds_a) is False

    pairs = [
        (_FakeDoc(sds_a, page=1, content="Generic handling notes."), 0.45),
        (
            _FakeDoc(
                "00_Career/07_SDS_Datasheets/02_Manuals/chemical_B.pdf",
                page=2,
                content="OWS-COM related residue handling note.",
            ),
            0.50,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )
    assert diagnostics["top_authority_family"] == "00_Career/07_SDS_Datasheets"
    assert diagnostics["family_coherence_eligible"] is False
    assert retained == []


def test_g_wiki_family_confidence_eligibility_false(monkeypatch):
    """G: Wiki organizational family is not confidence-allowlisted."""
    from rag_engine.authority import authority_family_counts_for_confidence

    wiki = "90_CE_Wiki/notes/generic_stop.md"
    assert authority_family_counts_for_confidence(wiki) is False

    pairs = [
        (_FakeDoc(wiki, page=1, content="Press stop in automatic mode."), 0.45),
        (
            _FakeDoc(
                "90_CE_Wiki/notes/ows_note.md",
                page=2,
                content="OWS-COM automatic stop recirculation.",
            ),
            0.50,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )
    assert diagnostics["top_authority_family"] == "90_CE_Wiki"
    assert diagnostics["family_coherence_eligible"] is False
    assert retained == []


def test_h_unknown_fallback_family_ineligible():
    """H: Unknown/fallback families are ineligible by default."""
    from rag_engine.authority import authority_family_counts_for_confidence

    assert authority_family_counts_for_confidence("misc/unscoped/doc.pdf") is False
    assert authority_family_counts_for_confidence("random/path/file.pdf", "SMS") is False


def test_b_broad_vessel_family_non_topical_manuals_fail(monkeypatch):
    """B: Unrelated same-family vessel manuals with no query anchor => FAIL."""
    family = "20_Vessels/Gaschem_Europe/01_Manuals"
    pairs = [
        (
            _FakeDoc(
                f"{family}/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/ops.pdf",
                page=32,
                content="Tap stop button on the HMI screen; stop the pumps manually.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                f"{family}/10_Sewage_Treatment_Plant/3.2_Manual_CS-BIO_Rev6.1.pdf",
                page=105,
                content="does not operate in automatic mode; overflow alarm; stop wastewater feed.",
            ),
            0.50,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert retained == []
    assert diagnostics["top_family_support"] >= 2
    assert diagnostics["family_coherence_eligible"] is False
    assert diagnostics["coherent_support"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_d_strong_distance_unaffected_by_broad_vessel_family(monkeypatch):
    """D: Broad vessel family must not block legitimate strong-distance pass."""
    family = "20_Vessels/Gaschem_Europe/01_Manuals"
    pairs = [
        (
            _FakeDoc(
                f"{family}/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/ops.pdf",
                page=1,
                content="Press the stop button to stop the system in automatic mode.",
            ),
            0.25,
        ),
        (
            _FakeDoc(
                f"{family}/12_OWS/OWS_COM_Operating_Manual.pdf",
                page=2,
                content="Unrelated sibling retained only to show broad family co-presence.",
            ),
            0.55,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.25),
        question="automatic stop procedure",
    )

    assert diagnostics["strong_distance"] is True
    assert diagnostics["family_coherence_eligible"] is False
    assert len(retained) == 2
    assert diagnostics["final_confidence_passed"] is True


def test_i_yanmar_narrow_maker_family_only_coherence_may_pass(monkeypatch):
    """I: Narrow maker family (Yanmar_6EY22) may still earn family-only coherence."""
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/0ASCR-EN0054.pdf",
                page=20,
                collection="maker-manuals",
                content="Generic dosing overview without distinctive inspection wording.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Yanmar_6EY22/OPERATION MANUAL_Y22SCR.pdf",
                page=10,
                collection="maker-manuals",
                content="Yanmar SCR dosing valve inspection procedure and settings.",
            ),
            0.46,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="Yanmar SCR dosing valve inspection",
    )

    assert diagnostics["top_source_support"] < 2
    assert diagnostics["top_family_support"] >= 2
    assert diagnostics["top_authority_family"] == "Yanmar_6EY22"
    assert diagnostics["family_coherence_eligible"] is True
    assert diagnostics["coherent_support"] is True
    assert diagnostics["topical_agreement_with_coherent_support"] is True
    assert diagnostics["final_confidence_passed"] is True
    assert len(retained) == 2


def test_j_ows_rwo_narrow_maker_family_only_coherence_may_pass(monkeypatch):
    """J: Second narrow equipment family (OWS_RWO) family-only support may PASS."""
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/overview.pdf",
                page=1,
                collection="maker-manuals",
                content="Press the stop button to halt the pump in automatic mode.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/3.2_Manual_OWS_COM_EN_Rev14.pdf",
                page=39,
                collection="maker-manuals",
                content="During recirculation the OWS-COM pump switches off automatically.",
            ),
            0.46,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    # Path of first chunk includes OWS_RWO; use content-only check via gate outcome.
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert diagnostics["top_source_support"] < 2
    assert diagnostics["top_family_support"] >= 2
    assert diagnostics["top_authority_family"] == "OWS_RWO"
    assert diagnostics["family_coherence_eligible"] is True
    assert diagnostics["coherent_support"] is True
    assert diagnostics["final_confidence_passed"] is True
    assert len(retained) == 2


def test_k_training_course_family_is_allowlisted_for_coherence(monkeypatch):
    """K: Training/<course> is allowlisted; authority rank may still block final pass."""
    from rag_engine.authority import authority_family_counts_for_confidence

    src_a = "00_Career/03_Engine_Knowledge/Training/SCR_Basics/lesson_a.pdf"
    src_b = "00_Career/03_Engine_Knowledge/Training/SCR_Basics/lesson_b.pdf"
    assert authority_family_counts_for_confidence(src_a) is True

    pairs = [
        (
            _FakeDoc(
                src_a,
                page=1,
                collection="maker-manuals",
                content="Generic overview of training material.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                src_b,
                page=2,
                collection="maker-manuals",
                content="Yanmar SCR dosing valve inspection procedure in the course notes.",
            ),
            0.46,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)
    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="Yanmar SCR dosing valve inspection",
    )
    assert diagnostics["top_authority_family"] == "Training/SCR_Basics"
    assert diagnostics["family_coherence_eligible"] is True
    assert diagnostics["coherent_support"] is True
    # Training docs are RANK_REFERENCE / non-authoritative under current ranks.
    assert diagnostics["top_canonical_authority_rank"] > 2
    assert diagnostics["final_confidence_passed"] is False
    assert retained == []


def test_f_cross_maker_family_hitchhiker_fails(monkeypatch):
    """L: Cross-family maker hitchhiker cannot lend topicality to another family."""
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Yanmar_6EY22/SCR/generic.pdf",
                page=1,
                collection="maker-manuals",
                content="Press stop to halt the system in automatic mode.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/hitchhiker.pdf",
                page=2,
                collection="maker-manuals",
                content="Yanmar SCR dosing valve inspection appears only in this hitchhiker.",
            ),
            0.55,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="Yanmar SCR dosing valve inspection",
    )

    assert retained == []
    assert diagnostics["top_family_support"] < 2
    assert diagnostics["coherent_support"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_b_same_family_anchor_in_supporting_chunk_may_pass(monkeypatch):
    """B: Anchor in another chunk of the SAME support source/family may PASS.

    Top chunk is operational/generic; a later chunk from the same supporting
    source carries the distinctive equipment id. Path is intentionally free of
    OWS tokens so only the supporting chunk supplies topicality.
    """
    source = (
        "00_Career/03_Engine_Knowledge/SeparatorSystems/"
        "Unit_Manual/3.2_Operating_Instructions.pdf"
    )
    pairs = [
        (
            _FakeDoc(
                source,
                page=10,
                collection="maker-manuals",
                content="Press the stop button to halt the pump in automatic mode.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                source,
                page=39,
                collection="maker-manuals",
                content="During recirculation the OWS-COM pump switches off automatically.",
            ),
            0.46,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    # Confirm the generic top chunk alone would not be topical.
    assert query._query_topical_agreement(
        "OWS-COM automatic stop recirculation", [pairs[0]]
    ) is False
    assert query._query_topical_agreement(
        "OWS-COM automatic stop recirculation", [pairs[1]]
    ) is True

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert len(retained) == 2
    assert diagnostics["strong_distance"] is False
    assert diagnostics["coherent_support"] is True
    assert diagnostics["top_source_support"] >= 2
    assert diagnostics["topical_agreement_with_coherent_support"] is True
    assert diagnostics["topical_agreement"] is True
    assert diagnostics["final_confidence_passed"] is True


def test_c_moderate_relevant_multi_chunk_still_accepted(monkeypatch):
    """Moderately scored but query-relevant coherent chunks remain accepted.

    Documents the intended moderate-distance rule: coherent + topical within
    the support subset (not strong_distance alone, not hitchhiker topicality).
    """
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/3.2_Manual_OWS_COM_EN_Rev14.pdf",
                page=22,
                collection="maker-manuals",
                content="The OWS-COM oil separation interval and flush cycle are aligned.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/3.2_Manual_OWS_COM_EN_Rev14.pdf",
                page=39,
                collection="maker-manuals",
                content="During recirculation the OWS-COM pump switches off automatically.",
            ),
            0.46,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert len(retained) == 2
    assert diagnostics["strong_distance"] is False
    assert diagnostics["coherent_support"] is True
    assert diagnostics["topical_agreement"] is True
    assert diagnostics["topical_agreement_with_coherent_support"] is True
    assert diagnostics["final_confidence_passed"] is True


def test_c_car_fallback_hitchhiker_fails(monkeypatch):
    """C: CAR<=2 top evidence off-topic; unrelated lower hit with anchor => FAIL."""
    pairs = [
        (
            _FakeDoc(
                "10_Company/SMS/Procedures/generic_stop_procedure.pdf",
                page=1,
                collection="other",
                content="Company stop procedure: press the emergency stop button.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/hitchhiker.pdf",
                page=2,
                collection="maker-manuals",
                content="OWS-COM recirculation stop is described here.",
            ),
            0.55,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert retained == []
    assert diagnostics["strong_distance"] is False
    assert diagnostics["top_canonical_authority_rank"] <= 2
    assert diagnostics["topical_agreement_with_top_authority_support"] is False
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_d_car_fallback_anchor_in_top_authority_passes(monkeypatch):
    """D: CAR<=2 authoritative support itself contains anchor => PASS."""
    pairs = [
        (
            _FakeDoc(
                "10_Company/SMS/Procedures/OWS_COM_stop.pdf",
                page=1,
                collection="other",
                content="OWS-COM automatic stop and recirculation control description.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/Training/unrelated.pdf",
                page=1,
                collection="maker-manuals",
                content="Unrelated training note without equipment anchors.",
            ),
            0.55,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert len(retained) == 2
    assert diagnostics["strong_distance"] is False
    assert diagnostics["top_canonical_authority_rank"] <= 2
    assert diagnostics["topical_agreement_with_top_authority_support"] is True
    assert diagnostics["topical_agreement"] is True
    assert diagnostics["final_confidence_passed"] is True


def test_d_strong_distance_authoritative_relevant_still_accepted(monkeypatch):
    """Strong-distance authoritative hit remains accepted even without lexical topic."""
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/OWS_RWO/Separator/3.2_Manual_OWS_COM_EN_Rev14.pdf",
                page=10,
                collection="maker-manuals",
                content="Generic chunk without distinctive anchors in this fixture text.",
            ),
            0.30,
        )
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.30, raw_count=1),
        question="automatic stop procedure",
    )

    assert diagnostics["strong_distance"] is True
    assert len(retained) == 1
    assert diagnostics["final_confidence_passed"] is True


def test_d_strong_distance_passes_without_topical_when_path_also_generic(monkeypatch):
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/General/operation_manual.pdf",
                page=1,
                collection="maker-manuals",
                content="Press the stop button to stop the system in automatic mode.",
            ),
            0.25,
        )
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.25, raw_count=1),
        question="automatic stop procedure",
    )

    assert diagnostics["strong_distance"] is True
    assert diagnostics["topical_agreement"] is False
    assert len(retained) == 1
    assert diagnostics["final_confidence_passed"] is True


@pytest.mark.parametrize(
    "equipment_id",
    [
        "OWS-COM",           # ASCII hyphen-minus
        "OWS\u2010COM",      # HYPHEN
        "OWS\u2011COM",      # NON-BREAKING HYPHEN
        "OWS\u2012COM",      # FIGURE DASH
        "OWS\u2013COM",      # EN DASH
        "OWS\u2014COM",      # EM DASH
        "OWS\u2212COM",      # MINUS SIGN
    ],
)
def test_e_unicode_dash_variants_equivalent_anchors(monkeypatch, equipment_id):
    """E: ASCII / en / em / minus / hyphen equipment ids behave equivalently."""
    source = (
        "00_Career/03_Engine_Knowledge/SeparatorSystems/"
        "Unit_Manual/3.2_Operating_Instructions.pdf"
    )
    pairs = [
        (
            _FakeDoc(
                source,
                page=1,
                collection="maker-manuals",
                content=f"During recirculation the {equipment_id} pump switches off.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                source,
                page=2,
                collection="maker-manuals",
                content="Automatic stop procedure for the separator unit.",
            ),
            0.46,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    assert "ows-com" in query._query_anchor_tokens("OWS-COM automatic stop recirculation")
    assert "ows-com" in query._query_anchor_tokens(f"{equipment_id} automatic stop")
    assert query._query_topical_agreement(
        "OWS-COM automatic stop recirculation", [pairs[0]]
    ) is True

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="OWS-COM automatic stop recirculation",
    )

    assert diagnostics["topical_agreement_with_coherent_support"] is True
    assert diagnostics["final_confidence_passed"] is True
    assert len(retained) == 2


def test_f_all_generic_operational_query_requires_strong_distance(monkeypatch):
    """F: All-generic operational query still requires strong_distance to pass."""
    pairs = [
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/General/operation_manual.pdf",
                page=1,
                collection="maker-manuals",
                content="Press the stop button to stop the system in automatic mode.",
            ),
            0.45,
        ),
        (
            _FakeDoc(
                "00_Career/03_Engine_Knowledge/General/operation_manual.pdf",
                page=2,
                collection="maker-manuals",
                content="Emergency stop activates while running in automatic mode.",
            ),
            0.46,
        ),
    ]
    monkeypatch.setattr(query, "retrieval_score_max", lambda: 0.38)

    retained, diagnostics = query._apply_final_confidence_gate(
        pairs,
        diagnostics=_diag(best_raw_distance=0.45),
        question="automatic stop procedure",
    )

    assert diagnostics["strong_distance"] is False
    assert diagnostics["coherent_support"] is True
    assert query._query_anchor_tokens("automatic stop procedure") == set()
    assert diagnostics["topical_agreement"] is False
    assert diagnostics["topical_agreement_with_coherent_support"] is False
    assert retained == []
    assert diagnostics["final_confidence_passed"] is False
    assert diagnostics["gate"] == "final_confidence_failed"


def test_g_original_ows_com_fcm_regression_fails(scopes_yaml):
    """G: Original OWS-COM vs FCM/CS-BIO/incinerator off-topic evidence => FAIL."""
    family = "20_Vessels/Gaschem_Europe/01_Manuals"
    pairs = [
        (
            _FakeDoc(
                f"{family}/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/"
                "1.5 S6 9012745 02_1 Operating Instructions.pdf",
                page=32,
                content="2.4 Stop\nTap stop button on the HMI screen; stop the pumps manually.",
            ),
            0.5676620602607727,
        ),
        (
            _FakeDoc(
                f"{family}/09_Cargo_LFSS/Fuel_Conditioning_Module_FCM_1.5/"
                "1.5 S6 9012745 02_1 Operating Instructions.pdf",
                page=51,
                content="Emergency Stop Button Activated while running in manual mode.",
            ),
            0.6377946138381958,
        ),
        (
            _FakeDoc(
                f"{family}/10_Sewage_Treatment_Plant/3.2_Manual_CS-BIO_Rev6.1.pdf",
                page=105,
                content="does not operate in automatic mode! Overflow Alarm! Stop wastewater feed!",
            ),
            0.6766383647918701,
        ),
        (
            _FakeDoc(
                f"{family}/10_Sewage_Treatment_Plant/3.2_Manual_CS-BIO_Rev6.1.pdf",
                page=68,
                content="Overflow Emergency Program starts in automatic operation of the plant.",
            ),
            0.6803479194641113,
        ),
        (
            _FakeDoc(
                f"{family}/11_Incinerator/1391.pdf",
                page=24,
                content="Pressing the button stop can stop the sludge circulating pump.",
            ),
            0.6647318005561829,
        ),
    ]
    diag = {
        "score_floor": 0.38,
        "best_raw_distance": 0.5676620602607727,
        "raw_count": 400,
        "post_admissibility_count": 400,
        "post_scope_count": 400,
        "post_rerank_count": 400,
        "post_dedupe_count": 301,
        "gate": None,
    }

    with patch(
        "rag_engine.query.retrieve_with_scores_and_diagnostics",
        return_value=(pairs, diag),
    ):
        result = query.answer(
            "OWS-COM automatic stop recirculation",
            scope="vessels",
        )

    assert result.status == "no_coverage"
    assert result.coverage == "none"
    assert result.answer is None
    assert result.sources == []
    assert result.gate == "final_confidence_failed"
    assert result.retrieval_diagnostics.get("topical_agreement") is False
    assert result.retrieval_diagnostics.get("topical_agreement_with_coherent_support") is False
    assert result.retrieval_diagnostics.get("coherent_support") is True
    assert result.retrieval_diagnostics.get("strong_distance") is False
    assert result.to_json()["status"] == "no_coverage"


def test_e_maker_manual_ows_com_retrieval_does_not_regress():
    """Existing maker-manuals OWS-COM retrieval must remain ok (live index)."""
    import rag_engine.config as cfg

    cfg.load_registry.cache_clear()
    query._get_db.cache_clear()
    result = query.answer(
        "OWS-COM automatic stop recirculation",
        scope="maker-manuals",
    )
    assert result.status == "ok"
    assert result.coverage == "full"
    assert result.sources
    blob = " ".join(
        str(c.get("text") or "") for c in (result.to_json().get("retrieved_chunks") or [])
    ).lower()
    path_blob = " ".join(str(s.get("path") or "") for s in result.sources).lower()
    assert "ows" in blob or "ows" in path_blob

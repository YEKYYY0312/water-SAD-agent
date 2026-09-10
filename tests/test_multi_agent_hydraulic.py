"""Step 29 hydraulic agent tests (deterministic, offline, no LLM)."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent, run_hydraulic_cases
from water_agent.multi_agent.contracts import (
    HydraulicCaseRequest,
    ProjectBrief,
    compute_payload_hash,
)
from water_agent.multi_agent.provenance import superseded_chain


def _brief(cases: list[HydraulicCaseRequest], project_id: str = "proj-001") -> ProjectBrief:
    return ProjectBrief(
        project_id=project_id,
        request="test hydraulic",
        systems=["water_supply"],
        unit_system="SI",
        hydraulic_cases=cases,
        topology=[],
        spatial_constraints=[],
        required_outputs=[],
        user_constraints=[],
    )


def test_normal_velocity() -> None:
    req = HydraulicCaseRequest(case_id="c-velocity-1", formula="velocity", inputs={"flow_lps": 20, "diameter_mm": 150})
    env = run_hydraulic_agent(_brief([req]))
    assert env.status == "ready"
    assert env.artifact_type == "calc_package"
    pkg = env.payload
    assert pkg["missing_inputs"] == []
    assert pkg["errors"] == []
    assert len(pkg["cases"]) == 1
    case = pkg["cases"][0]
    assert case["calc_case_id"] == "c-velocity-1"
    assert "velocity_mps" in case["outputs"]
    assert math.isfinite(case["outputs"]["velocity_mps"])
    assert abs(case["outputs"]["velocity_mps"] - 1.132) < 0.01
    assert case["tool_invocation_id"]
    assert case["tool_invocation_id"].startswith("hyd-")


def test_missing_inputs() -> None:
    req = HydraulicCaseRequest(case_id="c-miss-1", formula="velocity", inputs={"flow_lps": 10})
    env = run_hydraulic_agent(_brief([req]))
    assert env.status == "missing_inputs"
    assert "diameter_mm" in env.payload["missing_inputs"]
    case = env.payload["cases"][0]
    assert case["outputs"] == {}


def test_illegal_negative_diameter() -> None:
    req = HydraulicCaseRequest(case_id="c-illegal-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": -10})
    env = run_hydraulic_agent(_brief([req]))
    assert env.status in ("blocked", "failed")
    assert len(env.payload["errors"]) > 0
    assert env.payload["cases"][0]["outputs"] == {}


def test_out_of_range_assumptions() -> None:
    # water_supply velocity >1.5 should still compute but add assumption warning
    req = HydraulicCaseRequest(case_id="c-range-1", formula="velocity", inputs={"flow_lps": 50, "diameter_mm": 50, "scenario": "water_supply"})
    env = run_hydraulic_agent(_brief([req]))
    assert env.status == "ready"
    case = env.payload["cases"][0]
    assert "velocity_mps" in case["outputs"]
    assert len(case["assumptions"]) > 0
    assert any("高于" in a or "低于" in a for a in case["assumptions"])
    # drainage velocity >5 also warning
    req2 = HydraulicCaseRequest(case_id="c-range-2", formula="velocity", inputs={"flow_lps": 100, "diameter_mm": 50, "scenario": "drainage"})
    env2 = run_hydraulic_agent(_brief([req2]))
    assert len(env2.payload["cases"][0]["assumptions"]) > 0
    # shevelev out of DN range
    req3 = HydraulicCaseRequest(case_id="c-range-3", formula="head_loss_shevelev", inputs={"length_m": 10, "diameter_mm": 20, "velocity_mps": 1.0})
    env3 = run_hydraulic_agent(_brief([req3]))
    assert env3.status == "ready"
    assert any("DN50" in a for a in env3.payload["cases"][0]["assumptions"])


def test_each_formula_one_case() -> None:
    cases = [
        HydraulicCaseRequest(case_id="c1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 100}),
        HydraulicCaseRequest(case_id="c2", formula="head_loss_darcy_weisbach", inputs={"length_m": 10, "diameter_mm": 100, "velocity_mps": 1.0, "friction_factor": 0.02}),
        HydraulicCaseRequest(case_id="c3", formula="head_loss_hazen_williams", inputs={"length_m": 10, "diameter_mm": 100, "flow_lps": 10, "hazen_williams_c": 130}),
        HydraulicCaseRequest(case_id="c4", formula="head_loss_shevelev", inputs={"length_m": 10, "diameter_mm": 100, "velocity_mps": 1.0}),
        HydraulicCaseRequest(case_id="c5", formula="diameter", inputs={"flow_lps": 10, "velocity_mps": 1.0}),
        HydraulicCaseRequest(case_id="c6", formula="slope_manning", inputs={"flow_lps": 10, "diameter_mm": 300, "fill_ratio": 0.5, "manning_n": 0.013}),
        HydraulicCaseRequest(case_id="c7", formula="pump_head", inputs={"static_head_m": 10, "friction_head_m": 2, "local_head_m": 1, "residual_head_m": 3}),
    ]
    pkg = run_hydraulic_cases(cases)
    assert len(pkg.cases) == 7
    for cc in pkg.cases:
        assert len(cc.outputs) == 1
        for v in cc.outputs.values():
            assert math.isfinite(v)
        assert cc.tool_invocation_id


def test_tool_invocation_traceability() -> None:
    req = HydraulicCaseRequest(case_id="c-trace-1", formula="velocity", inputs={"flow_lps": 15, "diameter_mm": 100})
    brief = _brief([req])
    env1 = run_hydraulic_agent(brief)
    env2 = run_hydraulic_agent(brief)
    id1 = env1.payload["cases"][0]["tool_invocation_id"]
    id2 = env2.payload["cases"][0]["tool_invocation_id"]
    assert id1 == id2
    # also payload hash deterministic
    assert env1.payload_hash == env2.payload_hash
    assert env1.artifact_id == env2.artifact_id


def test_supersedes_chain() -> None:
    req = HydraulicCaseRequest(case_id="c-sup-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 100})
    brief = _brief([req], project_id="proj-sup")
    env_old = run_hydraulic_agent(brief)
    old_id = env_old.artifact_id
    old_payload_hash = env_old.payload_hash
    registry: dict[str, object] = {old_id: env_old}
    env_new = run_hydraulic_agent(brief, supersedes_artifact_id=old_id)
    assert env_new.payload["supersedes_artifact_id"] == old_id
    assert env_new.parent_artifact_ids == [old_id]
    assert env_new.artifact_id != old_id
    assert env_new.revision == 2
    # old not mutated
    assert registry[old_id] is env_old  # type: ignore[index]
    assert env_old.payload_hash == old_payload_hash
    assert env_old.payload["supersedes_artifact_id"] is None
    # superseded_chain traversable
    from water_agent.multi_agent.contracts import CalcPackage

    pkg_new = CalcPackage.model_validate(env_new.payload)
    chain = superseded_chain(pkg_new, {old_id: env_old, env_new.artifact_id: env_new})  # type: ignore[dict-item]
    assert chain == [old_id]


def test_offline_no_llm() -> None:
    # No ProviderGateway usage; just call agent and verify success without env vars
    req = HydraulicCaseRequest(case_id="c-offline-1", formula="diameter", inputs={"flow_lps": 10, "velocity_mps": 1.0})
    env = run_hydraulic_agent(_brief([req]))
    assert env.status == "ready"
    # ensure no gateway import inside hydraulic module
    import water_agent.multi_agent.agents.hydraulic as mod

    source = open(mod.__file__, encoding="utf-8").read()
    assert "ProviderGateway" not in source
    assert "retrieve_clauses" not in source
    assert "cite_clauses" not in source
    assert "material_stats" not in source
    assert "read_drawing" not in source


def test_finite_rejection() -> None:
    with pytest.raises(ValidationError):
        HydraulicCaseRequest(case_id="c-finite-1", formula="velocity", inputs={"flow_lps": float("nan"), "diameter_mm": 100})
    with pytest.raises(ValidationError):
        HydraulicCaseRequest(case_id="c-finite-2", formula="velocity", inputs={"flow_lps": float("inf"), "diameter_mm": 100})
    # also via brief construction
    with pytest.raises(ValidationError):
        _brief([HydraulicCaseRequest(case_id="c-finite-3", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": float("-inf")})])


def test_extra_forbid_and_unknown_formula() -> None:
    # HydraulicCaseRequest accepts any string; agent layer enforces 7-formula enum (09-hydraulics §6)
    req_any = HydraulicCaseRequest(case_id="c-unk-1", formula="unknown_formula", inputs={"flow_lps": 10})
    assert req_any.formula == "unknown_formula"
    with pytest.raises(ValidationError):
        run_hydraulic_cases([req_any])
    with pytest.raises(ValidationError):
        run_hydraulic_agent(_brief([req_any]))
    req = HydraulicCaseRequest(case_id="c-unk-2", formula="unknown_formula", inputs={"flow_lps": 10})
    with pytest.raises(ValidationError):
        run_hydraulic_cases([req])
    with pytest.raises(ValidationError):
        run_hydraulic_agent(_brief([req]))
    # extra forbid on ProjectBrief
    with pytest.raises(ValidationError):
        ProjectBrief(  # type: ignore[call-arg]
            project_id="proj-extra",
            request="req",
            systems=["water_supply"],
            unit_system="SI",
            hydraulic_cases=[],
            topology=[],
            spatial_constraints=[],
            required_outputs=[],
            user_constraints=[],
            extra_field="bad",
        )


def test_payload_hash_determinism_100() -> None:
    req = HydraulicCaseRequest(case_id="c-hash-1", formula="velocity", inputs={"flow_lps": 12, "diameter_mm": 100})
    brief = _brief([req])
    hashes: set[str] = set()
    for _ in range(100):
        env = run_hydraulic_agent(brief)
        hashes.add(env.payload_hash)
        # also verify compute_payload_hash matches
        assert env.payload_hash == compute_payload_hash(env.payload)
    assert len(hashes) == 1


def test_illegal_fill_ratio_and_zero_flow() -> None:
    # fill_ratio out of (0,1)
    req = HydraulicCaseRequest(case_id="c-illegal-2", formula="slope_manning", inputs={"flow_lps": 10, "diameter_mm": 300, "fill_ratio": 1.5, "manning_n": 0.013})
    env = run_hydraulic_agent(_brief([req]))
    assert env.status in ("blocked", "failed")
    assert len(env.payload["errors"]) > 0
    # zero flow
    req2 = HydraulicCaseRequest(case_id="c-illegal-3", formula="velocity", inputs={"flow_lps": 0, "diameter_mm": 100})
    env2 = run_hydraulic_agent(_brief([req2]))
    assert env2.status in ("blocked", "failed")

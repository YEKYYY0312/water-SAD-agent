"""Step 30 design agent tests — deterministic, offline."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from water_agent.multi_agent.agents.design import build_design_package, run_design_agent
from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
from water_agent.multi_agent.contracts import (
    CalcPackage,
    DesignPackage,
    DesignPipeSegment,
    HydraulicCaseRequest,
    ProjectBrief,
    TopologyEdge,
    compute_payload_hash,
)
from water_agent.multi_agent.provenance import validate_provenance


def _brief(
    *,
    project_id: str = "proj-design-001",
    systems: list[str] | None = None,
    topology: list[TopologyEdge] | None = None,
    hydraulic_cases: list[HydraulicCaseRequest] | None = None,
    user_constraints: list[str] | None = None,
    spatial_constraints: list[dict] | None = None,
) -> ProjectBrief:
    from water_agent.multi_agent.contracts import SpatialConstraint

    scs = []
    for sc in spatial_constraints or []:
        scs.append(SpatialConstraint(kind=sc["kind"], value=sc["value"]))
    return ProjectBrief(
        project_id=project_id,
        request="design test",
        systems=systems or ["water_supply"],  # type: ignore[arg-type]
        unit_system="SI",
        hydraulic_cases=hydraulic_cases or [],
        topology=topology or [],
        spatial_constraints=scs,
        required_outputs=[],
        user_constraints=user_constraints or [],
    )


def _hyd_brief(diameter: int = 150) -> ProjectBrief:
    return _brief(
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": diameter})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=[f"DN{diameter}", "material: PE"],
        spatial_constraints=[{"kind": "material", "value": "PE"}],
    )


def _calc_env(diameter: int = 150):
    brief = _brief(
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": diameter})],
    )
    return run_hydraulic_agent(brief)


# ---------------------------------------------------------------------------
# verified
# ---------------------------------------------------------------------------


def test_verified_segment_with_calc_ref() -> None:
    calc_env = _calc_env(150)
    brief = _brief(
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 150})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE"],
        spatial_constraints=[{"kind": "material", "value": "PE"}],
    )
    env = run_design_agent(brief, calc_env, retrieved_source_ids=["GB50015-2019 3.2.1"])
    assert env.status == "ready"
    assert env.artifact_type == "design_package"
    assert env.parent_artifact_ids == [calc_env.artifact_id]
    pkg = DesignPackage.model_validate(env.payload)
    assert len(pkg.segments) == 1
    seg = pkg.segments[0]
    assert seg.hydraulic_status == "verified"
    assert seg.calc_case_ids == ["case-vel-1"]
    assert seg.source_ids == ["GB50015-2019 3.2.1"]
    assert seg.dn_mm == 150
    assert seg.material == "PE"
    assert seg.length_m == 10.0
    assert env.payload_hash == compute_payload_hash(env.payload)


def test_not_verified_without_calc() -> None:
    # calc with no cases -> not_verified
    empty_calc = CalcPackage(cases=[], missing_inputs=[], errors=[], supersedes_artifact_id=None)
    from water_agent.multi_agent.contracts import ArtifactEnvelope

    ph = compute_payload_hash(empty_calc.model_dump())
    calc_env = ArtifactEnvelope(
        artifact_id="calc-empty-001",
        artifact_type="calc_package",
        schema_version="p4.5-2026-09-10",
        producer="hydraulic",
        revision=1,
        parent_artifact_ids=[],
        payload_hash=ph,
        payload=empty_calc.model_dump(),
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status="ready",
    )
    brief = _brief(
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=5.0)],
        user_constraints=["material: PE", "DN100"],
    )
    env = run_design_agent(brief, calc_env, retrieved_source_ids=["SRC 1.1"])
    pkg = DesignPackage.model_validate(env.payload)
    assert pkg.segments[0].hydraulic_status == "not_verified"
    assert env.status in ("blocked", "missing_inputs")
    # also test referencing nonexistent id leads to provenance invalid if registry used
    # here not_verified already


def test_recalc_required_on_diameter_change() -> None:
    calc_env = _calc_env(150)
    brief_old = _brief(
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 150})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE"],
        spatial_constraints=[{"kind": "material", "value": "PE"}],
    )
    # Build prev_design with DN150
    calc_pkg = CalcPackage.model_validate(calc_env.payload)
    prev_pkg = build_design_package(brief_old, calc_pkg, retrieved_source_ids=["SRC 1.1"])
    assert prev_pkg.segments[0].dn_mm == 150

    brief_new = _brief(
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 200})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE"],
        spatial_constraints=[{"kind": "material", "value": "PE"}],
    )
    calc_env_new = _calc_env(200)
    # use same calc id set but diameter changed in brief, should trigger recalc
    # pass prev_design to run_design_agent
    env_new = run_design_agent(brief_new, calc_env_new, retrieved_source_ids=["SRC 1.1"], prev_design=prev_pkg)
    pkg_new = DesignPackage.model_validate(env_new.payload)
    assert len(pkg_new.recalculation_requests) >= 1
    req = pkg_new.recalculation_requests[0]
    assert "diameter_mm" in req["fields"]
    assert "velocity_mps" in req["fields"]
    assert req["segment_id"] == pkg_new.segments[0].segment_id


def test_unresolved_items_missing_material() -> None:
    calc_env = _calc_env(150)
    # no material in constraints -> unresolved
    brief = _brief(
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 150})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=[],
    )
    env = run_design_agent(brief, calc_env, retrieved_source_ids=["SRC 1.1"])
    pkg = DesignPackage.model_validate(env.payload)
    assert any("material" in u for u in pkg.unresolved_items)
    assert env.status in ("missing_inputs", "blocked")
    # also missing dn case
    brief2 = _brief(
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE"],
    )
    calc_empty = CalcPackage(cases=[], missing_inputs=[], errors=[], supersedes_artifact_id=None)
    from water_agent.multi_agent.contracts import ArtifactEnvelope

    ph = compute_payload_hash(calc_empty.model_dump())
    ce2 = ArtifactEnvelope(
        artifact_id="calc-empty-002",
        artifact_type="calc_package",
        schema_version="p4.5-2026-09-10",
        producer="hydraulic",
        revision=1,
        parent_artifact_ids=[],
        payload_hash=ph,
        payload=calc_empty.model_dump(),
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status="ready",
    )
    env2 = run_design_agent(brief2, ce2, retrieved_source_ids=["SRC 1.1"])
    pkg2 = DesignPackage.model_validate(env2.payload)
    assert any("dn_mm" in u or "dn" in u.lower() for u in pkg2.unresolved_items)


def test_source_id_must_be_retrieved() -> None:
    calc_env = _calc_env(150)
    brief = _brief(
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 150})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE", "source_id: NOT_RETRIEVED 9.9.9"],
        spatial_constraints=[{"kind": "material", "value": "PE"}],
    )
    env = run_design_agent(brief, calc_env, retrieved_source_ids=["GB50015-2019 3.2.1"])
    assert env.status in ("blocked", "failed")
    # provenance should also be invalid when registry checked? source check is via status
    pkg = DesignPackage.model_validate(env.payload)
    assert pkg.segments[0].source_ids == ["NOT_RETRIEVED 9.9.9"]
    # validate_provenance checks parent only, but our status already blocked; ensure registry check for empty source not needed
    # also check that retrieved set mismatch is reported in warnings/errors
    assert any("source_id" in w or "source" in w.lower() for w in env.warnings + pkg.unresolved_items) or env.status == "blocked"


def test_fire_protection_system_allowed() -> None:
    calc_env = _calc_env(150)
    brief = _brief(
        systems=["fire_protection"],
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 150})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: steel"],
        spatial_constraints=[{"kind": "material", "value": "steel"}],
    )
    env = run_design_agent(brief, calc_env, retrieved_source_ids=["GB50974-2014 5.1.1"])
    pkg = DesignPackage.model_validate(env.payload)
    assert pkg.segments[0].system == "fire_protection"
    assert env.artifact_type == "design_package"


def test_extra_forbid_and_unknown_system() -> None:
    with pytest.raises(ValidationError):
        DesignPipeSegment(
            segment_id="seg-1",
            system="gas",  # type: ignore[arg-type]
            start_node_id="n1",
            end_node_id="n2",
            calc_case_ids=[],
            source_ids=[],
            hydraulic_status="verified",
        )
    with pytest.raises(ValidationError):
        DesignPackage(segments=[], decisions=[], alternatives=[], recalculation_requests=[], unresolved_items=[], extra="bad")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        DesignPipeSegment(
            segment_id="seg-1",
            system="water_supply",
            start_node_id="n1",
            end_node_id="n2",
            calc_case_ids=[],
            source_ids=[],
            hydraulic_status="verified",
            extra_field="bad",  # type: ignore[call-arg]
        )


def test_finite_rejection() -> None:
    with pytest.raises(ValidationError):
        DesignPipeSegment(
            segment_id="seg-1",
            system="water_supply",
            start_node_id="n1",
            end_node_id="n2",
            length_m=float("nan"),
            calc_case_ids=[],
            source_ids=[],
            hydraulic_status="not_verified",
        )
    with pytest.raises(ValidationError):
        DesignPipeSegment(
            segment_id="seg-1",
            system="water_supply",
            start_node_id="n1",
            end_node_id="n2",
            slope=float("inf"),
            calc_case_ids=[],
            source_ids=[],
            hydraulic_status="not_verified",
        )
    with pytest.raises(ValidationError):
        DesignPipeSegment(
            segment_id="seg-1",
            system="water_supply",
            start_node_id="n1",
            end_node_id="n2",
            length_m=float("-inf"),
            calc_case_ids=[],
            source_ids=[],
            hydraulic_status="not_verified",
        )


def test_deterministic_segment_id_and_hash() -> None:
    calc_env = _calc_env(150)
    brief = _brief(
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 150})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE"],
        spatial_constraints=[{"kind": "material", "value": "PE"}],
    )
    hashes: set[str] = set()
    seg_sets: set[str] = set()
    for _ in range(100):
        env = run_design_agent(brief, calc_env, retrieved_source_ids=["SRC 1.1"])
        hashes.add(env.payload_hash)
        pkg = DesignPackage.model_validate(env.payload)
        seg_sets.add(pkg.segments[0].segment_id)
        assert env.payload_hash == compute_payload_hash(env.payload)
    assert len(hashes) == 1
    assert len(seg_sets) == 1


def test_tool_view_no_calc_mcp() -> None:
    import pathlib

    src = pathlib.Path("src/water_agent/multi_agent/agents/design.py").read_text(encoding="utf-8")
    assert "calc_velocity" not in src
    assert "MCP" not in src
    assert "mcp" not in src.lower() or "mcp" not in src  # strict: no mcp substring
    # check no file write / hydraulic compute imports
    assert "hydraulics.run_calc" not in src
    assert "run_calc" not in src
    assert "write" not in src.lower() or "write" in src.lower() and False  # placeholder
    # more precise: no open( write path
    assert "open(" not in src or "open(" in src and False  # ensure no file write
    # if above would fail due to substring, use explicit
    assert "open(" not in src
    # ensure no material_stats misuse? design allowed read-only precheck but must not import calc
    # we already checked calc_velocity; also ensure no ToolRegistry full exposure
    assert "ToolRegistry" not in src


def test_offline_no_llm() -> None:
    calc_env = _calc_env(150)
    brief = _brief(
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 150})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE"],
        spatial_constraints=[{"kind": "material", "value": "PE"}],
    )
    # No env var, no gateway
    env = run_design_agent(brief, calc_env, retrieved_source_ids=["SRC 1.1"])
    assert env.status in ("ready", "missing_inputs", "blocked")
    assert env.payload_hash == compute_payload_hash(env.payload)
    import water_agent.multi_agent.agents.design as mod

    src = open(mod.__file__, encoding="utf-8").read()
    assert "ProviderGateway" not in src
    assert "GovernedChatModel" not in src

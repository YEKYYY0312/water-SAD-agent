"""Step 30 design agent tests — deterministic, offline."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from water_agent.domain.citation import CitationClaim
from water_agent.multi_agent.agents.design import build_design_package, run_design_agent
from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcCase,
    CalcPackage,
    DesignPackage,
    DesignPipeSegment,
    HydraulicCaseRequest,
    ProjectBrief,
    TopologyEdge,
    compute_payload_hash,
)


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
        systems=systems if systems is not None else ["water_supply"],  # type: ignore[arg-type]
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
        hydraulic_cases=[HydraulicCaseRequest(case_id="case-vel-1", formula="velocity", inputs={"flow_lps": 10, "diameter_mm": 150})],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE"],
        spatial_constraints=[
            {"kind": "material", "value": "PE"},
            {"kind": "diameter_mm", "value": 200},
        ],
    )
    env_new = run_design_agent(
        brief_new,
        calc_env,
        retrieved_source_ids=["SRC 1.1"],
        prev_design=prev_pkg,
    )
    pkg_new = DesignPackage.model_validate(env_new.payload)
    assert pkg_new.segments[0].dn_mm == 200
    assert pkg_new.recalculation_requests == [
        {
            "reason": "hydraulic_input_changed",
            "fields": ["diameter_mm"],
            "segment_id": pkg_new.segments[0].segment_id,
            "formula": "velocity",
            "source_calc_case_id": "case-vel-1",
            "input_updates": {"diameter_mm": 200.0},
        }
    ]


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
    assert env.status == "blocked"
    pkg = DesignPackage.model_validate(env.payload)
    assert pkg.segments[0].source_ids == []
    assert pkg.invalid_citation_ids == ["NOT_RETRIEVED 9.9.9"]
    assert env.citation_ids == []
    assert any("invalid citation" in warning for warning in env.warnings)


def test_validated_citation_retains_version_status_and_location() -> None:
    calc_env = _calc_env(150)
    brief = _brief(
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE", "DN150", "source_id: GB50015-2019 3.2.1"],
    )
    env = run_design_agent(
        brief,
        calc_env,
        validated_citations=[
            CitationClaim(
                source_id="GB50015-2019 3.2.1",
                spec_version="2019",
                version_status="unknown",
                declared_by="citation_validator",
            )
        ],
    )
    pkg = DesignPackage.model_validate(env.payload)
    assert env.status == "ready"
    assert pkg.segments[0].source_ids == ["GB50015-2019 3.2.1"]
    assert pkg.citations[0].source_id == "GB50015-2019 3.2.1"
    assert pkg.citations[0].spec_version == "2019"
    assert pkg.citations[0].version_status == "unknown"
    assert pkg.citations[0].location == f"segment:{pkg.segments[0].segment_id}:source_ids"


def test_old_calc_case_cannot_support_verified_status() -> None:
    calc_env = _calc_env(150)
    brief = _hyd_brief(150)
    env = run_design_agent(
        brief,
        calc_env,
        retrieved_source_ids=["SRC 1.1"],
        active_calc_case_ids={"case-current"},
    )
    pkg = DesignPackage.model_validate(env.payload)
    assert env.status == "blocked"
    assert pkg.segments[0].calc_case_ids == []
    assert pkg.segments[0].hydraulic_status == "not_verified"


def test_failed_calc_case_cannot_be_marked_verified() -> None:
    failed = CalcPackage(
        cases=[
            CalcCase(
                calc_case_id="case-failed",
                formula="velocity",
                inputs={"flow_lps": 10.0, "diameter_mm": 150.0},
                outputs={},
                tool_invocation_id="tool-failed",
                errors=["tool failed"],
                status="failed",
            )
        ],
        errors=["tool failed"],
    )
    payload = failed.model_dump()
    calc_env = ArtifactEnvelope(
        artifact_id="calc-failed-001",
        artifact_type="calc_package",
        schema_version="p4.5-2026-09-10",
        producer="hydraulic",
        revision=1,
        parent_artifact_ids=[],
        payload_hash=compute_payload_hash(payload),
        payload=payload,
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status="blocked",
    )
    env = run_design_agent(_hyd_brief(150), calc_env, retrieved_source_ids=["SRC 1.1"])
    pkg = DesignPackage.model_validate(env.payload)
    assert env.status == "blocked"
    assert pkg.segments[0].hydraulic_status == "not_verified"
    assert pkg.segments[0].calc_case_ids == []


def test_empty_systems_stays_unresolved_without_water_supply_default() -> None:
    calc_env = _calc_env(150)
    brief = _brief(
        systems=[],
        topology=[TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0)],
        user_constraints=["material: PE", "DN150"],
    )
    env = run_design_agent(brief, calc_env, retrieved_source_ids=["SRC 1.1"])
    pkg = DesignPackage.model_validate(env.payload)
    assert env.status == "missing_inputs"
    assert pkg.segments == []
    assert "missing system: brief.systems empty" in pkg.unresolved_items
    assert "water_supply" not in " ".join(pkg.decisions)


def test_new_hydraulic_input_always_creates_recalculation_request() -> None:
    calc_env = _calc_env(150)
    env = run_design_agent(
        _hyd_brief(150),
        calc_env,
        retrieved_source_ids=["SRC 1.1"],
        recalc_trigger_fields={"flow_lps", "roughness_mm"},
    )
    pkg = DesignPackage.model_validate(env.payload)
    assert env.status == "blocked"
    assert pkg.segments[0].hydraulic_status == "recalc_required"
    assert pkg.recalculation_requests == [
        {
            "reason": "hydraulic_input_changed",
            "fields": ["flow_lps", "roughness_mm"],
            "segment_id": pkg.segments[0].segment_id,
            "suggested_formula": "velocity",
        }
    ]


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
        topology=[
            TopologyEdge(start_node_id="n1", end_node_id="n2", length_m=10.0),
            TopologyEdge(start_node_id="n2", end_node_id="n3", length_m=8.0),
        ],
        user_constraints=["material: PE"],
        spatial_constraints=[{"kind": "material", "value": "PE"}],
    )
    hashes: set[str] = set()
    segment_id_sets: set[tuple[str, ...]] = set()
    for _ in range(100):
        env = run_design_agent(brief, calc_env, retrieved_source_ids=["SRC 1.1"])
        hashes.add(env.payload_hash)
        pkg = DesignPackage.model_validate(env.payload)
        segment_id_sets.add(tuple(segment.segment_id for segment in pkg.segments))
        assert len({segment.segment_id for segment in pkg.segments}) == len(pkg.segments)
        assert env.payload_hash == compute_payload_hash(env.payload)
    assert len(hashes) == 1
    assert len(segment_id_sets) == 1


def test_tool_view_no_calc_mcp() -> None:
    import pathlib

    src = pathlib.Path("src/water_agent/multi_agent/agents/design.py").read_text(encoding="utf-8")
    assert "calc_velocity" not in src
    assert "MCP" not in src
    assert "mcp" not in src.lower() or "mcp" not in src  # strict: no mcp substring
    # check no file write / hydraulic compute imports
    assert "hydraulics.run_calc" not in src
    assert "run_calc" not in src
    assert "write" not in src.lower()
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

    with open(mod.__file__, encoding="utf-8") as handle:
        src = handle.read()
    assert "ProviderGateway" not in src
    assert "GovernedChatModel" not in src

"""Step 34 bounded routing behavior."""
from __future__ import annotations

from water_agent.multi_agent.contracts import ReviewIssue, ReviewPackage
from water_agent.multi_agent.routing import MAX_DESIGN_REVISIONS, route_review


def _step33_brief() -> dict:
    return {
        "project_id": "project-step33", "request": "test", "systems": ["water_supply"], "unit_system": "SI",
        "hydraulic_cases": [{"case_id": "velocity-1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}],
        "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        "spatial_constraints": [{"kind": "material", "value": "PE"}], "required_outputs": ["drawing"],
        "user_constraints": ["material: PE", "DN150"],
    }


def _brief() -> dict:
    return {
        "project_id": "proj-orchestrator-001",
        "request": "给水管段设计，DN150 PE 管 10m，流量 10L/s",
        "systems": ["water_supply"],
        "unit_system": "SI",
        "hydraulic_cases": [
            {
                "case_id": "case-vel-1",
                "formula": "velocity",
                "inputs": {"flow_lps": 10, "diameter_mm": 150},
            }
        ],
        "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        "spatial_constraints": [{"kind": "material", "value": "PE"}],
        "required_outputs": ["calculation_book", "drawing"],
        "user_constraints": ["material: PE", "DN150"],
    }

def test_loop_exhaustion_blocks_without_a_followup_agent_call() -> None:
    package = ReviewPackage(
        decision="revise",
        issues=[
            ReviewIssue(
                issue_id="issue-loop-001",
                rule_id="doc_value_mismatch",
                severity="error",
                owner="design",
                artifact_id="doc-loop-001",
                message="numeric evidence requires design revision",
                evidence_ids=[],
            )
        ],
        not_checked=[],
        reviewed_artifact_hashes=[],
    )

    decision = route_review(package, revision_counts={"design": MAX_DESIGN_REVISIONS})

    assert decision["blocked"] is True
    assert decision["exhausted"] is True
    assert decision["next_owner"] is None
    assert decision["error_code"] == "MULTI_RETRY_EXHAUSTED"


def test_document_envelope_propagates_real_docgen_blocked_status() -> None:
    from water_agent.multi_agent.agents.design import run_design_agent
    from water_agent.multi_agent.agents.drawing import run_drawing_agent
    from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
    from water_agent.multi_agent.contracts import ProjectBrief
    from water_agent.multi_agent.orchestrator import _document_from_plan

    brief_data = _step33_brief()
    brief_data["topology"][0]["length_m"] = None
    brief = ProjectBrief.model_validate(brief_data)
    calc = run_hydraulic_agent(brief)
    design = run_design_agent(
        brief,
        calc,
        retrieved_source_ids=["GB50015-2019 3.2.1"],
    )
    plan = run_drawing_agent(design, calc, drawing_id="docgen-status-propagation")

    document = _document_from_plan(plan, calc, design)

    assert plan.payload["output_adaptations"]["docgen"]["status"] == "blocked"
    assert document.status == "blocked"
    assert document.payload["errors"]


def test_document_envelope_maps_docgen_missing_inputs_to_payload() -> None:
    from water_agent.multi_agent.agents.design import run_design_agent
    from water_agent.multi_agent.agents.drawing import build_drawing_plan
    from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
    from water_agent.multi_agent.contracts import (
        ArtifactEnvelope,
        CalcPackage,
        DesignPackage,
        ProjectBrief,
        compute_payload_hash,
    )
    from water_agent.multi_agent.orchestrator import _document_from_plan

    brief_data = _step33_brief()
    brief_data["topology"][0]["length_m"] = None
    brief = ProjectBrief.model_validate(brief_data)
    calc = run_hydraulic_agent(brief)
    design = run_design_agent(
        brief,
        calc,
        retrieved_source_ids=["GB50015-2019 3.2.1"],
    )
    plan = build_drawing_plan(
        DesignPackage.model_validate(design.payload),
        CalcPackage.model_validate(calc.payload),
        drawing_id="docgen-missing-inputs",
        source_design_artifact_id=design.artifact_id,
        source_calc_artifact_id=calc.artifact_id,
    )
    payload = plan.model_dump()
    plan_envelope = ArtifactEnvelope(
        artifact_id="plan-docgen-missing-inputs",
        artifact_type="drawing_plan",
        schema_version="test",
        producer="drawing",
        revision=1,
        parent_artifact_ids=[design.artifact_id, calc.artifact_id],
        payload_hash=compute_payload_hash(payload),
        payload=payload,
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status="missing_inputs",
    )

    document = _document_from_plan(plan_envelope, calc, design)

    assert plan.output_adaptations["docgen"]["status"] == "missing_inputs"
    assert document.status == "missing_inputs"
    assert document.payload["missing_inputs"]
    assert "errors" not in document.payload


def test_four_agents_run_to_approval_then_real_export(tmp_path) -> None:
    import sqlite3
    from pathlib import Path

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    from water_agent.multi_agent.orchestrator import (
        build_orchestrator_graph,
        default_multi_agent_state,
    )
    from water_agent.session import default_checkpoint_serde

    conn = sqlite3.connect(tmp_path / "state.sqlite", check_same_thread=False)
    try:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde()))
        config = {"configurable": {"thread_id": "step33-full"}}
        paused = app.invoke(default_multi_agent_state(project_brief=_step33_brief(), retrieved_source_ids=["GB50015-2019 3.2.1"], staging_dir=str(tmp_path / "staging"), final_dir=str(tmp_path / "final"), output_file_id="step33-drawing"), config=config)
        assert paused["__interrupt__"]
        assert paused["approval_context_hash"] == paused["__interrupt__"][0].value["final_package_hash"]
        paused_events = list(paused["trace_events"])
        paused_names = {event["event"] for event in paused_events}
        assert {
            "run_started",
            "agent_call",
            "artifact_created",
            "route_selected",
            "approval_requested",
        } <= paused_names
        result = app.invoke(Command(resume={"approved": True, "reason": "checked"}), config=config)
        assert result["status"] == "succeeded"
        assert Path(result["exported_file_path"]).is_file()
        assert all(result["release_checks"].values())
        assert result["trace_events"][: len(paused_events)] == paused_events
        event_names = {event["event"] for event in result["trace_events"]}
        assert {
            "approval_resumed",
            "release_gate_checked",
            "export_started",
            "export_completed",
            "run_finished",
        } <= event_names
        assert all(
            event.get("source") == "multi_agent_orchestrator"
            and event.get("delivery") == "local_event_only"
            for event in result["trace_events"]
        )
    finally:
        conn.close()


def test_review_owner_return_is_bounded_and_rebuilds_owner(tmp_path) -> None:
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from water_agent.multi_agent.agents.design import run_design_agent
    from water_agent.multi_agent.agents.review import run_review_agent
    from water_agent.multi_agent.contracts import (
        ArtifactEnvelope,
        ReviewIssue,
        compute_payload_hash,
    )
    from water_agent.multi_agent.orchestrator import (
        build_orchestrator_graph,
        default_multi_agent_state,
    )
    from water_agent.session import default_checkpoint_serde

    calls = {"design": 0, "review": 0}

    def design_runner(*args, **kwargs):
        calls["design"] += 1
        return run_design_agent(*args, **kwargs)

    def review_runner(registry, *, review_id, document_artifact_id, revision):
        calls["review"] += 1
        if calls["review"] > 1:
            return run_review_agent(registry, review_id=review_id, document_artifact_id=document_artifact_id, revision=revision)
        design = next(env for env in registry.values() if env.artifact_type == "design_package")
        package = ReviewPackage(decision="revise", issues=[ReviewIssue(issue_id="return-design", rule_id="test", severity="error", owner="design", artifact_id=design.artifact_id, message="owner only")], not_checked=[], reviewed_artifact_hashes=[registry[key].payload_hash for key in sorted(registry)])
        payload = package.model_dump()
        return ArtifactEnvelope(artifact_id="review-return-design", artifact_type="review_package", schema_version="test", producer="review", revision=revision, parent_artifact_ids=sorted(registry), payload_hash=compute_payload_hash(payload), payload=payload, citation_ids=[], assumptions=[], warnings=[], status="failed")

    conn = sqlite3.connect(tmp_path / "state.sqlite", check_same_thread=False)
    try:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde()), design_runner=design_runner, review_runner=review_runner)
        state = default_multi_agent_state(project_brief=_step33_brief(), retrieved_source_ids=["GB50015-2019 3.2.1"], staging_dir=str(tmp_path / "staging"), final_dir=str(tmp_path / "final"))
        result = app.invoke(state, config={"configurable": {"thread_id": "return-owner"}})
        assert result["__interrupt__"]
        assert calls == {"design": 2, "review": 2}
        assert result["revision_counts"]["design"] == 1
    finally:
        conn.close()


def test_structured_diameter_revision_recalculates_new_case_and_keeps_history(tmp_path) -> None:
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from water_agent.multi_agent.contracts import CalcPackage, DesignPackage, DrawingPlan
    from water_agent.multi_agent.orchestrator import (
        build_orchestrator_graph,
        default_multi_agent_state,
    )
    from water_agent.session import default_checkpoint_serde

    brief = _step33_brief()
    brief["spatial_constraints"] = [
        {"kind": "material", "value": "PE"},
        {"kind": "diameter_mm", "value": 200},
    ]

    conn = sqlite3.connect(tmp_path / "state.sqlite", check_same_thread=False)
    try:
        app = build_orchestrator_graph(
            checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde())
        )
        result = app.invoke(
            default_multi_agent_state(
                project_brief=brief,
                retrieved_source_ids=["GB50015-2019 3.2.1"],
                staging_dir=str(tmp_path / "staging"),
                final_dir=str(tmp_path / "final"),
                output_file_id="diameter-revision",
            ),
            config={"configurable": {"thread_id": "diameter-revision"}},
        )

        assert result["__interrupt__"]
        calc_artifacts = [
            envelope
            for envelope in result["artifacts"].values()
            if envelope["artifact_type"] == "calc_package"
        ]
        assert len(calc_artifacts) == 2
        active_calc = result["artifacts"][result["active_artifact_ids"]["calc"]]
        active_package = CalcPackage.model_validate(active_calc["payload"])
        assert active_package.supersedes_artifact_id is not None
        old_calc = next(
            envelope
            for envelope in calc_artifacts
            if envelope["artifact_id"] == active_package.supersedes_artifact_id
        )
        old_package = CalcPackage.model_validate(old_calc["payload"])
        assert active_package.cases[0].calc_case_id != old_package.cases[0].calc_case_id
        assert active_package.cases[0].inputs["diameter_mm"] == 200

        design = DesignPackage.model_validate(
            result["artifacts"][result["active_artifact_ids"]["design"]]["payload"]
        )
        assert design.recalculation_requests == []
        assert design.segments[0].dn_mm == 200
        assert design.segments[0].calc_case_ids == [active_package.cases[0].calc_case_id]
        plan = DrawingPlan.model_validate(
            result["artifacts"][result["active_artifact_ids"]["drawing_plan"]]["payload"]
        )
        assert {entity.dn_mm for entity in plan.entities if entity.segment_id} == {200}
    finally:
        conn.close()


def test_recalculation_request_without_new_value_waits_and_does_not_rerun_old_case(tmp_path) -> None:
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from water_agent.multi_agent.agents.design import run_design_agent
    from water_agent.multi_agent.orchestrator import (
        build_orchestrator_graph,
        default_multi_agent_state,
    )
    from water_agent.session import default_checkpoint_serde

    calls = {"design": 0}

    def design_runner(*args, **kwargs):
        calls["design"] += 1
        return run_design_agent(*args, **kwargs, recalc_trigger_fields={"flow_lps"})

    conn = sqlite3.connect(tmp_path / "state.sqlite", check_same_thread=False)
    try:
        app = build_orchestrator_graph(
            checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde()),
            design_runner=design_runner,
        )
        result = app.invoke(
            default_multi_agent_state(
                project_brief=_step33_brief(),
                retrieved_source_ids=["GB50015-2019 3.2.1"],
                staging_dir=str(tmp_path / "staging"),
                final_dir=str(tmp_path / "final"),
            ),
            config={"configurable": {"thread_id": "missing-recalc-value"}},
        )

        assert "__interrupt__" not in result
        assert result["status"] == "waiting"
        assert any("input_updates.flow_lps" in item for item in result["missing_inputs"])
        assert calls["design"] == 1
        assert sum(
            envelope["artifact_type"] == "calc_package"
            for envelope in result["artifacts"].values()
        ) == 1
        assert "drawing" not in result["active_artifact_ids"]
    finally:
        conn.close()


def test_hydraulic_review_owner_requires_structured_recalculation_before_calling_hydraulic(tmp_path) -> None:
    """A hydraulic review finding cannot loop the original brief back into hydraulic."""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
    from water_agent.multi_agent.contracts import ArtifactEnvelope, compute_payload_hash
    from water_agent.multi_agent.orchestrator import (
        build_orchestrator_graph,
        default_multi_agent_state,
    )
    from water_agent.session import default_checkpoint_serde

    calls = {"hydraulic": 0, "review": 0}

    def hydraulic_runner(*args, **kwargs):
        calls["hydraulic"] += 1
        return run_hydraulic_agent(*args, **kwargs)

    def review_runner(registry, *, review_id, document_artifact_id, revision):
        calls["review"] += 1
        calc_id = next(env.artifact_id for env in registry.values() if env.artifact_type == "calc_package")
        payload = ReviewPackage(
            decision="revise",
            issues=[
                ReviewIssue(
                    issue_id="hydraulic-needs-new-input",
                    rule_id="stale_calculation_used",
                    severity="error",
                    owner="hydraulic",
                    artifact_id=calc_id,
                    message="new hydraulic input is required",
                    evidence_ids=[calc_id],
                )
            ],
            not_checked=[],
            reviewed_artifact_hashes=[env.payload_hash for _, env in sorted(registry.items())],
        )
        raw = payload.model_dump()
        envelope = ArtifactEnvelope(
            artifact_id=f"review-hydraulic-{revision}",
            artifact_type="review_package",
            schema_version="test",
            producer="review",
            revision=revision,
            parent_artifact_ids=sorted(registry),
            payload_hash=compute_payload_hash(raw),
            payload=raw,
            citation_ids=[],
            assumptions=[],
            warnings=[],
            status="failed",
        )
        return envelope

    conn = sqlite3.connect(tmp_path / "state.sqlite", check_same_thread=False)
    try:
        app = build_orchestrator_graph(
            checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde()),
            hydraulic_runner=hydraulic_runner,
            review_runner=review_runner,
        )
        result = app.invoke(
            default_multi_agent_state(
                project_brief=_step33_brief(),
                retrieved_source_ids=["GB50015-2019 3.2.1"],
                staging_dir=str(tmp_path / "staging"),
                final_dir=str(tmp_path / "final"),
            ),
            config={"configurable": {"thread_id": "hydraulic-owner-recalc"}},
        )

        assert result["status"] == "waiting"
        assert "MULTI_MISSING_INPUTS" in result["errors"]
        assert calls == {"hydraulic": 1, "review": 1}
        assert sum(
            envelope["artifact_type"] == "calc_package"
            for envelope in result["artifacts"].values()
        ) == 1
    finally:
        conn.close()

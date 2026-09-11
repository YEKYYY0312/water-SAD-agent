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
        result = app.invoke(Command(resume={"approved": True, "reason": "checked"}), config=config)
        assert result["status"] == "succeeded"
        assert Path(result["exported_file_path"]).is_file()
        assert all(result["release_checks"].values())
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

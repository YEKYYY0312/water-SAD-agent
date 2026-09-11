"""Step 34 approval binding behavior used by the multi-agent HITL gate."""
from __future__ import annotations

from water_agent.multi_agent.contracts import ApprovalRecord, ArtifactEnvelope, compute_payload_hash
from water_agent.multi_agent.orchestrator import is_approval_valid


def _step33_brief() -> dict:
    return {
        "project_id": "project-hitl", "request": "test", "systems": ["water_supply"], "unit_system": "SI",
        "hydraulic_cases": [{"case_id": "velocity-1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}],
        "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        "spatial_constraints": [{"kind": "material", "value": "PE"}], "required_outputs": ["drawing"],
        "user_constraints": ["material: PE", "DN150"],
    }


def test_approval_hash_binding_invalidates_after_artifact_change() -> None:
    payload = {"doc_type": "calculation_book", "calc_values": {}, "sections": [], "citation_ids": []}
    artifact = ArtifactEnvelope(
        artifact_id="doc-hitl-001",
        artifact_type="document",
        schema_version="p4.5-2026-09-10",
        producer="orchestrator",
        revision=1,
        parent_artifact_ids=[],
        payload_hash=compute_payload_hash(payload),
        payload=payload,
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status="ready",
    )
    approval = ApprovalRecord(
        approved=True,
        reason="human approved reviewed package",
        artifact_ids=[artifact.artifact_id],
        artifact_hashes=[artifact.payload_hash],
        approved_at="2026-09-11T00:00:00Z",
    )

    assert is_approval_valid(approval, [artifact.payload_hash])
    assert not is_approval_valid(approval, ["f" * 64])


def test_sqlite_resume_revalidates_hash_and_rejects_drift(tmp_path) -> None:
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

    from water_agent.multi_agent.orchestrator import (
        build_orchestrator_graph,
        default_multi_agent_state,
    )
    from water_agent.session import default_checkpoint_serde

    db = tmp_path / "state.sqlite"
    config = {"configurable": {"thread_id": "resume-drift"}}
    conn1 = sqlite3.connect(db, check_same_thread=False)
    app1 = build_orchestrator_graph(checkpointer=SqliteSaver(conn1, serde=default_checkpoint_serde()))
    paused = app1.invoke(default_multi_agent_state(project_brief=_step33_brief(), retrieved_source_ids=["GB50015-2019 3.2.1"], staging_dir=str(tmp_path / "staging"), final_dir=str(tmp_path / "final")), config=config)
    assert paused["__interrupt__"]
    document_id = paused["active_artifact_ids"]["document"]
    document = ArtifactEnvelope.model_validate(paused["artifacts"][document_id])
    altered_payload = {**document.payload, "sections": [{"heading": "changed", "lines": ["changed"]}]}
    altered = document.model_copy(update={"payload": altered_payload, "payload_hash": compute_payload_hash(altered_payload)})
    app1.update_state(config, {"artifacts": {**paused["artifacts"], document_id: altered.model_dump(mode="json")}})
    conn1.close()

    conn2 = sqlite3.connect(db, check_same_thread=False)
    try:
        app2 = build_orchestrator_graph(checkpointer=SqliteSaver(conn2, serde=default_checkpoint_serde()))
        result = app2.invoke(Command(resume={"approved": True}), config=config)
        assert result["status"] == "blocked"
        assert "MULTI_APPROVAL_STALE" in result["errors"]
    finally:
        conn2.close()

"""Step 33 release gate and narrow export failure semantics."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from water_agent.multi_agent.export_adapter import assemble_export_path
from water_agent.multi_agent.orchestrator import build_orchestrator_graph, default_multi_agent_state
from water_agent.session import default_checkpoint_serde


def _brief() -> dict:
    return {
        "project_id": "project-export", "request": "test", "systems": ["water_supply"], "unit_system": "SI",
        "hydraulic_cases": [{"case_id": "velocity-1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}],
        "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        "spatial_constraints": [{"kind": "material", "value": "PE"}], "required_outputs": ["drawing"],
        "user_constraints": ["material: PE", "DN150"],
    }


def _state(tmp_path):
    return default_multi_agent_state(project_brief=_brief(), retrieved_source_ids=["GB50015-2019 3.2.1"], staging_dir=str(tmp_path / "staging"), final_dir=str(tmp_path / "final"), output_file_id="export-drawing")


def test_unapproved_adapter_export_is_rejected(tmp_path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "safe-id.dxf").write_text("dxf", encoding="utf-8")
    with pytest.raises(ValueError, match="MULTI_APPROVAL_REQUIRED"):
        assemble_export_path(file_id="safe-id", staging_dir=str(staging), final_dir=str(tmp_path / "final"), approval=None, artifacts=None)


def test_export_adapter_failure_is_failed_not_succeeded(tmp_path) -> None:
    def broken_exporter(**_kwargs):
        raise OSError("disk full")

    conn = sqlite3.connect(tmp_path / "state.sqlite", check_same_thread=False)
    try:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde()), exporter=broken_exporter)
        config = {"configurable": {"thread_id": "export-failure"}}
        assert app.invoke(_state(tmp_path), config=config)["__interrupt__"]
        result = app.invoke(Command(resume={"approved": True}), config=config)
        assert result["status"] == "failed"
        assert any(error.startswith("MULTI_EXPORT_FAILED") for error in result["errors"])
        assert result["exported_file_path"] is None
    finally:
        conn.close()


def test_successful_export_requires_all_release_checks(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "state.sqlite", check_same_thread=False)
    try:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde()))
        config = {"configurable": {"thread_id": "release-checks"}}
        assert app.invoke(_state(tmp_path), config=config)["__interrupt__"]
        result = app.invoke(Command(resume={"approved": True}), config=config)
        assert result["status"] == "succeeded"
        assert result["release_checks"] == {
            "approval_exists": True, "approval_not_expired": True, "package_hash_matches": True,
            "no_stale_artifacts": True, "review_passed": True, "no_unresolved_blocking_findings": True,
        }
        assert Path(result["exported_file_path"]).is_file()
    finally:
        conn.close()

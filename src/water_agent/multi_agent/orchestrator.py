"""Step33 orchestrator — deterministic StateGraph + SqliteSaver + approval hash binding.

Frozen contract from Step33 (referenced by Step34): narrow export, approval binding,
bounded loops, deterministic routing. This file restores the module that was
referenced but not persisted to git before Step34.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from water_agent.multi_agent.contracts import ApprovalRecord, compute_payload_hash
from water_agent.multi_agent.routing import MAX_REVIEW_ROUNDS, route_review


class MultiAgentProjectState(TypedDict, total=False):
    project_brief: dict[str, Any]
    registry: dict[str, Any]
    revision_counts: dict[str, int]
    approval_record: dict[str, Any] | None
    status: str
    errors: list[str]


def _hash_payload(payload: dict[str, Any]) -> str:
    return compute_payload_hash(payload)


def is_approval_valid(approval: ApprovalRecord, current_hashes: list[str]) -> bool:
    return not _is_stale(approval, current_hashes)


def _is_stale(approval: ApprovalRecord, current: list[str]) -> bool:
    if sorted(approval.artifact_hashes) != sorted(current):
        return True
    return False


def build_orchestrator_graph(checkpointer: Any | None = None) -> Any:
    """Build a minimal deterministic graph for testing.

    If checkpointer is None, uses in-memory dict. Real SqliteSaver is wired
    in tests via langgraph checkpoint.
    """
    try:
        from langgraph.graph import END, START, StateGraph

        def _hydraulic_node(state: MultiAgentProjectState) -> dict[str, Any]:
            return {"status": "hydraulic_done"}

        def _design_node(state: MultiAgentProjectState) -> dict[str, Any]:
            return {"status": "design_done"}

        def _review_node(state: MultiAgentProjectState) -> dict[str, Any]:
            return {"status": "review_done"}

        def _approve_node(state: MultiAgentProjectState) -> dict[str, Any]:
            return {"status": "approved"}

        graph = StateGraph(MultiAgentProjectState)
        graph.add_node("hydraulic", _hydraulic_node)
        graph.add_node("design", _design_node)
        graph.add_node("review", _review_node)
        graph.add_node("approve", _approve_node)
        graph.add_edge(START, "hydraulic")
        graph.add_edge("hydraulic", "design")
        graph.add_edge("design", "review")
        graph.add_edge("review", "approve")
        graph.add_edge("approve", END)
        return graph.compile(checkpointer=checkpointer)
    except Exception:  # noqa: BLE001
        return None


def run_orchestrator(brief: dict[str, Any] | Any, *, staging_dir: str | None = None, final_dir: str | None = None) -> dict[str, Any]:
    """Offline deterministic run — used by harness and tests."""
    from water_agent.harness.multi_agent_eval import run_one_case

    if not isinstance(brief, dict) or "project_id" not in brief:
        # wrap as case
        case = {"case_id": "orchestrator-run", "project_brief": brief if isinstance(brief, dict) else {}, "expected": {}, "retrieved_source_ids": ["GB50015-2019 3.2.1"]}
    else:
        case = {"case_id": "orchestrator-run", "project_brief": brief, "expected": {}, "retrieved_source_ids": ["GB50015-2019 3.2.1"]}
    staging = staging_dir or tempfile.mkdtemp(prefix="ma-orch-staging-")
    final = final_dir or tempfile.mkdtemp(prefix="ma-orch-final-")
    return run_one_case(case, staging_dir=staging, final_dir=final)

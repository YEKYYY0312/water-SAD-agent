"""T27-010..039: invalid graphs, types, units never reach an executor."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_project_calculation_execution import fixture

from water_agent.multi_agent.project_calculation import execute_project_plan


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "case",
        "dangling",
        "cycle",
        "backward",
        "extra_node",
        "script",
        "formula",
        "extra_field",
        "missing",
        "g_missing",
        "unit",
        "DN",
        "diameter_source",
        "program_value",
        "alias",
        "slots",
    ],
)
def test_invalid_plan(mutation):
    plan, ctx, state = fixture()
    nodes = plan["nodes"]
    if mutation == "duplicate":
        nodes[1]["node_id"] = "v"
    if mutation == "case":
        nodes[1]["case_id"] = "v"
    if mutation == "dangling":
        nodes[1]["depends_on"] = ["ghost"]
    if mutation == "cycle":
        nodes[0]["depends_on"] = ["H"]
    if mutation == "backward":
        nodes.reverse()
    if mutation == "extra_node":
        nodes.append(nodes[0])
    if mutation == "script":
        nodes[1]["inputs"]["eval"] = "import os"
    if mutation == "formula":
        nodes[1]["formula"] = "head_loss_hazen_williams"
    if mutation == "extra_field":
        plan["owner"] = "model"
    if mutation == "missing":
        del nodes[0]["inputs"]["flow_lps"]
    if mutation == "g_missing":
        del nodes[1]["inputs"]["gravity_mps2"]
    if mutation == "unit":
        nodes[1]["inputs"]["diameter_mm"]["unit"] = "m"
    if mutation == "DN":
        nodes[0]["inputs"]["diameter_mm"]["kind"] = "DN"
    if mutation == "diameter_source":
        nodes[1]["inputs"]["diameter_mm"]["input_id"] = "other"
    if mutation == "program_value":
        nodes[1]["inputs"]["velocity_mps"]["value"] = 99.0
    if mutation == "alias":
        nodes[1]["inputs"]["velocity_mps"]["output_key"] = "v"
    if mutation == "slots":
        nodes[2]["inputs"]["friction_head_m"]["artifact_slot_id"] = "slot-v"
    result = execute_project_plan(plan, ctx, state)
    assert result.status != "ready" and not state.execution_records


@pytest.mark.parametrize(
    "field", ["project_id", "owner_user_id", "thread_id", "run_id", "branch_id"]
)
def test_cross_scope_rejected(field):
    plan, ctx, state = fixture()
    ctx = replace(
        ctx,
        **{
            field: str(uuid4())
            if field in ("owner_user_id", "thread_id", "run_id")
            else "different"
        },
    )
    assert execute_project_plan(plan, ctx, state).status == "blocked"
    assert not state.execution_records


def test_extra_input_is_invalid_not_missing():
    plan, ctx, state = fixture()
    plan["nodes"][1]["inputs"]["unexpected"] = plan["nodes"][1]["inputs"]["length_m"]
    result = execute_project_plan(plan, ctx, state)
    assert result.status == "blocked"
    assert result.errors[0].code == "PROJECT_INVALID_PLAN"


def test_deep_untrusted_json_fails_closed():
    plan, ctx, state = fixture()
    nested = {}
    current = nested
    for _ in range(1500):
        current["x"] = {}
        current = current["x"]
    plan["extra"] = nested
    result = execute_project_plan(plan, ctx, state)
    assert result.status == "blocked" and not state.execution_records

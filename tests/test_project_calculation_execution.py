"""T2-7B offline governed chain; synthetic data only."""

from uuid import uuid4

import pytest

from water_agent.multi_agent.project_calculation import ProjectState, execute_project_plan
from water_agent.multi_agent.project_calculation_contracts import ProjectExecutionContext
from water_agent.runtime.governance.contracts import Principal


def fixture():
    owner, thread, run, inputs = (str(uuid4()) for _ in range(4))
    ctx = ProjectExecutionContext(
        owner_user_id=owner,
        thread_id=thread,
        run_id=run,
        project_id="synthetic",
        plan_id="chain",
        branch_id="main",
        plan_revision=1,
        generation=1,
        authorization_epoch=1,
        checkpoint_namespace="offline",
        principal=Principal.local("offline"),
        cancel_check=lambda: False,
    )
    values = {
        "Q": (10.0, "L/s"),
        "d": (100.0, "mm"),
        "L": (100.0, "m"),
        "f": (0.02, "1"),
        "g": (9.81, "m/s²"),
        "static": (10.0, "m"),
        "local": (0.0, "m"),
        "residual": (2.0, "m"),
    }

    def user(k):
        v, unit = values[k]
        return {"kind": "user", "input_id": k, "input_set_id": inputs, "value": v, "unit": unit}

    def upstream(node, key, unit):
        return {
            "kind": "planned_output",
            "node_id": node,
            "case_id": node,
            "artifact_slot_id": "slot-" + node,
            "output_key": key,
            "unit": unit,
        }

    nodes = [
        {
            "node_id": "v",
            "case_id": "v",
            "formula": "velocity",
            "depends_on": [],
            "inputs": {"flow_lps": user("Q"), "diameter_mm": user("d")},
            "requested_outputs": ["velocity_mps"],
        },
        {
            "node_id": "hf",
            "case_id": "hf",
            "formula": "head_loss_darcy_weisbach",
            "depends_on": ["v"],
            "inputs": {
                "length_m": user("L"),
                "diameter_mm": user("d"),
                "friction_factor": user("f"),
                "gravity_mps2": user("g"),
                "velocity_mps": upstream("v", "velocity_mps", "m/s"),
            },
            "requested_outputs": ["head_loss_m"],
        },
        {
            "node_id": "H",
            "case_id": "H",
            "formula": "pump_head",
            "depends_on": ["hf"],
            "inputs": {
                "static_head_m": user("static"),
                "local_head_m": user("local"),
                "residual_head_m": user("residual"),
                "friction_head_m": upstream("hf", "head_loss_m", "m"),
            },
            "requested_outputs": ["pump_head_m"],
        },
    ]
    plan = {
        "contract_version": "t27-v1",
        "project_id": "synthetic",
        "plan_id": "chain",
        "branch_id": "main",
        "system_id": "ws",
        "segment_id": "pipe",
        "revision": 1,
        "base_revision": 0,
        "supersedes_plan_hash": None,
        "system": "water_supply",
        "input_set_id": inputs,
        "nodes": nodes,
    }
    state = ProjectState.confirm_for_offline(plan, ctx)
    return plan, ctx, state


def test_chain_full_precision_and_real_invocations():
    plan, ctx, state = fixture()
    result = execute_project_plan(plan, ctx, state)
    assert result.status == "ready"
    v, hf, H = result.node_records
    assert v.outputs["velocity_mps"] == pytest.approx(1.273239544735162686, abs=1e-12)
    assert hf.outputs["head_loss_m"] == pytest.approx(1.652537144013663958, rel=1e-12)
    assert H.outputs["pump_head_m"] == pytest.approx(13.652537144013663958, abs=1e-12)
    assert v.outputs["velocity_mps"].hex() == hf.resolved_inputs["velocity_mps"].hex()
    assert hf.outputs["head_loss_m"].hex() == H.resolved_inputs["friction_head_m"].hex()
    assert len({r.invocation_id for r in result.node_records}) == 3
    assert all(r.invocation_id and len(r.args_hash) == 64 for r in result.node_records)


@pytest.mark.parametrize(
    "value", [False, True, "10", None, float("nan"), float("inf"), -0.0, 2**100]
)
def test_untrusted_numeric_input_never_executes(value):
    plan, ctx, state = fixture()
    plan["nodes"][0]["inputs"]["flow_lps"]["value"] = value
    result = execute_project_plan(plan, ctx, state)
    assert result.status != "ready" and not state.execution_records


def test_confirmation_cannot_be_replaced_with_model_numbers():
    plan, ctx, state = fixture()
    plan["nodes"][0]["inputs"]["flow_lps"]["value"] = 12.0
    assert execute_project_plan(plan, ctx, state).status == "blocked"


def test_evidence_failure_prevents_kernel(monkeypatch):
    from water_agent.tools import project_calculation as tools

    plan, ctx, state = fixture()
    calls = []
    monkeypatch.setattr(tools, "run_calc", lambda *args: calls.append(args))
    state.before_execute = lambda *_: (_ for _ in ()).throw(OSError("synthetic failure"))
    result = execute_project_plan(plan, ctx, state)
    assert result.status == "blocked" and calls == []


def test_cancelled_before_start():
    plan, ctx, state = fixture()
    object.__setattr__(ctx, "cancel_check", lambda: True)
    assert execute_project_plan(plan, ctx, state).status == "cancelled"


def test_no_replay():
    plan, ctx, state = fixture()
    assert execute_project_plan(plan, ctx, state).status == "ready"
    assert execute_project_plan(plan, ctx, state).status == "blocked"


def test_scope_dict_mutation_is_not_authorization():
    from water_agent.multi_agent.project_calculation import resolve_node
    from water_agent.tools.project_calculation import PROJECT_SPECS
    from water_agent.tools.registry import ToolRegistry

    plan, ctx, state = fixture()
    scope = resolve_node(
        __import__(
            "water_agent.multi_agent.project_calculation", fromlist=["validate_project_plan"]
        ).validate_project_plan(plan, ctx),
        "v",
        state,
        ctx,
    )
    scope.record.resolved_inputs["flow_lps"] = 99.0
    registry = ToolRegistry(principal=ctx.principal)
    for spec in PROJECT_SPECS:
        registry.register(spec)
    with pytest.raises(ValueError, match="PROJECT_UNTRUSTED_SOURCE"):
        registry.invoke_with_evidence(
            scope.record.tool_name, scope.record.resolved_inputs, scope=scope
        )


def test_namespace_isolation():
    from dataclasses import replace

    plan, ctx, state = fixture()
    assert (
        execute_project_plan(plan, replace(ctx, checkpoint_namespace="other"), state).status
        == "blocked"
    )


@pytest.mark.parametrize(
    "fault", ["exception", "bad_output", "cancel", "epoch", "generation", "timeout"]
)
def test_failed_upstream_does_not_publish_or_run_successors(monkeypatch, fault):
    from water_agent.tools import project_calculation as tools

    plan, ctx, state = fixture()
    original = tools.run_calc
    calls = []

    def fail(formula, args):
        calls.append(formula)
        if fault == "exception":
            raise OverflowError("synthetic")
        result = original(formula, args)
        if fault == "bad_output":
            result.values["velocity_mps"] = False
        if fault == "cancel":
            object.__setattr__(ctx, "cancel_check", lambda: True)
        if fault == "epoch":
            state.authorization_epoch += 1
        if fault == "generation":
            state.generation += 1
        return result

    monkeypatch.setattr(tools, "run_calc", fail)
    if fault == "timeout":
        from water_agent.tools import registry

        values = iter([0.0, 0.0, 0.0, 100.0])
        monkeypatch.setattr(registry.time, "monotonic", lambda: next(values))
    result = execute_project_plan(plan, ctx, state)
    assert result.status != "ready" and len(calls) == 1
    assert not state.artifacts and not state.active_artifact_ids
    assert all(not r.outputs and r.status != "verified" for r in result.node_records)


def test_principal_cannot_be_elevated_by_registry():
    from dataclasses import replace

    from water_agent.tools.registry import ToolRegistry

    plan, ctx, _ = fixture()
    ctx = replace(ctx, principal=Principal.local("denied", capabilities=frozenset()))
    state = ProjectState.confirm_for_offline(plan, ctx)
    assert execute_project_plan(plan, ctx, state, ToolRegistry()).status == "blocked"
    assert not state.execution_records


@pytest.mark.parametrize(
    "tamper", ["payload_hash_status", "revision", "case", "output", "activity", "missing_receipt"]
)
def test_untrusted_sources_rejected_even_with_recomputed_hash(tamper):
    from water_agent.multi_agent.contracts import ArtifactEnvelope, compute_payload_hash
    from water_agent.multi_agent.project_calculation_contracts import ProgramOutputBinding

    plan, ctx, state = fixture()
    res = execute_project_plan(plan, ctx, state)
    assert res.status == "ready"
    binding = res.node_records[1].input_sources["velocity_mps"]
    assert isinstance(binding, ProgramOutputBinding)
    if tamper == "payload_hash_status":
        data = state.artifacts[binding.artifact_id].model_dump()
        data["payload"]["node_record"]["outputs"]["velocity_mps"] = 99.0
        data["payload_hash"] = compute_payload_hash(data["payload"])
        data["status"] = "ready"
        data["producer"] = "hydraulic"
        state.artifacts[binding.artifact_id] = ArtifactEnvelope.model_validate(data)
        binding = binding.model_copy(update={"payload_hash": data["payload_hash"]})
    if tamper == "revision":
        binding = binding.model_copy(update={"revision": 2})
    if tamper == "case":
        binding = binding.model_copy(update={"case_id": "other"})
    if tamper == "output":
        binding = binding.model_copy(update={"output_key": "head_loss_m"})
    if tamper == "activity":
        state.active_artifact_ids.clear()
    if tamper == "missing_receipt":
        state.execution_records.clear()
    with pytest.raises(ValueError):
        state.validate_source(binding, ctx)


def test_internal_specs_not_in_production_plugins():
    import inspect

    from water_agent.plugins.builtin import HydraulicsPlugin
    from water_agent.tools.project_calculation import PROJECT_SPECS

    # Production contribution is the model-visible authority; no internal spec imports/references.
    source = inspect.getsource(HydraulicsPlugin)
    assert all(spec.name not in source for spec in PROJECT_SPECS)


def test_plain_registry_cannot_execute_internal_spec():
    from water_agent.tools.project_calculation import project_velocity_spec
    from water_agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(project_velocity_spec)
    result = registry.invoke(project_velocity_spec.name, flow_lps=10.0, diameter_mm=100.0)
    assert result["error"] is True


@pytest.mark.parametrize("field", ["units", "context_binding", "algorithm_version"])
def test_issued_record_metadata_cannot_be_rewritten(field):
    from water_agent.multi_agent.project_calculation import resolve_node, validate_project_plan
    from water_agent.tools.project_calculation import PROJECT_SPECS
    from water_agent.tools.registry import ToolRegistry

    plan, ctx, state = fixture()
    scope = resolve_node(validate_project_plan(plan, ctx), "v", state, ctx)
    if field == "units":
        scope.record.units["velocity_mps"] = "mm"
    if field == "context_binding":
        scope.record.context_binding["owner_user_id"] = "other"
    if field == "algorithm_version":
        scope.record = scope.record.model_copy(update={"algorithm_version": "fake"})
    registry = ToolRegistry(principal=ctx.principal)
    for spec in PROJECT_SPECS:
        registry.register(spec)
    with pytest.raises(ValueError, match="PROJECT_UNTRUSTED_SOURCE"):
        registry.invoke_with_evidence(
            scope.record.tool_name, scope.record.resolved_inputs, scope=scope
        )


def test_node_scope_cannot_be_reissued_to_bypass_budget():
    from water_agent.multi_agent.project_calculation import resolve_node, validate_project_plan

    plan, ctx, state = fixture()
    validated = validate_project_plan(plan, ctx)
    resolve_node(validated, "v", state, ctx)
    with pytest.raises(ValueError, match="PROJECT_BUDGET_EXCEEDED"):
        resolve_node(validated, "v", state, ctx)

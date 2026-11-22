"""C revisions use actual governed calls, not edited result fixtures."""

from dataclasses import replace
from uuid import uuid4

import pytest

from test_project_calculation_execution import fixture
from water_agent.multi_agent.project_calculation_service import Authorization, CalculationService


class Authority:
    def __init__(self, ctx):
        self.ctx = ctx
        self.epoch = 1
        self.allowed = True
        self.broken = False

    def query(self, ctx):
        if self.broken:
            raise OSError("synthetic repository failure")
        if not self.allowed or (ctx.owner_user_id, ctx.project_id, ctx.thread_id) != (
            self.ctx.owner_user_id,
            self.ctx.project_id,
            self.ctx.thread_id,
        ):
            raise ValueError("PROJECT_SCOPE_MISMATCH")
        return Authorization(self.ctx.principal, self.epoch)


def setup_service(tmp_path):
    plan, ctx, _ = fixture()
    authority = Authority(ctx)
    return CalculationService(tmp_path, authority), plan, ctx, authority


def confirm(service, plan, ctx, base=0, generation=0, branch=None):
    return service.confirm(plan, ctx, base_revision=base, generation=generation, branch=branch)


def test_T27_002_003_058_059_062_revisions(tmp_path, monkeypatch):
    service, plan, ctx, _ = setup_service(tmp_path)
    from water_agent.tools import project_calculation as tools

    original = tools.run_calc
    calls = []

    def kernel(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(tools, "run_calc", kernel)
    previous = None
    all_ids = set()
    for index, (q, length) in enumerate(
        [(10.0, 100.0), (12.0, 100.0), (12.0, 120.0), (12.0, 120.0)], 1
    ):
        plan["nodes"][0]["inputs"]["flow_lps"]["value"] = q
        plan["nodes"][1]["inputs"]["length_m"]["value"] = length
        current = replace(ctx, run_id=str(uuid4()))
        run = confirm(service, plan, current, index - 1, index - 1)
        assert run["plan"]["revision"] == index
        if previous:
            assert not service.release_check(previous, ctx)["valid"]
        run = service.execute(current)
        assert run["status"] == "waiting_approval"
        assert len(calls) == 3 * index
        records = run["result"]["node_records"]
        assert len({r["invocation_id"] for r in records} - all_ids) == 3
        all_ids.update(r["invocation_id"] for r in records)
        assert (
            records[0]["outputs"]["velocity_mps"].hex()
            == records[1]["resolved_inputs"]["velocity_mps"].hex()
        )
        assert (
            records[1]["outputs"]["head_loss_m"].hex()
            == records[2]["resolved_inputs"]["friction_head_m"].hex()
        )
        assert records[2]["outputs"]["pump_head_m"] == pytest.approx(
            [13.652537144013664, 14.379653487379676, 14.855584184855611, 14.855584184855611][
                index - 1
            ],
            abs=1e-12,
        )
        previous = current.run_id
        assert service.approve(current, True, "synthetic acceptance")["status"] == "approved"
    assert len(service.inspect(ctx)["runs"]) == 4


def test_T27_046_050_063_branch_history(tmp_path):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    first = service.execute(ctx)
    other = replace(ctx, run_id=str(uuid4()))
    confirm(service, plan, other, 1, 1, branch="fork")
    second = service.execute(other)
    assert second["plan"]["revision"] == 2 and second["plan"]["branch_id"] == "fork"
    assert first["plan_hash"] == second["plan"]["supersedes_plan_hash"]
    assert not service.release_check(ctx.run_id, ctx)["valid"]
    with pytest.raises(ValueError, match="PROJECT_REVISION_CONFLICT"):
        confirm(service, plan, replace(ctx, run_id=str(uuid4())), 2, 2, branch="main")


def test_T27_064_cancel_no_reactivation(tmp_path):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    service.approve(ctx, True, "ok")
    service.cancel(ctx)
    assert service.recover(ctx)["status"] == "cancelled"
    assert not service.release_check(ctx.run_id, ctx)["valid"]


@pytest.mark.parametrize("change", ["binding", "algorithm"])
def test_T27_060_061_changed_provenance_invalidates(tmp_path, monkeypatch, change):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    before = service.execute(ctx)
    service.approve(ctx, True, "ok")
    if change == "binding":
        plan["nodes"][0]["inputs"]["flow_lps"]["input_id"] = "Q-new-source"
    else:
        import water_agent.multi_agent.project_calculation_service as module

        monkeypatch.setattr(module, "algorithm_version", lambda: "f" * 64)
        assert not service.release_check(ctx.run_id, ctx)["valid"]
        # Simulate code-version change at the host, but do not accept old receipts.
        monkeypatch.undo()
    other = replace(ctx, run_id=str(uuid4()))
    confirm(service, plan, other, 1, 1)
    assert not service.release_check(ctx.run_id, ctx)["valid"]
    after = service.execute(other)
    assert after["status"] == "waiting_approval"
    assert set(before["artifacts"]).isdisjoint(after["artifacts"])


def test_internal_orchestrator_path(tmp_path):
    from water_agent.multi_agent.orchestrator import run_calculation_only

    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    assert run_calculation_only(service, ctx)["status"] == "waiting_approval"


def test_T27_050_program_source_cross_branch_rejected(tmp_path):
    from water_agent.multi_agent.project_calculation import (
        ProjectState,
        validate_project_plan,
        resolve_node,
    )
    from water_agent.multi_agent.project_calculation_contracts import ProgramOutputBinding

    plan, ctx, state = fixture()
    from water_agent.multi_agent.project_calculation import execute_project_plan

    execute_project_plan(plan, ctx, state)
    env = state.artifacts[state.active_artifact_ids["v"]]
    binding = ProgramOutputBinding(
        kind="program",
        node_id="v",
        case_id="v",
        artifact_id=env.artifact_id,
        revision=1,
        output_key="velocity_mps",
        payload_hash=env.payload_hash,
        unit="m/s",
    )
    with pytest.raises(ValueError, match="PROJECT_SCOPE_MISMATCH"):
        state.validate_source(binding, replace(ctx, branch_id="fork"))

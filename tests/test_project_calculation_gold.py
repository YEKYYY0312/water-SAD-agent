"""Independent 100-place Machin pi / energy relation, no production oracle."""

from decimal import Decimal, localcontext

import pytest
from test_project_calculation_execution import fixture

from water_agent.multi_agent.project_calculation import ProjectState, execute_project_plan


def atan(x):
    term = total = x
    k = 1
    while True:
        term = -term * x * x
        new = total + term / Decimal(2 * k + 1)
        if total == new:
            return total
        total = new
        k += 1


def gold(q, L):
    with localcontext() as ctx:
        ctx.prec = 100
        D = Decimal
        pi = 16 * atan(D(1) / 5) - 4 * atan(D(1) / 239)
        v = 4 * D(q) / 1000 / (pi * D(".1") ** 2)
        hf = D(".02") * D(L) / D(".1") * v * v / (2 * D("9.81"))
        return v, hf, 12 + hf


@pytest.mark.parametrize("q,L", [(10, 100), (12, 100), (12, 120)])
def test_three_references_and_full_precision(q, L):
    plan, ctx, _ = fixture()
    plan["nodes"][0]["inputs"]["flow_lps"]["value"] = float(q)
    plan["nodes"][1]["inputs"]["length_m"]["value"] = float(L)
    state = ProjectState.confirm_for_offline(plan, ctx)
    res = execute_project_plan(plan, ctx, state)
    assert res.status == "ready"
    for record, key, ref in zip(
        res.node_records, ["velocity_mps", "head_loss_m", "pump_head_m"], gold(q, L), strict=True
    ):
        assert abs(Decimal.from_float(record.outputs[key]) - ref) <= Decimal("1e-12")
    if q == 12:
        assert any(n.source_node_id == "v" for n in res.node_records[-1].assumptions)


def test_round3_mutation_is_detected():
    v, hf, _ = gold(10, 100)
    rounded = Decimal(str(round(float(v), 3)))
    assert abs(
        Decimal(".02") * 100 / Decimal(".1") * rounded**2 / (2 * Decimal("9.81")) - hf
    ) > Decimal("1e-6")


@pytest.mark.parametrize("parts", [(0.0, 2.8, 0.0, 2.0), (0.0, 0.0, 0.0, 0.0)])
def test_pump_zero_numeric_contract_only(monkeypatch, parts):
    from water_agent.tools import project_calculation as tools

    # Numeric unit test only: service authorization is covered separately, never a fake zero-flow chain.
    monkeypatch.setattr(tools, "_guard", lambda _: None)
    args = tools.Pump(
        **dict(
            zip(
                ["static_head_m", "friction_head_m", "local_head_m", "residual_head_m"],
                parts,
                strict=True,
            )
        )
    )
    result = tools._execute(2, args)
    with localcontext() as ctx:
        ctx.prec = 2000
        expected = float(sum((Decimal.from_float(v) for v in parts), Decimal(0)))
    import math

    assert abs(result["outputs"]["pump_head_m"] - expected) <= math.ulp(expected)

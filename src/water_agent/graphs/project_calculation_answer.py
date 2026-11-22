"""Program-only candidate summary and explicit public provenance projection."""

from __future__ import annotations

from typing import Any

from water_agent.multi_agent.project_calculation_contracts import ProjectCalculationResult


def project_calculation_projection(run: dict[str, Any]) -> list[dict[str, Any]]:
    result = ProjectCalculationResult.model_validate(run["result"])
    if result.status != "ready":
        raise ValueError("PROJECT_UNTRUSTED_SOURCE")
    return [
        {
            "node_id": r.node_id,
            "formula": r.formula,
            "inputs": {
                key: {"value": value, "unit": r.units[key], "source": source.model_dump()}
                for key, value in r.resolved_inputs.items()
                for source in [r.input_sources[key]]
            },
            "outputs": r.outputs,
            "units": r.units,
            "assumptions": [n.model_dump() for n in r.assumptions],
        }
        for r in result.node_records
    ]


def render_project_calculation_summary(run: dict[str, Any]) -> str:
    nodes = project_calculation_projection(run)
    lines = [
        f"项目计算 {run['plan']['plan_id']} / 修订 {run['plan']['revision']}",
        "生成时状态：ready_for_approval；发布审批见随附记录。",
    ]
    for node in nodes:
        lines.append(f"节点 {node['node_id']}：{node['formula']}")
        for key, item in node["inputs"].items():
            lines.append(f"输入 {key} = {item['value']!r} {item['unit']}")
        for key, value in node["outputs"].items():
            lines.append(f"输出 {key} = {value!r} {node['units'][key]}")
        for note in node["assumptions"]:
            lines.append(
                f"工具提示（来源 {note['source_node_id']}，未进行规范校核）：{note['message']}"
            )
    lines.append("仅数值参考：系数及参数出处、工程适用性未核验；未检索条文，未做合规判定或选泵。")
    return "\n".join(lines)

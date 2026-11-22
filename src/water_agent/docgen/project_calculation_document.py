"""Approval candidate Word for trusted T27 runs; no calculation or publication here.

The service validates authority and execution provenance before calling this renderer.
This boundary repeats strict shape/completeness checks to prevent a partial document.
Approval metadata is delivered separately; these candidate bytes are never rewritten.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from pydantic import TypeAdapter

from water_agent.docgen.docx_export import (
    _add_body,
    _add_heading,
    _add_table,
    _configure_header,
    _configure_styles,
)
from water_agent.multi_agent.project_calculation_contracts import (
    INPUT_UNITS,
    OUTPUTS,
    Hash,
    NodeExecutionRecord,
    ProgramOutputBinding,
    ProjectCalculationPlan,
    ProjectCalculationResult,
    UserInputBinding,
)

PROJECT_DOCUMENT_RENDERER_VERSION = "t27-docx-v1"
_LIMITATIONS = (
    "仅供参考；仅验证数学和来源一致性，参数来源和工程适用性未验证。",
    "未检索条文；未做选泵合规校核。",
    "工具提示，未进行规范校核。发布审批见随附记录。",
)
_CHAPTERS = (
    ("v — 流速", "v = (Q / 1000) / (π × (d / 1000)² / 4)；Q=flow_lps，d=diameter_mm"),
    ("hf — 沿程水头损失", "hf = f × (L / (d / 1000)) × v² / (2 × g)；f=friction_factor，L=length_m，d=diameter_mm，v=velocity_mps，g=gravity_mps2"),
    ("H — 所需扬程", "H = static_head_m + friction_head_m + local_head_m + residual_head_m"),
)


def _validated(run: dict[str, Any]) -> tuple[ProjectCalculationPlan, ProjectCalculationResult]:
    plan = ProjectCalculationPlan.model_validate(run["plan"])
    result = ProjectCalculationResult.model_validate(run["result"])
    manifest = TypeAdapter(Hash).validate_python(run["execution_manifest_hash"], strict=True)
    if result.status != "ready" or result.errors or manifest != result.manifest_hash:
        raise ValueError("PROJECT_INVALID_DOCUMENT")
    for key in ("project_id", "plan_id", "branch_id", "revision", "input_set_id"):
        if getattr(result, key) != getattr(plan, key):
            raise ValueError("PROJECT_INVALID_DOCUMENT")
    TypeAdapter(Hash).validate_python(result.plan_hash, strict=True)
    context = run["context"]
    for key, expected in (
        ("project_id", plan.project_id), ("plan_id", plan.plan_id),
        ("branch_id", plan.branch_id), ("plan_revision", plan.revision),
    ):
        if type(context[key]) is not type(expected) or context[key] != expected:
            raise ValueError("PROJECT_INVALID_DOCUMENT")
    if [rec.node_id for rec in result.node_records] != ["v", "hf", "H"]:
        raise ValueError("PROJECT_INVALID_DOCUMENT")
    for index, (node, item) in enumerate(zip(plan.nodes, result.node_records, strict=True)):
        # Revalidate through a dict even if caller supplied model instances.
        rec = NodeExecutionRecord.model_validate(item.model_dump())
        expected_inputs = set(INPUT_UNITS[index])
        expected_units = {**INPUT_UNITS[index], OUTPUTS[index]: "m/s" if index == 0 else "m"}
        binding = {k: context[k] for k in (
            "project_id", "plan_id", "branch_id", "owner_user_id", "thread_id", "run_id",
            "generation", "authorization_epoch",
        )}
        binding["revision"] = plan.revision
        if (
            rec.status != "verified" or rec.case_id != node.case_id
            or rec.formula != node.formula or rec.plan_hash != result.plan_hash
            or rec.context_binding != binding
            or set(rec.resolved_inputs) != expected_inputs
            or set(rec.input_sources) != expected_inputs
            or rec.units != expected_units or set(rec.outputs) != {OUTPUTS[index]}
            or not rec.algorithm_version or not rec.tool_identity_version
            or not rec.tool_name
            or rec.outputs[OUTPUTS[index]] < 0
            or (index < 2 and rec.outputs[OUTPUTS[index]] == 0)
        ):
            raise ValueError("PROJECT_INVALID_DOCUMENT")
        for key, source in rec.input_sources.items():
            if source.unit != rec.units[key]:
                raise ValueError("PROJECT_INVALID_DOCUMENT")
            if isinstance(source, UserInputBinding):
                if source != node.inputs[key] or source.value != rec.resolved_inputs[key]:
                    raise ValueError("PROJECT_INVALID_DOCUMENT")
            elif isinstance(source, ProgramOutputBinding):
                if index == 0:
                    raise ValueError("PROJECT_INVALID_DOCUMENT")
                previous = result.node_records[index - 1]
                if (
                    source.node_id != previous.node_id or source.case_id != previous.case_id
                    or source.revision != plan.revision or source.output_key != OUTPUTS[index - 1]
                    or rec.resolved_inputs[key] != previous.outputs[source.output_key]
                ):
                    raise ValueError("PROJECT_INVALID_DOCUMENT")
    return plan, result


def _source_text(source: UserInputBinding | ProgramOutputBinding) -> str:
    if isinstance(source, UserInputBinding):
        return f"用户：input_id={source.input_id}；input_set_id={source.input_set_id}"
    return (
        f"程序上游：node_id={source.node_id}；artifact_id={source.artifact_id}；"
        f"revision={source.revision}；case_id={source.case_id}；output_key={source.output_key}；"
        f"payload_hash={source.payload_hash}"
    )


def render_project_calculation_docx(run: dict) -> bytes:
    """Render the service-validated internal run as an immutable approval candidate."""
    from docx import Document

    plan, result = _validated(run)
    document = Document()
    _configure_styles(document)
    _configure_header(document, f"项目 {plan.project_id} 项目级多步计算书")
    _add_heading(document, "项目级多步计算书", style="Title")
    for line in (
        f"project_id={plan.project_id}；plan_id={plan.plan_id}；branch_id={plan.branch_id}",
        f"revision={plan.revision}；run_id={run['context']['run_id']}",
        "生成时状态：ready_for_approval；发布审批见随附记录。",
        f"合同版本：{result.contract_version}；渲染器版本：{PROJECT_DOCUMENT_RENDERER_VERSION}",
        f"plan_hash={result.plan_hash}",
        f"execution_manifest_hash={result.manifest_hash}",
        *_LIMITATIONS,
    ):
        _add_body(document, line)
    for index, rec in enumerate(result.node_records):
        heading, formula = _CHAPTERS[index]
        _add_heading(document, heading)
        for line in (
            f"node_id={rec.node_id}；case_id={rec.case_id}；formula={rec.formula}",
            f"公式：{formula}",
            f"algorithm_version={rec.algorithm_version}；contract_version={rec.contract_version}",
            f"tool_name={rec.tool_name}；tool_identity_version={rec.tool_identity_version}",
        ):
            _add_body(document, line)
        _add_table(document, ["输入参数", "完整数值", "单位", "来源绑定"], [
            [key, repr(rec.resolved_inputs[key]), rec.units[key], _source_text(rec.input_sources[key])]
            for key in INPUT_UNITS[index]
        ])
        _add_table(document, ["程序输出", "完整数值", "单位"], [
            [key, repr(value), rec.units[key]] for key, value in rec.outputs.items()
        ])
        _add_body(document, "节点警示及继承的上游说明（工具提示，未进行规范校核）：")
        for label, disclosures in (("assumptions", rec.assumptions), ("warnings", rec.warnings)):
            if not disclosures:
                _add_body(document, f"{label}：无记录")
            for note in disclosures:
                _add_body(document, (
                    f"{label}；来源节点={note.source_node_id}；code={note.code}；"
                    f"severity={note.severity}；{note.message}"
                ))
    target = BytesIO()
    document.save(target)
    return target.getvalue()


def project_document_text(content: bytes) -> str:
    """Extract paragraph and table text in document order for deterministic guard checks."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(BytesIO(content))
    lines = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            lines.append(block.text)
        elif isinstance(block, Table):
            lines.extend("\t".join(cell.text for cell in row.cells) for row in block.rows)
    return "\n".join(lines)

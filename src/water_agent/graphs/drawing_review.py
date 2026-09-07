"""图纸审查确定性子图（P3 Step 22）。

固定管线来自 `docs/design/02-loop-engineering.md:67-71`：

    load_dwg → extract_entities → map_to_schema → rule_check → report_defects

对应到 Step 19–21 交付的实现（Step 21 决策记录 §4 交接的接缝）：

    read_drawing（load + extract：登记号 → 结构化图元，路径防御全在解析器内）
    map_pipes    （map_to_schema：图元 → 管段语义）
    review_drawing（rule_check：管段 → 分级缺陷 + 未检查项）
    report       （report_defects：缺陷清单 + 未检查项 + 缺输入 + 结论）

本图是**确定性编排**：全管线不调用任何模型（铁律① —— 数值与判定程序化，
LLM 不在本管线里）。它和 `graphs/calc.py` 一样使用独立内部状态（`DrawingReviewState`），
不占用 `WaterAgentState`；何时把它接进主图、把结果写入 `WaterAgentState.drawings`
是后续接线步的事（本步没有调用方，不预埋死字段）。

编排纪律（Step 21 交接 §4 / 任务书硬约束，违反其一即失败）
------------------------------------------------------------
1. **报告渲染的唯一出口是 `Finding.rendered_finding()`**。本模块不拼写任何
   缺陷措辞 —— `report_text` 的缺陷行逐条取自 `rendered` 字段原文；
   `not_checked`/`missing_inputs`/`assumptions`/`summary` 原样透传 Step 21 结构。
2. **`not_checked` 必须进报告**：一条规则因缺输入没跑，与它跑了且通过是
   完全不同的结论。报告把未检查项单独成节。
3. **`pipes_reviewed == 0` 时不得出现合规措辞**。图纸解析失败 / 图上无可识别
   管线，都走完 map → check → report 全管线 —— `ReviewResult.summary()` 在零管段
   时输出"**未执行任何管段审查**…不含任何合规判断"（Step 21 D7 修过的坑）；
   本图在 `pipes_reviewed == 0` 时连"缺陷（0 条）"小节都不渲染 ——
   零审查的图不该有任何看起来像"没问题"的计数。

错误传播语义：`DrawingExtraction.ok=False` **不中断管线**。失败的错误码在
`extraction_errors` 原样携带（`DRAWING_NOT_FOUND` / `DRAWING_UNREADABLE` / …，
前置条件缺失与真实失败可区分），`map_pipes` 会把"图纸抽取失败"写进
`missing_inputs`，`review_drawing` 产出零管段结论 —— 最终报告的
`ok=False`、`status="failed"`、`summary="未执行任何管段审查…"` 让
"审查失败"与"检查过且干净"一眼可分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from water_agent.domain.state import STATUS_FAILED, STATUS_SUCCEEDED
from water_agent.drawing.dxf_reader import read_drawing
from water_agent.drawing.mapping import MappingResult, map_pipes
from water_agent.drawing.models import DrawingExtraction
from water_agent.drawing.rules import ReviewResult, review_drawing
from water_agent.runtime.governance.pipeline import (
    GuardrailPipeline,
    ToolPolicy,
    default_guardrail_pipeline,
)


class DrawingReviewState(TypedDict):
    """图纸审查子图内部状态（与 WaterAgentState 解耦）。

    阶段产物在管线内以对象形式传递（无 checkpointer，无需序列化）——
    与 `graphs/calc.py` 的 `calc_result` 同一模式；仅在 report 节点收口为
    JSON 可序列化 dict。
    """

    drawing_id: str
    extraction: DrawingExtraction | None
    mapping: MappingResult | None
    review: ReviewResult | None
    report: dict[str, Any] | None
    status: str
    step_count: int


@dataclass(slots=True)
class DrawingReviewReport:
    """审查子图的最终报告：结构化字段 + 人类可读 `report_text`。

    ``ok`` 与审查**完成**是两个维度：管线总能把一张图"审完"（即使解析失败），
    ``ok`` 表达的是"审查对象是否成功抽取"。`status` 与 `summary` 由 Step 21 的
    语义决定，本模块只做编排，不改写结论措辞。
    """

    drawing_id: str
    ok: bool
    status: str
    #: Step 21 `ReviewResult.summary()` 原文 —— 唯一的一句话结论出口。
    summary: str
    pipes_reviewed: int
    #: 解析摘要（file_hash 等，**不含路径**；与 DrawingExtraction 同纪律）。
    extraction: dict[str, Any] = field(default_factory=dict)
    extraction_errors: tuple[str, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    rendered_findings: tuple[str, ...] = ()
    not_checked: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    report_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "drawing_id": self.drawing_id,
            "ok": self.ok,
            "status": self.status,
            "summary": self.summary,
            "pipes_reviewed": self.pipes_reviewed,
            "extraction": self.extraction,
            "extraction_errors": list(self.extraction_errors),
            "findings": [f for f in self.findings],
            "rendered_findings": list(self.rendered_findings),
            "not_checked": list(self.not_checked),
            "missing_inputs": list(self.missing_inputs),
            "assumptions": list(self.assumptions),
            "report_text": self.report_text,
        }


def render_report_text(report: DrawingReviewReport) -> str:
    """把报告渲染为人类可读文本。

    渲染纪律：缺陷行逐条来自 `Finding.rendered_finding()` 的 `rendered` 字段，
    未检查/缺输入/假设/结论逐条透传 Step 21 原文 —— 本函数只排版，不改写措辞。
    小节标题用中性的【】标注，不携带任何合规性判断。

    `pipes_reviewed == 0` 时**省略缺陷小节**：零审查的图若出现"缺陷（0 条）"，
    读者会把它读成"没问题"，而那正是零管段结论要避免的读法（编排纪律 3）。
    """
    lines = [
        f"== 图纸审查报告：{report.drawing_id} ==",
        f"审查对象抽取：{'成功' if report.ok else '失败'}",
        f"已审查管段：{report.pipes_reviewed}",
        f"结论：{report.summary}",
    ]
    if report.pipes_reviewed > 0 and report.findings:
        lines.append(f"【缺陷（{len(report.findings)} 条）】")
        lines.extend(f"- {rendered}" for rendered in report.rendered_findings)
    if report.not_checked:
        lines.append(f"【未检查项（{len(report.not_checked)} 项）】")
        lines.extend(f"- {entry}" for entry in report.not_checked)
    if report.missing_inputs:
        lines.append(f"【缺输入（{len(report.missing_inputs)} 项）】")
        lines.extend(f"- {entry}" for entry in report.missing_inputs)
    if report.assumptions:
        lines.append(f"【假设（{len(report.assumptions)} 项）】")
        lines.extend(f"- {entry}" for entry in report.assumptions)
    if report.extraction_errors:
        lines.append("【解析错误】")
        lines.extend(f"- {entry}" for entry in report.extraction_errors)
    return "\n".join(lines)


def _extraction_summary(extraction: DrawingExtraction) -> dict[str, Any]:
    """解析摘要：只带计数与稳定标识，不内联整批图元（报告不是实体转储）。"""
    return {
        "file_hash": extraction.file_hash,
        "dxf_version": extraction.dxf_version,
        "unit_known": extraction.unit_known,
        "truncated": extraction.truncated,
        "entity_count": len(extraction.entities),
        "layer_count": len(extraction.layers),
    }


def _redact_root(text: str, root: Path) -> str:
    """报告层的路径脱敏：错误详情里的绝对路径替换为 ``<DRAWING_ROOT>`` 占位。

    `extraction.errors` 保留原文（进程内调试需要），但报告是**设计为可落盘/
    落 trace 的产物** —— ezdxf 的异常 message 会内嵌文件绝对路径，
    原样透传就构成路径泄漏（与 `DrawingExtraction.file_hash` 不含路径的同一纪律）。
    """
    for form in (str(root.resolve()), str(root.expanduser())):
        if form and form in text:
            text = text.replace(form, "<DRAWING_ROOT>")
    return text


def _build_report(state: DrawingReviewState, *, root: Path) -> DrawingReviewReport:
    """从各阶段产物组装最终报告（纯函数，便于单测）。"""
    drawing_id = state.get("drawing_id", "")
    extraction = state.get("extraction")
    review = state.get("review")
    ok = bool(extraction is not None and extraction.ok)

    findings = tuple(f.to_dict() for f in (review.findings if review is not None else []))
    rendered = tuple(str(f.get("rendered", "")) for f in findings if f.get("rendered"))
    errors = tuple(
        _redact_root(str(e), root) for e in (extraction.errors if extraction is not None else ())
    )
    report = DrawingReviewReport(
        drawing_id=drawing_id,
        ok=ok,
        status=STATUS_SUCCEEDED if ok else STATUS_FAILED,
        summary=str(review.summary() if review is not None else ""),
        pipes_reviewed=int(review.pipes_reviewed if review is not None else 0),
        extraction=_extraction_summary(extraction) if extraction is not None else {},
        extraction_errors=errors,
        findings=findings,
        rendered_findings=rendered,
        not_checked=tuple(review.not_checked) if review is not None else (),
        missing_inputs=tuple(review.missing_inputs) if review is not None else (),
        assumptions=tuple(review.assumptions) if review is not None else (),
    )
    report.report_text = render_report_text(report)
    return report


def build_drawing_review_graph(
    *,
    root: Path,
    policy: ToolPolicy | None = None,
    pipeline: GuardrailPipeline | None = None,
):
    """构建图纸审查确定性子图（load → map → check → report）。

    Args:
        root: 图纸目录（`DRAWING_ROOT`）。登记号只在此目录内解析 ——
            与 `read_drawing` 同一路径口径，**装配期注入**，不入图状态。
        policy: 读白名单策略；`None` 时按 ``root`` 自动构造（同
            `tools/read_drawing.build_read_drawing_tool` 的语义：装配方传入
            ``root`` 已表达"允许读这个目录"的意图）。
        pipeline: 护栏管线；`None` 取进程级默认管线（**不是跳过护栏**）。
    """
    guard = pipeline if pipeline is not None else default_guardrail_pipeline()
    effective_policy = (
        policy
        if policy is not None
        else ToolPolicy(allowed_read_dirs=frozenset({root.resolve()}))
    )

    graph = StateGraph(DrawingReviewState)

    def load_extract_node(state: DrawingReviewState) -> dict[str, Any]:
        extraction = read_drawing(
            state.get("drawing_id", ""),
            root=root,
            policy=effective_policy,
            pipeline=guard,
        )
        return {
            "extraction": extraction,
            "status": STATUS_SUCCEEDED if extraction.ok else STATUS_FAILED,
            "step_count": state.get("step_count", 0) + 1,
        }

    def map_node(state: DrawingReviewState) -> dict[str, Any]:
        extraction = state.get("extraction")
        mapped = map_pipes(extraction) if extraction is not None else None
        return {
            "mapping": mapped,
            "step_count": state.get("step_count", 0) + 1,
        }

    def check_node(state: DrawingReviewState) -> dict[str, Any]:
        mapping = state.get("mapping")
        reviewed = review_drawing(mapping) if mapping is not None else None
        return {
            "review": reviewed,
            "step_count": state.get("step_count", 0) + 1,
        }

    def report_node(state: DrawingReviewState) -> dict[str, Any]:
        report = _build_report(state, root=root)
        return {
            "report": report.to_dict(),
            "status": report.status,
            "step_count": state.get("step_count", 0) + 1,
        }

    graph.add_node("load_extract", load_extract_node)
    graph.add_node("map_pipes", map_node)
    graph.add_node("rule_check", check_node)
    graph.add_node("report", report_node)

    graph.add_edge(START, "load_extract")
    graph.add_edge("load_extract", "map_pipes")
    graph.add_edge("map_pipes", "rule_check")
    graph.add_edge("rule_check", "report")
    graph.add_edge("report", END)

    return graph.compile()


def run_drawing_review(
    drawing_id: str,
    *,
    root: Path,
    policy: ToolPolicy | None = None,
    pipeline: GuardrailPipeline | None = None,
) -> dict[str, Any]:
    """跑一次完整图纸审查，返回报告的 dict（JSON 可序列化）。

    直接走 LangGraph 子图（编排入口与评测/CLI 共用，保证测的是同一管线）。
    """
    app = build_drawing_review_graph(root=root, policy=policy, pipeline=pipeline)
    state: DrawingReviewState = {
        "drawing_id": drawing_id,
        "extraction": None,
        "mapping": None,
        "review": None,
        "report": None,
        "status": STATUS_SUCCEEDED,
        "step_count": 0,
    }
    result = app.invoke(state)
    report = result.get("report")
    if not isinstance(report, dict):
        raise TypeError(
            f"图纸审查子图未产出报告（drawing_id={drawing_id!r}，"
            f"得到 {type(report).__name__}）—— 编排层契约被破坏"
        )
    return report

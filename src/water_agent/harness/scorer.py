"""确定性评测判分器。"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from water_agent.docgen import (
    REQUIRED_SECTIONS,
    PolishVerdict,
    RenderedDoc,
    normalize_heading,
    validate_citation_policy,
)
from water_agent.domain.models import (
    AgenticTurn,
    CalcCase,
    CalcTolerances,
    DocgenCase,
    DocgenQualityCase,
    EvalCase,
    EvalResult,
    GovernanceCase,
    MaterialCase,
)
from water_agent.harness.retrieval_metrics import case_retrieval_metrics
from water_agent.materials import MaterialTable
from water_agent.tools.hydraulics import CalcResult, run_calc


@dataclass(slots=True)
class ScoreBreakdown:
    passed: bool
    failure_reason: str
    actual_status: str
    actual_citations: list[str]
    expected_articles: list[str]
    gold_articles_any_hit: list[str]
    gold_articles_all_hit: list[str]
    forbidden_articles_hit: list[str]



def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output



def score_eval_case(
    case: EvalCase,
    *,
    actual_status: str,
    actual_citations: Iterable[str],
    context_articles: Iterable[str] | None = None,
    latency_ms: float = 0.0,
    trace_id: str = "",
    retry_count: int = 0,
    line_no: int | None = None,
    errors: Iterable[str] | None = None,
) -> EvalResult:
    """RAG 用例判分。

    ``errors`` 是运行时的错误文本（引用校验器的拒绝原因就在这里）。陷阱用例
    （``expected_rejected_citations``）只有在运行失败**且**错误里点名了伪造引用时才通过，
    这些引用本身不再计入"不在上下文"的判负——它们就是陷阱要校验器拒绝的东西。
    """
    actual_citation_list = _dedupe(actual_citations)
    context_article_list = _dedupe(context_articles or [])
    expected_articles = _dedupe([*case.gold_articles_any, *case.gold_articles_all])
    error_texts = [str(error) for error in errors or []]
    trap_citations = set(case.expected_rejected_citations)

    citation_set = set(actual_citation_list)
    context_set = set(context_article_list)
    invalid_citations = [
        citation
        for citation in actual_citation_list
        if citation not in context_set and citation not in trap_citations
    ]

    gold_any_hit = [article for article in case.gold_articles_any if article in citation_set]
    gold_all_hit = [article for article in case.gold_articles_all if article in citation_set]
    forbidden_hit = [article for article in case.forbidden_articles if article in citation_set]

    reasons: list[str] = []
    if actual_status != case.expected_status:
        reasons.append(f"expected status {case.expected_status}, got {actual_status}")
    for trap in case.expected_rejected_citations:
        # 错误文本里的引用号是 repr 形态（'FAKE 0.0.0'），按子串匹配即可。
        if not any(trap in text for text in error_texts):
            reasons.append(f"trap citation {trap!r} was not rejected by the citation validator")
    if invalid_citations:
        reasons.append(f"citations not present in retrieved context: {invalid_citations}")
    if case.gold_articles_any and not gold_any_hit:
        reasons.append(f"gold_articles_any not hit: {case.gold_articles_any}")
    if case.gold_articles_all and len(gold_all_hit) != len(case.gold_articles_all):
        missing = [article for article in case.gold_articles_all if article not in citation_set]
        reasons.append(f"gold_articles_all missing: {missing}")
    if forbidden_hit:
        reasons.append(f"forbidden_articles hit: {forbidden_hit}")

    passed = not reasons
    retrieval_metrics = case_retrieval_metrics(
        context_article_list,
        [*case.gold_articles_any, *case.gold_articles_all],
    )
    return EvalResult(
        case_id=case.id,
        passed=passed,
        expected_status=case.expected_status,
        actual_status=actual_status,
        expected_articles=expected_articles,
        actual_citations=actual_citation_list,
        failure_reason="; ".join(reasons),
        latency_ms=round(latency_ms, 3),
        trace_id=trace_id,
        retry_count=retry_count,
        gold_articles_any_hit=gold_any_hit,
        gold_articles_all_hit=gold_all_hit,
        forbidden_articles_hit=forbidden_hit,
        line_no=line_no,
        context_articles=context_article_list,
        variant=case.variant,
        retrieval_metrics=retrieval_metrics,
    )


def score_agentic_turn(
    turn: AgenticTurn,
    *,
    actual_status: str,
    answer: str,
    citations: Iterable[str],
    context_articles: Iterable[str],
    errors: Iterable[str],
    warnings: Iterable[str],
    tool_rejected_citations: Iterable[str],
    script_steps_used: int,
    model_inputs: Iterable[str] = (),
) -> list[str]:
    """mock-agentic 用例一轮的判分原因（空 = 通过）。

    RAG 口径（状态 / 引用 ⊆ 跨轮检索上下文 / gold / forbidden / 陷阱）与
    :func:`score_eval_case` 同源；陷阱引用被 cite 工具当场拒绝（可追溯检查）与被
    校验器拒绝同样算"点名拒绝"。在此之上再查 agentic 专有的收口纪律：成功必须有
    非空回答、引用集合精确、warnings / errors 期望、脚本恰好用完。
    """
    citation_list = _dedupe(citations)
    warning_list = [str(item) for item in warnings]
    error_list = [str(item) for item in errors]
    evidence = [
        *error_list,
        *(f"cite_clauses 拒绝了引用 {cid!r}" for cid in tool_rejected_citations),
    ]
    rag_case = EvalCase(
        id="agentic-turn",
        query=turn.query,
        gold_articles_any=turn.gold_articles_any,
        gold_articles_all=turn.gold_articles_all,
        forbidden_articles=turn.forbidden_articles,
        expected_rejected_citations=turn.expected_rejected_citations,
        expected_status=turn.expected_status,
    )
    scored = score_eval_case(
        rag_case,
        actual_status=actual_status,
        actual_citations=citation_list,
        context_articles=context_articles,
        errors=evidence,
    )
    reasons = [scored.failure_reason] if scored.failure_reason else []
    if actual_status == "succeeded" and not answer.strip():
        reasons.append("succeeded with an empty answer")
    if turn.expected_citations is not None and citation_list != turn.expected_citations:
        reasons.append(f"citations {citation_list} != expected {turn.expected_citations}")
    for expected in turn.expected_warnings:
        if expected not in warning_list:
            reasons.append(f"expected warning missing: {expected}")
    for fragment in turn.expected_error_substrings:
        if not any(fragment in error for error in error_list):
            reasons.append(f"expected error containing {fragment!r}, got {error_list}")
    for fragment in turn.answer_must_not_contain:
        if fragment in answer:
            reasons.append(f"answer must not contain {fragment!r}")
    seen_by_model = "\n".join(model_inputs)
    for fragment in turn.model_input_must_not_contain:
        if fragment in seen_by_model:
            reasons.append(f"model input must not contain {fragment!r}")
    unused = len(turn.script) - script_steps_used
    if unused > 0:
        reasons.append(f"script has {unused} unused step(s): the loop stopped earlier than scripted")
    return reasons


def _within_tolerance(actual: float, gold: float, tolerances: CalcTolerances) -> bool:
    abs_err = abs(actual - gold)
    rel_err = abs_err / abs(gold) if gold else 0.0
    return abs_err <= tolerances.absolute and rel_err <= tolerances.relative


def score_calc_case(
    case: CalcCase,
    *,
    calc_result: CalcResult | None,
    latency_ms: float = 0.0,
    line_no: int | None = None,
    graph_status: str | None = None,
    graph_missing_inputs: Iterable[str] | None = None,
) -> EvalResult:
    """程序化数值判分：状态匹配 + 每个 gold 键双阈值比对。

    - 缺参用例：gold 为空 + expected_status == "missing_inputs" 时按通过计；
    - 非法输入用例：gold 为空 + expected_status == "invalid_input" 时按通过计；
    - 正常用例：expected_status == "succeeded"，逐键数值比对。

    端到端（runner 驱动计算子图）传 ``graph_status`` / ``graph_missing_inputs``：状态以子图
    自己报告的为准，``calc_result`` 为子图产物（缺参/口径外在解析阶段短路时为 None）；
    报 succeeded 却没有有效计算结果即判负。只传 ``calc_result`` 时是工具级判分，状态由
    结果推出。
    """
    if graph_status is not None:
        actual_status = graph_status
        missing_inputs = list(graph_missing_inputs or [])
    elif calc_result is not None:
        actual_status = "succeeded" if calc_result.ok else ("missing_inputs" if calc_result.missing_inputs else "invalid_input")
        missing_inputs = list(calc_result.missing_inputs)
    else:
        raise ValueError(f"calc case {case.id} 需要 calc_result 或 graph_status 才能判分")
    reasons: list[str] = []
    values = calc_result.values if calc_result is not None else {}

    if actual_status == "succeeded" and (calc_result is None or not calc_result.ok):
        reasons.append("calc graph reported succeeded without a valid calc_result")
    if actual_status != case.expected_status:
        reasons.append(f"expected status {case.expected_status}, got {actual_status}")
        if calc_result is not None:
            reasons.append(f"tool detail: missing_inputs={calc_result.missing_inputs} errors={calc_result.errors}")
    elif actual_status == "missing_inputs" and case.expected_missing_inputs:
        if set(missing_inputs) != set(case.expected_missing_inputs):
            reasons.append(
                f"missing_inputs mismatch: expected {sorted(case.expected_missing_inputs)}, "
                f"got {sorted(missing_inputs)}"
            )
    elif calc_result is not None and calc_result.ok:
        for key, gold_value in case.gold.items():
            actual_value = values.get(key)
            if actual_value is None:
                reasons.append(f"gold key {key!r} missing from output {sorted(values)}")
            elif not _within_tolerance(actual_value, gold_value, case.tolerances):
                reasons.append(
                    f"{key}: actual={actual_value:.6f}, gold={gold_value}, "
                    f"tol(rel={case.tolerances.relative}, abs={case.tolerances.absolute})"
                )

        # 额外检查：期望的 assumptions（如果有）应出现在工具输出的 assumptions 中
        expected_assumptions = getattr(case, 'expected_assumptions', []) or []
        if expected_assumptions:
            actual_assumptions = calc_result.assumptions or []
            for expected in expected_assumptions:
                # treat expected as substring to match against any actual assumption
                if not any(expected in a for a in actual_assumptions):
                    reasons.append(f"expected assumption missing: {expected}")

    return EvalResult(
        case_id=case.id,
        passed=not reasons,
        expected_status=case.expected_status,
        actual_status=actual_status,
        failure_reason="; ".join(reasons),
        latency_ms=round(latency_ms, 3),
        line_no=line_no,
    )


# 模板数值行形如 "- velocity_mps = 1.13177 m/s"（templates.render_docgen_doc 的唯一写法）
_RENDERED_KEY_VALUE_RE = re.compile(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+)")


def _rendered_key_values(rendered: RenderedDoc) -> dict[str, str]:
    """正文里的 ``key = value`` 行（首次出现为准）。"""
    shown: dict[str, str] = {}
    for section in rendered.sections:
        for line in section.lines:
            match = _RENDERED_KEY_VALUE_RE.match(line)
            if match:
                shown.setdefault(match.group(1), match.group(2))
    return shown


def _shows_value(raw: str, expected: float, tolerances: CalcTolerances) -> bool:
    """正文展示值是否就是 ``expected``：模板按 ``:g``（≤6 位有效数字）展示，按展示刻度比。"""
    try:
        shown = float(raw)
    except ValueError:
        return False
    return _within_tolerance(shown, float(f"{expected:g}"), tolerances)


def score_docgen_case(
    case: DocgenCase,
    *,
    rendered: RenderedDoc | None = None,
    render_error: str = "",
    latency_ms: float = 0.0,
    line_no: int | None = None,
) -> EvalResult:
    """文档生成机制层判分（P2）：invalid_input 判定 + 结构合规 + 数值一致 + 引用可追溯。

    与模板引擎同一规则（见 ``docgen/templates.py``，规则来源单一）：

    - ``actual_status``：渲染成功 → ``succeeded``；渲染失败（未知 doc_type 等，
      runner 传 ``render_error``）→ ``invalid_input``；
    - 结构：恒查 ``REQUIRED_SECTIONS[doc_type]``（canonical，I-3 不可被评测配置清空），
      ``case.required_sections`` 只能追加约束；缺失章节 = 失败；
    - 数值：``required_values`` 每键都须在渲染 ``calc_values`` 中找到（双阈值，与
      calc 同构）；非有限值由模板在渲染层拒绝，进不了 ``calc_values``；
    - 引用：``citations_required`` 全部出现 / ``citations_forbidden`` 不得出现；再跑
      ``validate_citation_policy``（与模板自检同一校验器：正文不得出现输入之外条款）；
    - invalid_input 语义：渲染失败且 ``expected_status=invalid_input`` → 判通过（无效
      输入可评测）；渲染失败但期望 ``succeeded`` → 判失败并附 ``render_error``（不把
      实现缺陷伪装成通过）。
    """
    reasons: list[str] = []
    actual_status = "succeeded" if rendered is not None else "invalid_input"
    if case.expected_status != actual_status:
        reasons.append(f"expected status {case.expected_status}, got {actual_status}")
        if rendered is None and render_error:
            reasons.append(f"docgen render error: {render_error}")
    if rendered is None:
        return EvalResult(
            case_id=case.id,
            passed=not reasons,
            expected_status=case.expected_status,
            actual_status=actual_status,
            failure_reason="; ".join(reasons),
            latency_ms=round(latency_ms, 3),
            line_no=line_no,
        )

    if case.expected_status != "succeeded":
        return EvalResult(
            case_id=case.id,
            passed=not reasons,
            expected_status=case.expected_status,
            actual_status=actual_status,
            expected_articles=list(case.citations_required),
            actual_citations=list(rendered.citation_ids),
            failure_reason="; ".join(reasons),
            latency_ms=round(latency_ms, 3),
            line_no=line_no,
        )

    # 结构：canonical 必需章节（按 doc_type 恒查）+ 用例级追加（只能更严，不能清空）
    required_section_names = set(REQUIRED_SECTIONS[rendered.doc_type]) | set(case.required_sections)
    present = {normalize_heading(section.heading) for section in rendered.sections}
    missing = sorted(required_section_names - present)
    if missing:
        reasons.append(f"required sections missing: {missing}")
    # 章节在、正文空 = 什么都没写（模板对每个必需章节至少写一行，缺依据/缺数值也会写明）
    empty = sorted(
        normalize_heading(section.heading)
        for section in rendered.sections
        if normalize_heading(section.heading) in required_section_names and not section.lines
    )
    if empty:
        reasons.append(f"empty section: {empty}")

    values = rendered.calc_values
    for key, gold_value in case.required_values.items():
        actual_value = values.get(key)
        if actual_value is None:
            reasons.append(f"required value key {key!r} missing from rendered calc_values {sorted(values)}")
        elif not _within_tolerance(actual_value, gold_value, case.tolerances):
            reasons.append(
                f"{key}: actual={actual_value:.6f}, gold={gold_value}, "
                f"tol(rel={case.tolerances.relative}, abs={case.tolerances.absolute})"
            )

    # 正文：结构化映射之外，还要核对渲染出来的文字。calc_values / citation_ids 就是输入的
    # 拷贝，只比它们等于同义反复——正文数字全错或被清空也判通过（review 2026-09-30）。
    input_values = {
        key: float(value)
        for key, value in (case.calc_result or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    shown = _rendered_key_values(rendered)
    for key, raw in shown.items():
        if key not in input_values:
            reasons.append(f"rendered value for a key not in the input: {key} = {raw}")
        elif not _shows_value(raw, input_values[key], case.tolerances):
            reasons.append(f"rendered value mismatch: {key} = {raw}（输入 {input_values[key]:g}）")
    for key in case.required_values:
        if key not in shown:
            reasons.append(f"required value {key!r} not shown in the rendered text")
        elif not _shows_value(shown[key], case.required_values[key], case.tolerances):
            reasons.append(
                f"rendered value mismatch: {key} = {shown[key]}（gold {case.required_values[key]:g}）"
            )

    actual_ids = rendered.citation_ids
    basis_text = "\n".join(
        line
        for section in rendered.sections
        if normalize_heading(section.heading) == "设计依据"
        for line in section.lines
    )
    for required in case.citations_required:
        if required not in actual_ids:
            reasons.append(f"required citation not rendered: {required}")
        elif required not in basis_text:
            reasons.append(f"required citation not rendered under 设计依据: {required}")
    rendered_text = rendered.text
    for forbidden in case.citations_forbidden:
        if forbidden in actual_ids or forbidden in rendered_text:
            reasons.append(f"forbidden citation rendered: {forbidden}")

    for problem in validate_citation_policy(rendered):
        reasons.append(f"citation policy: {problem}")

    return EvalResult(
        case_id=case.id,
        passed=not reasons,
        expected_status=case.expected_status,
        actual_status=actual_status,
        expected_articles=list(case.citations_required),
        actual_citations=list(actual_ids),
        failure_reason="; ".join(reasons),
        latency_ms=round(latency_ms, 3),
        line_no=line_no,
    )


def score_docgen_quality_case(
    case: DocgenQualityCase,
    *,
    verdict: PolishVerdict,
    latency_ms: float = 0.0,
    line_no: int | None = None,
) -> EvalResult:
    """docgen 质量层判分：期望结论与实际 pass/fail/blocked 分离。

    ``blocked`` 不是 pass。只有无 Key 的前置条件缺失才带 ``skipped=True``；有 Key但
    调用失败或模型破坏结构时保留 ``actual_status=blocked`` 并计为失败，便于命令退出码
    和证据文档如实表达阻塞。
    """
    actual = verdict.verdict
    reasons: list[str] = []
    if actual != case.expected_verdict and not verdict.skipped:
        reasons.append(f"expected verdict {case.expected_verdict}, got {actual}")
    if actual == "fail" and actual != case.expected_verdict:
        reasons.extend(verdict.problems)
    if actual == "blocked":
        reasons.append(verdict.blocked_reason or "docgen quality blocked")

    # blocked 永远不是通过：即使某条测试显式期望 blocked，也只能验证状态语义，
    # 不能把外部依赖不可用计入 passed。
    counted_pass = actual == case.expected_verdict and actual != "blocked" and not verdict.skipped
    return EvalResult(
        case_id=case.id,
        passed=counted_pass,
        expected_status=case.expected_verdict,
        actual_status=actual,
        actual_citations=list(verdict.polished.citation_ids) if verdict.polished else [],
        # docgen 没有 RAG context；质量层的可追溯边界就是结构化底座允许的 citation_ids。
        # 仅用于通用 citation_ok 汇总，不宣称发生过真实检索。
        context_articles=list(verdict.polished.citation_ids) if verdict.polished else [],
        failure_reason="; ".join(reason for reason in reasons if reason),
        latency_ms=round(latency_ms, 3),
        line_no=line_no,
        skipped=verdict.skipped,
        quality_fail_reasons=[
            *verdict.problems,
            *([verdict.blocked_reason] if verdict.blocked_reason else []),
        ],
        quality_output_text=(
            verdict.polished.text if verdict.polished is not None else verdict.raw_output
        ),
        quality_used_real_model=verdict.used_real_model,
    )


def score_governance_case(
    case: GovernanceCase,
    *,
    outcome: Any,
    latency_ms: float = 0.0,
    line_no: int | None = None,
) -> EvalResult:
    """治理评测判分（P2.5 Step 15）。

    判分纪律：

    1. **场景执行异常一律失败**，绝不当 SKIP —— 本套件全离线，不存在"缺前置
       条件"的合法理由。执行不起来就是真的坏了。
    2. ``expected_code`` 非空时**精确匹配**。只断言"被拒了"会让规则退化时假绿：
       被别的原因偶然拒掉也算通过，测试就失去区分力（与 Step 14 红队数据集同源纪律）。
    3. ``expect_evidence_true/false`` 逐键校验，缺键即失败 —— 键缺失说明场景
       没有产出该证据，而不是"证据为假"，两者都不该算通过。
    """
    reasons: list[str] = []

    if getattr(outcome, "error", ""):
        reasons.append(f"场景执行异常：{outcome.error}")
        return EvalResult(
            case_id=case.id,
            passed=False,
            expected_status=case.expected_observed,
            actual_status="error",
            failure_reason="; ".join(reasons),
            latency_ms=round(latency_ms, 3),
            line_no=line_no,
            skipped=False,  # 治理套件永不 SKIP
        )

    observed = str(getattr(outcome, "observed", "") or "")
    code = str(getattr(outcome, "code", "") or "")
    evidence = dict(getattr(outcome, "evidence", {}) or {})

    if observed != case.expected_observed:
        reasons.append(f"expected observed {case.expected_observed!r}, got {observed!r}")
    if case.expected_code and code != case.expected_code:
        reasons.append(f"expected code {case.expected_code!r}, got {code!r}")

    for key in case.expect_evidence_true:
        if key not in evidence:
            reasons.append(f"证据缺少键 {key!r}（期望为真）")
        elif not evidence[key]:
            reasons.append(f"证据 {key!r} 期望为真，实际 {evidence[key]!r}")
    for key in case.expect_evidence_false:
        if key not in evidence:
            reasons.append(f"证据缺少键 {key!r}（期望为假）")
        elif evidence[key]:
            reasons.append(f"证据 {key!r} 期望为假，实际 {evidence[key]!r}")

    return EvalResult(
        case_id=case.id,
        passed=not reasons,
        expected_status=case.expected_observed,
        actual_status=observed,
        failure_reason="; ".join(reasons),
        latency_ms=round(latency_ms, 3),
        line_no=line_no,
        skipped=False,
    )


def _material_row_label(key: tuple[str, str, str], quantity: float) -> str:
    material, spec, unit = key
    return f"{material} {spec}：{quantity:g} {unit}"


def _expected_group_sums(segments: list[dict[str, Any]]) -> dict[tuple[str, str], float] | None:
    """从原始管段表按 (材质, 规格) 重算组内长度和，作为 ``raw_quantity`` 的口径审计基准。

    只会在引擎已判 ``succeeded`` 的表上调用（输入必然可解析）；若某段解析异常则返回
    None（放弃该项审计，不误报）。
    """
    sums: dict[tuple[str, str], float] = {}
    for segment in segments:
        dn_value = segment.get("dn_mm")
        length_value = segment.get("length_m")
        if dn_value is None or length_value is None:
            return None
        try:
            material = str(segment.get("material", "") or "").strip()
            spec = f"DN{int(dn_value)}"
            length = float(length_value)
        except (TypeError, ValueError):
            return None
        if not material or not spec:
            return None
        sums[(material, spec)] = sums.get((material, spec), 0.0) + length
    return sums


def score_material_case(
    case: MaterialCase,
    *,
    table: MaterialTable,
    latency_ms: float = 0.0,
    line_no: int | None = None,
) -> EvalResult:
    """材料统计机制层判分（P2）：行集合 + 数量 + 溯源 + 口径审计，全部与引擎输出一致。

    - 行集合：每个 (material, spec, unit) 都是 gold 里声明的行，且无额外行（材料行由
      管段表确定性推出，出现 gold 之外的行 = 漏/多计）；
    - 数量：逐 gold 行比对（relative/absolute 双阈值，同 calc 口径）；
    - 溯源：每条输入管段的 segment_id 必须出现在某行的 ``segments``（防静默丢弃）；
      每行的 ``detail`` 必须覆盖其 ``segments`` 全部段号（逐段明细可见）；
    - 口径审计：``raw_quantity`` 必须等于从原始管段表按 (材质, 规格) 重算的组内长度和
      （I-4 单一舍入口径之外的原始和不允许漂移）；
    - invalid_input 语义：引擎拒绝（表不 ok）→ ``invalid_input``；此类用例不得携带
      ``gold_rows``（非法输入不可能产出合法行，携带即数据矛盾）。
    """
    reasons: list[str] = []
    actual_status = "succeeded" if table.ok else "invalid_input"
    if case.expected_status != actual_status:
        reasons.append(f"expected status {case.expected_status}, got {actual_status}")
        if not table.ok:
            reasons.append(f"material engine errors: {table.errors}")

    if case.expected_status == "invalid_input" and case.gold_rows:
        specs = [str(row.get("spec", "")) for row in case.gold_rows]
        reasons.append(f"invalid_input case must not carry gold_rows（非法输入无合法行可期望）: {specs}")

    if case.expected_status == "succeeded" and table.ok:
        actual_by_key = {(row.material, row.spec, row.unit): row for row in table.rows}
        gold_by_key: dict[tuple[str, str, str], float] = {}
        for gold in case.gold_rows:
            key = (str(gold.get("material", "")), str(gold.get("spec", "")), str(gold.get("unit", "m")))
            gold_by_key[key] = float(gold.get("quantity", 0.0))

        # 按键集合比，不按行数比：行数相同时一行被改了规格/材质（数量随之错）也必须报出。
        extra = sorted(set(actual_by_key) - set(gold_by_key))
        missing = sorted(set(gold_by_key) - set(actual_by_key))
        if extra or missing:
            reasons.append(
                "row set mismatch: "
                f"extra={[_material_row_label(key, actual_by_key[key].quantity) for key in extra]}, "
                f"missing={[_material_row_label(key, gold_by_key[key]) for key in missing]}"
            )
        for key, gold_qty in gold_by_key.items():
            actual_row = actual_by_key.get(key)
            if actual_row is None:
                continue  # 已在 row set mismatch 里点名
            if not _within_tolerance(actual_row.quantity, gold_qty, case.tolerances):
                reasons.append(
                    f"{key[0]} {key[1]}: actual={actual_row.quantity}, gold={gold_qty}, "
                    f"tol(rel={case.tolerances.relative}, abs={case.tolerances.absolute})"
                )

        segment_ids = {
            str(seg.get("segment_id", "")).strip()
            for seg in case.segments
            if str(seg.get("segment_id", "")).strip()
        }
        covered = {sid for row in table.rows for sid in row.segments}
        uncovered = sorted(segment_ids - covered)
        if uncovered:
            reasons.append(f"segments not covered by any material row (silent drop): {uncovered}")

        for row in table.rows:
            lost = [sid for sid in row.segments if sid not in row.detail]
            if not row.detail or lost:
                reasons.append(
                    f"row {row.material} {row.spec}: detail 溯源不完整"
                    f"（segments={row.segments}, detail={row.detail!r}, 缺={lost}）"
                )

        expected_sums = _expected_group_sums(case.segments)
        if expected_sums is not None:
            for row in table.rows:
                expected_raw = expected_sums.get((row.material, row.spec))
                if expected_raw is None:
                    reasons.append(
                        f"row {row.material} {row.spec}: 管段表中没有这一 (材质, 规格) 分组（口径审计）"
                    )
                elif not _within_tolerance(row.raw_quantity, expected_raw, case.tolerances):
                    reasons.append(
                        f"row {row.material} {row.spec}: raw_quantity={row.raw_quantity} 与管段表重算"
                        f" {expected_raw} 不一致（口径审计）"
                    )

    return EvalResult(
        case_id=case.id,
        passed=not reasons,
        expected_status=case.expected_status,
        actual_status=actual_status,
        failure_reason="; ".join(reasons),
        latency_ms=round(latency_ms, 3),
        line_no=line_no,
    )


def score_case(
    case: EvalCase | CalcCase | DocgenCase | MaterialCase | DocgenQualityCase | GovernanceCase,
    *,
    calc_result: CalcResult | None = None,
    rendered: RenderedDoc | None = None,
    render_error: str = "",
    table: MaterialTable | None = None,
    quality_verdict: PolishVerdict | None = None,
    governance_outcome: Any | None = None,
    actual_status: str = "",
    actual_citations: Iterable[str] | None = None,
    context_articles: Iterable[str] | None = None,
    latency_ms: float = 0.0,
    trace_id: str = "",
    retry_count: int = 0,
    line_no: int | None = None,
) -> EvalResult:
    if isinstance(case, CalcCase):
        if calc_result is None:
            calc_result = run_calc(case.formula, case.input)
        return score_calc_case(case, calc_result=calc_result, latency_ms=latency_ms, line_no=line_no)
    if isinstance(case, DocgenCase):
        return score_docgen_case(
            case,
            rendered=rendered,
            render_error=render_error,
            latency_ms=latency_ms,
            line_no=line_no,
        )
    if isinstance(case, DocgenQualityCase):
        if quality_verdict is None:
            raise ValueError(f"docgen quality case {case.id} 需要 quality_verdict 才能判分")
        return score_docgen_quality_case(
            case,
            verdict=quality_verdict,
            latency_ms=latency_ms,
            line_no=line_no,
        )
    if isinstance(case, MaterialCase):
        if table is None:
            raise ValueError(f"material case {case.id} 需要 table 才能判分")
        return score_material_case(case, table=table, latency_ms=latency_ms, line_no=line_no)
    if isinstance(case, GovernanceCase):
        if governance_outcome is None:
            # 不静默放过：缺 outcome 说明 runner 没驱动场景，那是接线缺陷。
            raise ValueError(f"governance case {case.id} 需要 governance_outcome 才能判分")
        return score_governance_case(
            case, outcome=governance_outcome, latency_ms=latency_ms, line_no=line_no
        )
    return score_eval_case(
        case,
        actual_status=actual_status,
        actual_citations=actual_citations or [],
        context_articles=context_articles,
        latency_ms=latency_ms,
        trace_id=trace_id,
        retry_count=retry_count,
        line_no=line_no,
    )

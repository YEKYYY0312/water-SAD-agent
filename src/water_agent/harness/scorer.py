"""确定性评测判分器。"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from water_agent.docgen import RenderedDoc, validate_citation_policy
from water_agent.domain.models import (
    CalcCase,
    CalcTolerances,
    DocgenCase,
    EvalCase,
    EvalResult,
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
) -> EvalResult:
    actual_citation_list = _dedupe(actual_citations)
    context_article_list = _dedupe(context_articles or [])
    expected_articles = _dedupe([*case.gold_articles_any, *case.gold_articles_all])

    citation_set = set(actual_citation_list)
    context_set = set(context_article_list)
    invalid_citations = [citation for citation in actual_citation_list if citation not in context_set]

    gold_any_hit = [article for article in case.gold_articles_any if article in citation_set]
    gold_all_hit = [article for article in case.gold_articles_all if article in citation_set]
    forbidden_hit = [article for article in case.forbidden_articles if article in citation_set]

    reasons: list[str] = []
    if actual_status != case.expected_status:
        reasons.append(f"expected status {case.expected_status}, got {actual_status}")
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


def _within_tolerance(actual: float, gold: float, tolerances: CalcTolerances) -> bool:
    abs_err = abs(actual - gold)
    rel_err = abs_err / abs(gold) if gold else 0.0
    return abs_err <= tolerances.absolute and rel_err <= tolerances.relative


def score_calc_case(
    case: CalcCase,
    *,
    calc_result: CalcResult,
    latency_ms: float = 0.0,
    line_no: int | None = None,
) -> EvalResult:
    """程序化数值判分：状态匹配 + 每个 gold 键双阈值比对。

    - 缺参用例：gold 为空 + expected_status == "missing_inputs" 时按通过计；
    - 非法输入用例：gold 为空 + expected_status == "invalid_input" 时按通过计；
    - 正常用例：expected_status == "succeeded"，逐键数值比对。
    """
    actual_status = "succeeded" if calc_result.ok else ("missing_inputs" if calc_result.missing_inputs else "invalid_input")
    reasons: list[str] = []
    values = calc_result.values

    if actual_status != case.expected_status:
        reasons.append(f"expected status {case.expected_status}, got {actual_status}")
        reasons.append(f"tool detail: missing_inputs={calc_result.missing_inputs} errors={calc_result.errors}")
    elif calc_result.ok:
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


def score_docgen_case(
    case: DocgenCase,
    *,
    rendered: RenderedDoc,
    latency_ms: float = 0.0,
    line_no: int | None = None,
) -> EvalResult:
    """文档生成机制层判分（P2）：结构合规 + 数值一致 + 引用可追溯。

    - 必需章节齐全（工程概况/设计依据/…，按 doc_type 口径表）；
    - ``required_values`` 每个键都能在渲染的 calc_values 中找到（双阈值比对，与 calc 同构）；
    - ``citations_required`` 必须全部出现在文档；``citations_forbidden`` 不得出现；
    - 引用策略校验：正文不得出现输入之外的 source_id（模板禁止编造条款）。
    """
    reasons: list[str] = []
    actual_status = "succeeded"
    if case.expected_status != "succeeded":
        reasons.append(f"expected status {case.expected_status}, got {actual_status}")

    if case.required_sections:
        present = set(rendered.section_keys())
        still_missing = [name for name in case.required_sections if name not in present]
        if still_missing:
            reasons.append(f"required sections missing: {still_missing}")

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

    actual_ids = rendered.citation_ids
    for required in case.citations_required:
        if required not in actual_ids:
            reasons.append(f"required citation not rendered: {required}")
    for forbidden in case.citations_forbidden:
        if forbidden in actual_ids:
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


def score_material_case(
    case: MaterialCase,
    *,
    table: MaterialTable,
    latency_ms: float = 0.0,
    line_no: int | None = None,
) -> EvalResult:
    """材料统计机制层判分（P2）：行集合精确一致 + 数量双阈值 + 全段溯源覆盖。

    - 行集合：每个 (material, spec, unit) 都是 gold 里声明的行，且无额外行（材料行
      由管段表确定性推出，出现 gold 之外的行 = 漏/多计）；
    - 数量：逐 gold 行比对（relative/absolute 双阈值）；
    - 溯源：每条输入管段的 segment_id 必须出现在某行的 segments（保证无静默丢弃）。
    """
    reasons: list[str] = []
    if not table.ok and case.expected_status == "succeeded":
        reasons.append(f"material engine errors: {table.errors}")

    actual_status = "succeeded" if table.ok else "invalid_input"
    if case.expected_status != actual_status:
        reasons.append(f"expected status {case.expected_status}, got {actual_status}")

    if case.expected_status == "succeeded" and table.ok:
        actual_by_key = {(row.material, row.spec, row.unit): row for row in table.rows}
        gold_by_key: dict[tuple[str, str, str], float] = {}
        for gold in case.gold_rows:
            key = (str(gold.get("material", "")), str(gold.get("spec", "")), str(gold.get("unit", "m")))
            gold_by_key[key] = float(gold.get("quantity", 0.0))

        if len(actual_by_key) != len(gold_by_key):
            extra = sorted(set(actual_by_key) - set(gold_by_key))
            missing = sorted(set(gold_by_key) - set(actual_by_key))
            reasons.append(f"row set mismatch: extra={extra}, missing={missing}")
        for key, gold_qty in gold_by_key.items():
            actual_row = actual_by_key.get(key)
            if actual_row is None:
                continue
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
    case: EvalCase | CalcCase | DocgenCase | MaterialCase,
    *,
    calc_result: CalcResult | None = None,
    rendered: RenderedDoc | None = None,
    table: MaterialTable | None = None,
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
        if rendered is None:
            raise ValueError(f"docgen case {case.id} 需要 rendered 才能判分")
        return score_docgen_case(case, rendered=rendered, latency_ms=latency_ms, line_no=line_no)
    if isinstance(case, MaterialCase):
        if table is None:
            raise ValueError(f"material case {case.id} 需要 table 才能判分")
        return score_material_case(case, table=table, latency_ms=latency_ms, line_no=line_no)
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

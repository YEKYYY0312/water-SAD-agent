"""真实质量层判定阈值与阻断维度（单一配置位置）。

P1/Step 10 评审：judge_scores 过去只记录、不影响任何质量结论；本模块把"质量是否
通过"的判定规则集中在这里，任何阈值调整必须改本文件并同步更新文档。

阈值依据（锚定刻度见 ``harness/llm_judge.py::JUDGE_RUBRIC``）：
- 阻断维度 = rubric 中机制层无法覆盖的内容层面。确定性校验器只检查 cite_clauses
  声明的 source_id 是否 ∈ 检索上下文（"引了没引/引错没有"），无法发现正文层面的
  编造、忠实度偏差或"答非所引"——因此 faithfulness（忠实度）、citation_coverage
  （引用覆盖）、no_fabrication（不编造）作为质量阻断维度；
- ``JUDGE_QUALITY_PASS_SCORE = 4``：锚定刻度 4 = "绝大多数陈述基于上下文，有少量
  合理推断但不改变事实"；3 = "部分陈述与上下文不一致 / 个别规范号或条款数与检索
  结果不一致"——属材料性缺陷，不得计为质量通过；
- completeness / clarity 属于改善性维度：低分只进平均值与报告，不阻断质量通过，
  也不改写确定性机制判分。
"""
from __future__ import annotations

from collections.abc import Mapping

# 阻断维度：任一评分 < JUDGE_QUALITY_PASS_SCORE 即质量不通过
BLOCKING_JUDGE_DIMENSIONS: tuple[str, ...] = (
    "faithfulness",
    "citation_coverage",
    "no_fabrication",
)
# 阻断维度须 ≥4/5（见模块 docstring 依据）
JUDGE_QUALITY_PASS_SCORE = 4
# 报告"低分用例"口径：阻断维度低于该值即 quality_pass=False
JUDGE_LOW_SCORE = 3


def judge_quality_verdict(scores: Mapping[str, int]) -> tuple[bool | None, list[str]]:
    """由 judge 各维度评分给出独立质量判定（不影响机制层 passed）。

    Args:
        scores: judge 逐维评分 {维度: 1-5}；空 dict 表示 judge 未运行/未给出评分。

    Returns:
        (verdict, reasons)：
        - verdict True = 阻断维度全部 ≥ JUDGE_QUALITY_PASS_SCORE；
        - verdict False = 有阻断维度缺失或低于阈值（reasons 给出明确原因）；
        - verdict None = 无评分（judge SKIP/调用失败），不做质量结论。
    """
    if not scores:
        return None, ["judge 未返回评分，质量判定未执行（无 Key / 调用失败 / 未启用 --judge）"]
    reasons: list[str] = []
    missing = [d for d in BLOCKING_JUDGE_DIMENSIONS if d not in scores]
    if missing:
        reasons.append(f"judge 缺少阻断维度评分：{', '.join(missing)}")
    low = [
        d for d in BLOCKING_JUDGE_DIMENSIONS
        if d in scores and int(scores[d]) < JUDGE_QUALITY_PASS_SCORE
    ]
    if low:
        reasons.append(
            "阻断维度低于 {}/5：{}".format(
                JUDGE_QUALITY_PASS_SCORE,
                "、".join(f"{d}={int(scores[d])}" for d in low),
            )
        )
    return (not reasons), reasons

"""LLM 调用成本估算（Step 10 成本记账）。

以 DeepSeek 公开单价为默认口径（USD / 1M tokens）：
- deepseek-chat 输入 $0.27 / 1M，输出 $1.10 / 1M；
- 缓存命中输入 $0.07 / 1M（本工程暂不依赖，预留常量）。

成本字段用于双轨评测的 ``EvalResult.token_usage.cost_usd`` 与汇总报告
``EvalReport.total_cost_usd``；估算值仅作记账参考，不替代账单。
"""
from __future__ import annotations

DEEPSEEK_INPUT_PER_1M = 0.27
DEEPSEEK_OUTPUT_PER_1M = 1.10
DEEPSEEK_CACHED_INPUT_PER_1M = 0.07


def estimate_deepseek_cost(input_tokens: int, output_tokens: int) -> float:
    """估算一次 DeepSeek 调用的成本（USD，保留 8 位）。"""
    cost = (
        max(0, input_tokens) * DEEPSEEK_INPUT_PER_1M
        + max(0, output_tokens) * DEEPSEEK_OUTPUT_PER_1M
    ) / 1_000_000
    return round(cost, 8)


def tokens_to_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """别名：与 ``estimate_deepseek_cost`` 等价，语义更直白。"""
    return estimate_deepseek_cost(input_tokens, output_tokens)

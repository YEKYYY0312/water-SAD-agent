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


def estimate_model_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """按模型估算一次调用的成本（USD，8 位）；**不知道价格就返回 ``None``，不估算**（P7-S15）。

    - 目录（``model_catalog``）之外的模型：与此前逐字相同，沿用 DeepSeek 公开单价口径（评测 / 脚本模型 / 缺省
      ``deepseek-chat`` 的行为不变）；
    - 目录里的模型：先查治理层价格表（``deepseek-chat`` / ``deepseek-reasoner`` 已登记），再查目录项声明的估算单价
      （永远视为未核对）；两者都没有 → ``None``。把别家模型按 deepseek-chat 的价格记账就是在编造成本，
      聚合处（``aggregate_metrics``）把 ``None`` 当"未知"跳过，不记 0。
    """

    from water_agent.generation.model_catalog import entry_for
    from water_agent.runtime.governance.config import DEFAULT_GOVERNANCE_CONFIG, ModelPrice

    entry = entry_for(model)
    if entry is None:
        return estimate_deepseek_cost(input_tokens, output_tokens)
    price = DEFAULT_GOVERNANCE_CONFIG.price_for(model)
    if price is None and entry.price is not None:
        price = ModelPrice(
            input_per_1m=entry.price.input_per_1m,
            output_per_1m=entry.price.output_per_1m,
            cached_input_per_1m=entry.price.cached_input_per_1m,
            confirmed=False,
        )
    return None if price is None else price.estimate_usd(input_tokens, output_tokens)


def tokens_to_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """别名：与 ``estimate_deepseek_cost`` 等价，语义更直白。"""
    return estimate_deepseek_cost(input_tokens, output_tokens)

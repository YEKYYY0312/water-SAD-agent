"""LLM-as-judge 回答质量评估器（Step 10）。

对 RAG 回答按 rubric 多准则评分（1-5 分 + 理由），judge 模型可配置；
结果缓存 jsonl 防重复扣费；无 Key 时输出结构化 SKIP。

Rubric 准则（3-5 条，锚定刻度）：
1. faithfulness（忠实度）：回答是否严格基于检索上下文，不编造事实
2. citation_coverage（引用覆盖）：关键主张是否都有对应引用标记
3. completeness（完整性）：是否充分回答了用户问题
4. clarity（清晰度）：表达是否专业、清晰、简洁
5. no_fabrication（不编造）：是否包含规范号/条款/数值的编造

设计遵循 skill-eval-patterns.md：锚定刻度、点式评分、结果缓存。
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from water_agent.domain.models import TokenUsage

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_JUDGE_CACHE_DIR = REPO_ROOT / "data" / "eval" / "judge_cache"
DEFAULT_JUDGE_MODEL = "deepseek-chat"
DEFAULT_JUDGE_BASE_URL = "https://api.deepseek.com"
DEFAULT_JUDGE_API_KEY_ENV = "DEEPSEEK_API_KEY"


# ─── Rubric 定义 ────────────────────────────────────────────────────

JUDGE_RUBRIC: dict[str, str] = {
    "faithfulness": (
        "5: 所有陈述均严格基于检索上下文，无任何编造或推断。\n"
        "4: 绝大多数陈述基于上下文，有少量合理推断但不改变事实。\n"
        "3: 部分陈述有上下文依据，但存在一些与上下文不一致的表述。\n"
        "2: 多处关键陈述与上下文不符，或明显编造了条款内容。\n"
        "1: 回答几乎完全脱离检索上下文，大量编造信息。"
    ),
    "citation_coverage": (
        "5: 每个关键主张都有对应的引用标记，引用格式规范。\n"
        "4: 大部分关键主张有引用，遗漏了少量次要主张。\n"
        "3: 约半数关键主张有引用，部分引用不准确。\n"
        "2: 仅有少数引用，多数关键主张无引用支撑。\n"
        "1: 无任何引用标记，或引用完全与内容无关。"
    ),
    "completeness": (
        "5: 全面、准确地回答了用户问题的所有方面。\n"
        "4: 回答了主要方面，遗漏了次要细节或未明确说明的方面。\n"
        "3: 回答了部分内容，但关键方面被遗漏。\n"
        "2: 仅触及问题表面，大量关键方面未回答。\n"
        "1: 完全未回答用户问题，或答非所问。"
    ),
    "clarity": (
        "5: 表达专业、结构清晰、语言简洁，易于理解。\n"
        "4: 表达较清晰，有少量专业术语未解释或结构稍显松散。\n"
        "3: 表达基本清晰，但存在冗余或不够简洁之处。\n"
        "2: 表达模糊、结构混乱，或使用了不恰当的术语。\n"
        "1: 几乎无法理解，语言表达严重问题。"
    ),
    "no_fabrication": (
        "5: 未发现任何编造，所有规范号/条款/数值均可追溯。\n"
        "4: 极少量格式上的不准确，但不影响事实正确性。\n"
        "3: 有个别规范号或条款数与检索结果不一致。\n"
        "2: 有多处编造，包括不存在的规范号或条款内容。\n"
        "1: 大量编造规范号、条款或数值，完全不可信。"
    ),
}

DEFAULT_CRITERIA = list(JUDGE_RUBRIC.keys())

JUDGE_SYSTEM_PROMPT = (
    "你是给排水专业 AI 回答质量评审员。请严格依据提供的检索上下文和锚定刻度准则，"
    "对 AI 助手的回答进行逐项评分。评分要严格、客观，避免中庸倾向——"
    "好的回答给高分，差的回答给低分，不要倾向于给 3 分。"
    "输出必须为 JSON 格式，包含每个准则的评分（1-5 整数）和一句话理由。"
)


# ─── 数据模型 ────────────────────────────────────────────────────────


class JudgeInput(BaseModel):
    """单次 judge 请求的输入。"""

    query: str
    answer: str
    context: list[str] = Field(default_factory=list)
    criteria: list[str] = Field(default_factory=lambda: list(DEFAULT_CRITERIA))


class JudgeScore(BaseModel):
    """单条准则的评分结果。"""

    score: int = Field(ge=1, le=5)
    reasoning: str = ""


class JudgeResult(BaseModel):
    """单次 judge 的完整结果。"""

    scores: dict[str, JudgeScore] = Field(default_factory=dict)
    overall_score: float = 0.0
    token_usage: TokenUsage | None = None
    latency_ms: float = 0.0
    cache_hit: bool = False
    skipped: bool = False
    error: str = ""


# ─── 缓存键 ──────────────────────────────────────────────────────────


def _judge_cache_key(input_data: JudgeInput, judge_model: str) -> str:
    """稳定缓存键：sha256(query + answer + context + model + criteria)。

    context 必须入键：相同 query+answer 但检索上下文不同（换 rerank/召回）时，
    faithfulness/citation_coverage 判据不同，不得复用同一条缓存评分。
    """
    raw = json.dumps(
        {
            "query": input_data.query,
            "answer": input_data.answer,
            "context": list(input_data.context),
            "criteria": list(input_data.criteria),
            "model": judge_model,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ─── LLM Judge 实现 ──────────────────────────────────────────────────


class LLMJudge:
    """LLM-as-judge 回答质量评估器。

    用法：
        judge = LLMJudge()
        result = judge.evaluate(query="...", answer="...", context=[...])
        print(result.scores, result.overall_score)
    """

    def __init__(
        self,
        *,
        judge_model: str = DEFAULT_JUDGE_MODEL,
        judge_base_url: str = DEFAULT_JUDGE_BASE_URL,
        judge_api_key_env: str = DEFAULT_JUDGE_API_KEY_ENV,
        criteria: list[str] | None = None,
        cache_dir: str | Path = DEFAULT_JUDGE_CACHE_DIR,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.judge_model = judge_model
        self.judge_base_url = judge_base_url
        self.judge_api_key_env = judge_api_key_env
        self.criteria = list(criteria) if criteria else list(DEFAULT_CRITERIA)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._transport = transport
        self._api_key: str | None = None

    def _resolve_api_key(self) -> str | None:
        if self._api_key is not None:
            return self._api_key
        raw = os.environ.get(self.judge_api_key_env, "")
        self._api_key = raw.strip() or None
        return self._api_key

    def _build_prompt(self, input_data: JudgeInput) -> list[dict[str, str]]:
        """构建 judge 的系统+用户消息。"""
        context_section = ""
        if input_data.context:
            context_section = "检索上下文：\n" + "\n---\n".join(
                f"[{i + 1}] {text[:500]}" for i, text in enumerate(input_data.context)
            )

        rubric_section = "\n\n".join(
            f"## {criterion}\n{JUDGE_RUBRIC[criterion]}"
            for criterion in input_data.criteria
        )

        user_message = (
            f"问题：{input_data.query}\n\n"
            f"AI 回答：{input_data.answer}\n\n"
            f"{context_section}\n\n"
            f"评分准则：\n{rubric_section}\n\n"
            "请为每个准则评分（1-5 整数），并给出理由。"
            "输出 JSON 格式：{\"scores\": {\"准则名\": {\"score\": 整数, \"reasoning\": \"理由\"}}, "
            "\"overall_reasoning\": \"总体评价\"}"
        )

        return [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

    def _check_cache(self, cache_key: str) -> JudgeResult | None:
        """从缓存 jsonl 查找已缓存的 judge 结果。"""
        cache_path = self.cache_dir / "judge_cache.jsonl"
        if not cache_path.exists():
            return None
        try:
            with cache_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    if record.get("cache_key") == cache_key:
                        result = JudgeResult(**record["result"])
                        result.cache_hit = True
                        return result
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass
        return None

    def _save_cache(self, cache_key: str, result: JudgeResult) -> None:
        """将 judge 结果写入缓存 jsonl。"""
        cache_path = self.cache_dir / "judge_cache.jsonl"
        try:
            with cache_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {"cache_key": cache_key, "result": result.model_dump(mode="json")},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass  # 缓存写入失败不阻塞评测

    def _call_llm(self, messages: list[dict[str, str]]) -> tuple[str, TokenUsage | None]:
        """调用 LLM 获取 judge 结果。使用 ChatOpenAI 风格接口。"""
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        api_key = self._resolve_api_key()
        if not api_key:
            raise ValueError(f"{self.judge_api_key_env} must not be empty")

        llm = ChatOpenAI(
            model=self.judge_model,
            base_url=self.judge_base_url,
            api_key=SecretStr(api_key),
            temperature=0.0,
        )
        response = llm.invoke(messages)

        token_usage = None
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            meta = response.usage_metadata
            input_tokens = getattr(meta, "input_tokens", 0) or 0
            output_tokens = getattr(meta, "output_tokens", 0) or 0
            from water_agent.generation.cost import estimate_deepseek_cost
            token_usage = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cost_usd=estimate_deepseek_cost(input_tokens, output_tokens),
            )

        if not hasattr(response, "content") or not response.content:
            raise ValueError("judge LLM returned empty response")

        return str(response.content), token_usage

    def _parse_response(self, content: str) -> dict[str, Any]:
        """从 LLM 响应中解析 JSON。"""
        content = content.strip()
        # 查找 JSON 块
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            content = content[start : end + 1]
        try:
            return json.loads(content)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            raise ValueError(f"judge LLM did not return valid JSON: {content[:200]}")

    def evaluate(
        self,
        query: str,
        answer: str,
        *,
        context: list[str] | None = None,
        criteria: list[str] | None = None,
        skip_on_missing_key: bool = True,
    ) -> JudgeResult:
        """评估单条回答的质量。

        Args:
            query: 用户问题
            answer: AI 回答文本
            context: 检索到的上下文文本列表（用作忠实度评判依据）
            criteria: 要评估的准则列表（默认 5 条）
            skip_on_missing_key: 无 API Key 时是否 SKIP（默认 True）

        Returns:
            JudgeResult 包含逐项评分、总分、token 用量等
        """
        active_criteria = list(criteria) if criteria else list(self.criteria)
        input_data = JudgeInput(
            query=query,
            answer=answer,
            context=context or [],
            criteria=active_criteria,
        )

        # 无 Key 时 SKIP
        if skip_on_missing_key and not self._resolve_api_key():
            return JudgeResult(
                skipped=True,
                error=f"{self.judge_api_key_env} 未配置，judge SKIP",
            )

        # 检查缓存
        cache_key = _judge_cache_key(input_data, self.judge_model)
        cached = self._check_cache(cache_key)
        if cached is not None:
            return cached

        # 调用 LLM
        import time

        try:
            messages = self._build_prompt(input_data)
            started = time.perf_counter()
            content, token_usage = self._call_llm(messages)
            latency_ms = (time.perf_counter() - started) * 1000
            parsed = self._parse_response(content)
        except Exception as exc:  # noqa: BLE001 — judge 调用失败不抛异常，结构化返回
            return JudgeResult(
                error=f"judge LLM call failed: {exc}",
                token_usage=None,
                latency_ms=0.0,
            )

        # 解析评分
        scores: dict[str, JudgeScore] = {}
        raw_scores = parsed.get("scores", {})
        for criterion in active_criteria:
            entry = raw_scores.get(criterion, {})
            if isinstance(entry, dict):
                score = int(entry.get("score", 3))
                score = max(1, min(5, score))  # 裁剪到 1-5
                reasoning = str(entry.get("reasoning", ""))
            elif isinstance(entry, (int, float)):
                score = max(1, min(5, int(entry)))
                reasoning = ""
            else:
                score = 3
                reasoning = ""
            scores[criterion] = JudgeScore(score=score, reasoning=reasoning)

        # 计算总分（各准则平均）
        overall = sum(s.score for s in scores.values()) / len(scores) if scores else 0.0

        result = JudgeResult(
            scores=scores,
            overall_score=round(overall, 2),
            token_usage=token_usage,
            latency_ms=round(latency_ms, 2),
            cache_hit=False,
        )

        # 缓存结果
        self._save_cache(cache_key, result)

        return result


# ─── Fake Judge（测试用） ────────────────────────────────────────────


class FakeJudge(LLMJudge):
    """确定性假 judge，用于离线测试。

    所有准则返回固定分数（默认 4 分），不调用任何 LLM。
    """

    def __init__(self, *, fixed_score: int = 4, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fixed_score = max(1, min(5, fixed_score))
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def evaluate(
        self,
        query: str,
        answer: str,
        *,
        context: list[str] | None = None,
        criteria: list[str] | None = None,
        skip_on_missing_key: bool = True,
    ) -> JudgeResult:
        active_criteria = list(criteria) if criteria else list(self.criteria)
        self._call_count += 1
        scores = {
            criterion: JudgeScore(
                score=self.fixed_score,
                reasoning=f"FakeJudge 固定评分：{self.fixed_score}/5",
            )
            for criterion in active_criteria
        }
        overall = sum(s.score for s in scores.values()) / len(scores) if scores else 0.0
        return JudgeResult(
            scores=scores,
            overall_score=round(overall, 2),
            token_usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0, cost_usd=0.0),
            latency_ms=0.0,
            cache_hit=False,
            skipped=False,
        )
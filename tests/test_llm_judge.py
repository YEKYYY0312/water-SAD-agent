"""Step 10 测试：LLM-as-judge 回答质量评估器。

覆盖（离线，全部 FakeJudge / mock transport，不连真实 API）：
- FakeJudge 对每条 criteria 输出 1-5 分 + overall 均分；
- 自定义 criteria 子集生效；
- 无 API Key 时 LLMJudge SKIP（skip_on_missing_key=True 且无环境变量）；
- mock transport 返回可解析 JSON → 分数被正确解析；
- 结果缓存：相同 query/answer/model 第二次命中 cache（不重复调用）；
- rubric 常量：5 条默认准则的锚定刻度文本齐全；
- FakeJudge call_count 语义（用于证明缓存命中不重复扣费）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from water_agent.harness.llm_judge import (
    DEFAULT_CRITERIA,
    JUDGE_RUBRIC,
    FakeJudge,
    LLMJudge,
)


class TestFakeJudge:
    def test_default_criteria_scored(self) -> None:
        judge = FakeJudge()
        result = judge.evaluate(query="给水管道设计秒流量", answer="根据 GB50015 确定……")
        assert result.skipped is False
        assert result.scores.keys() == set(DEFAULT_CRITERIA)
        for criterion in result.scores.values():
            assert 1 <= criterion.score <= 5
            assert criterion.reasoning
        # 默认固定 4 分 → overall = 4.0
        assert result.overall_score == 4.0

    def test_custom_criteria_subset(self) -> None:
        judge = FakeJudge(fixed_score=5)
        result = judge.evaluate("问题", "回答", criteria=["faithfulness", "clarity"])
        assert set(result.scores.keys()) == {"faithfulness", "clarity"}
        assert result.overall_score == 5.0

    def test_fixed_score_respected(self) -> None:
        judge = FakeJudge(fixed_score=1)
        result = judge.evaluate("问题", "回答")
        assert result.overall_score == 1.0
        assert all(e.score == 1 for e in result.scores.values())


class TestRealJudgeSkipSemantics:
    def test_no_api_key_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        judge = LLMJudge()
        result = judge.evaluate("唯一问题1", "回答1")
        assert result.skipped is True
        assert "SKIP" in result.error or "未配置" in result.error
        assert not result.scores

    def test_skips_regardless_of_dashscope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("DASHSCOPE_API_KEY", "fake")
        judge = LLMJudge(judge_api_key_env="DEEPSEEK_API_KEY")
        result = judge.evaluate("唯一问题2", "回答2")
        assert result.skipped is True

    def test_has_key_attempts_llm_call(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """有 Key 时尝试真实 LLM 调用，用子类覆盖 _call_llm 避免网络。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        class _FakeCallJudge(LLMJudge):
            def _call_llm(self, messages):
                import json as _json
                content = _json.dumps({
                    "scores": {"faithfulness": {"score": 5, "reasoning": "test"}},
                    "overall_reasoning": "ok",
                })
                from water_agent.domain.models import TokenUsage
                return content, TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)

        judge = _FakeCallJudge(judge_api_key_env="DEEPSEEK_API_KEY", cache_dir=tmp_path)
        result = judge.evaluate("唯一问题3", "回答3", skip_on_missing_key=True)
        assert result.skipped is False
        assert result.scores["faithfulness"].score == 5
        assert result.token_usage and result.token_usage.total_tokens == 15


class TestTransportParsing:
    def test_parses_json_from_transport_response(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """模拟 judge LLM 返回合法 JSON → 逐条解析为分数。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

        class _Judge(LLMJudge):
            def _call_llm(self, messages):
                import json as _json
                content = _json.dumps({
                    "scores": {
                        "faithfulness": {"score": 5, "reasoning": "严格依据检索条款"},
                        "citation_coverage": {"score": 4, "reasoning": "缺 1 条引用"},
                        "completeness": {"score": 3, "reasoning": "遗漏"},
                        "clarity": {"score": 4, "reasoning": "清晰"},
                        "no_fabrication": {"score": 5, "reasoning": "无编造"},
                    },
                    "overall_reasoning": "总体良好",
                })
                from water_agent.domain.models import TokenUsage
                return content, TokenUsage(input_tokens=100, output_tokens=60, total_tokens=160)

        judge = _Judge(judge_api_key_env="DEEPSEEK_API_KEY", cache_dir=tmp_path)
        result = judge.evaluate("唯一问题4", "回答4")
        assert result.skipped is False
        assert result.scores["faithfulness"].score == 5
        assert result.scores["completeness"].score == 3
        assert result.overall_score == pytest.approx(4.2)  # (5+4+3+4+5)/5

    def test_parse_response_extracts_json_block(self) -> None:
        """LLM 返回带 ```json 围栏或前后文本的响应 → 仍能解析。"""
        judge = LLMJudge()
        raw = '抱歉，我来评审：\n{"scores": {"clarity": {"score": 4, "reasoning": "ok"}}}\n以上。'
        parsed = judge._parse_response(raw)
        assert parsed["scores"]["clarity"]["score"] == 4

    def test_unparseable_response_raises(self) -> None:
        judge = LLMJudge()
        with pytest.raises(ValueError, match="valid JSON"):
            judge._parse_response("not json at all")


class TestCache:
    def test_cache_key_includes_context(self) -> None:
        """缓存键必须含 context：相同 query/answer 但检索上下文不同不得复用评分（S10-01）。"""
        from water_agent.harness.llm_judge import JudgeInput, _judge_cache_key

        base = JudgeInput(query="q", answer="a", criteria=["faithfulness"])
        key_a = _judge_cache_key(base.model_copy(update={"context": ["FIX 1.1.1"]}), "judge-model")
        key_b = _judge_cache_key(base.model_copy(update={"context": ["FIX 2.2.2"]}), "judge-model")
        assert key_a != key_b

    def test_cache_hit_avoids_repeat_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """相同 query/answer/model 第二次命中缓存，不重复调用 judge。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        call_count = [0]

        class _Judge(LLMJudge):
            def _call_llm(self, messages):
                call_count[0] += 1
                import json as _json
                content = _json.dumps({
                    "scores": {"faithfulness": {"score": 5, "reasoning": "called"}},
                    "overall_reasoning": "ok",
                })
                from water_agent.domain.models import TokenUsage
                return content, TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)

        judge = _Judge(judge_api_key_env="DEEPSEEK_API_KEY", cache_dir=tmp_path)
        # 第一次：未缓存 → 调用 _call_llm
        r1 = judge.evaluate("缓存测试问题", "缓存回答")
        assert call_count[0] == 1
        assert r1.cache_hit is False
        assert r1.scores["faithfulness"].score == 5
        # 第二次：相同 query/answer → 命中缓存，不调用 _call_llm
        r2 = judge.evaluate("缓存测试问题", "缓存回答")
        assert call_count[0] == 1, "缓存命中后不应再次调用 _call_llm"
        assert r2.cache_hit is True
        assert r2.scores["faithfulness"].score == 5
        # 第三次：不同 query → 新调用
        r3 = judge.evaluate("不同问题", "不同回答")
        assert call_count[0] == 2
        assert r3.cache_hit is False

    def test_cache_key_stable(self) -> None:
        from water_agent.harness.llm_judge import JudgeInput, _judge_cache_key
        inp = JudgeInput(query="q", answer="a", context=[], criteria=["clarity"])
        key1 = _judge_cache_key(inp, "deepseek-chat")
        key2 = _judge_cache_key(inp, "deepseek-chat")
        assert key1 == key2
        assert len(key1) == 32

    def test_different_answer_different_key(self) -> None:
        from water_agent.harness.llm_judge import JudgeInput, _judge_cache_key
        k1 = _judge_cache_key(JudgeInput(query="q", answer="a"), "m")
        k2 = _judge_cache_key(JudgeInput(query="q", answer="b"), "m")
        assert k1 != k2


class TestRubric:
    def test_rubric_has_all_default_criteria(self) -> None:
        assert set(JUDGE_RUBRIC.keys()) == set(DEFAULT_CRITERIA)
        for text in JUDGE_RUBRIC.values():
            assert "5:" in text and "1:" in text, "锚定刻度必须包含最高与最低锚点"

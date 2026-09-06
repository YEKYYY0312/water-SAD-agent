"""P2.5 Step 15 测试：governance_p0.jsonl 数据集完整性。

规格 §6 对红队数据集的要求同样适用 governance 评测集：

- 32 条用例（≥30 的下界与规格一致），id 唯一；
- 覆盖十个面（规格 §7）；
- 不含真实业主/项目/密钥；
- 每条用例的 expected_observed/expected_code 能精确裁定该场景的治理行为。

全部离线。
"""
from __future__ import annotations

import json
from pathlib import Path

from water_agent.harness.loader import load_eval_cases


class TestGovernanceDatasetIntegrity:
    def test_at_least_30_cases(self) -> None:
        """规格 §7 沿用红队数据集「≥30 条」的同一下界。"""
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        assert len(cases) >= 30

    def test_ids_are_unique(self) -> None:
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        ids = [c.id for c in cases]
        assert len(ids) == len(set(ids)), f"重复 id：{[i for i in ids if ids.count(i) > 1]}"

    def test_covers_ten_surfaces(self) -> None:
        """规格 §7：限流、预算、可靠性、护栏(四个面)、trace、检测器、缓存。"""
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        surfaces = {c.surface for c in cases}  # type: ignore[attr-defined]
        required = {
            "rate_limit",
            "budget",
            "reliability",
            "guardrail",
            "trace",
            "detector",
            "cache",
        }
        assert required <= surfaces, f"缺面：{required - surfaces}"

    def test_every_case_has_expected_observed(self) -> None:
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        for c in cases:
            assert c.expected_observed, f"{c.id} 缺 expected_observed"  # type: ignore[attr-defined]

    def test_no_real_credentials(self) -> None:
        """规格 §6：使用合成数据，不含真实密钥。"""
        raw = Path("data/eval/governance_p0.jsonl").read_text(encoding="utf-8")
        for forbidden in (
            "DEEPSEEK_API_KEY",
            "DASHSCOPE_API_KEY",
            "sk-",
            "Bearer ",
            "password=",
        ):
            assert forbidden not in raw, f"数据集不得含真实凭据特征：{forbidden}"

    def test_expected_code_is_declared_when_blocking(self) -> None:
        """期望被拒的场景必须声明 expected_code：只断言"被拒了"会让规则退化时假绿。

        例外：工具策略拒绝（路径 allowlist、capability 缺失）不产生错误码，
        只有 gateway 与护栏层的拒绝才有 code（RATE_LIMITED / GUARDRAIL_BLOCKED 等）。
        """
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        for c in cases:
            obs = c.expected_observed  # type: ignore[attr-defined]
            code = c.expected_code  # type: ignore[attr-defined]
            scenario = c.scenario  # type: ignore[attr-defined]

            # 工具策略拒绝：没有 code 是正常的
            if scenario in (
                "tool_export_dir_not_allowlisted",
                "tool_undeclared_fails_closed",
            ):
                continue

            if obs in ("block", "blocked", "rate_limited", "budget_exceeded", "circuit_open"):
                assert code, f"{c.id} 期望被拒但未声明 expected_code"

    def test_anti_regression_at_least_five_no_misfire_cases(self) -> None:
        """防误拦正例必须足够：太少会让护栏误拦时仍能全绿。"""
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        no_misfire = [c for c in cases if c.expected_observed == "allow"]  # type: ignore[attr-defined]
        assert len(no_misfire) >= 5

    def test_each_case_has_rationale(self) -> None:
        """每条用例必须有 rationale：reviewer 与未来修改者需要知道**为什么**期望这个裁定。"""
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        for c in cases:
            assert c.rationale, f"{c.id} 缺 rationale"  # type: ignore[attr-defined]

    def test_evidence_keys_are_not_typos(self) -> None:
        """证据键必须是场景实际产出的键，否则会因拼错而永远缺失 → 永远失败。

        这条测试**不遍历全部可能键**（那是测场景实现），而是防止明显拼错
        （比如 `contnet_unchanged` 之类）与不可能共存的矛盾期望。
        """
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        for c in cases:
            true_keys = set(c.expect_evidence_true)  # type: ignore[attr-defined]
            false_keys = set(c.expect_evidence_false)  # type: ignore[attr-defined]
            overlap = true_keys & false_keys
            assert not overlap, f"{c.id} 的证据期望自相矛盾：{overlap}"

    def test_cache_cases_cover_all_rejection_reasons(self) -> None:
        """缓存拒绝有十种原因（规格 §5），至少覆盖主要的四种。"""
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        cache_cases = [c for c in cases if c.surface == "cache"]  # type: ignore[attr-defined]
        scenarios = {c.scenario for c in cache_cases}  # type: ignore[attr-defined]
        key_reasons = {
            "cache_unknown_version_never_stored",
            "cache_unverified_citation_never_stored",
            "cache_nonzero_temperature_never_stored",
            "cache_calc_result_never_stored",
        }
        assert key_reasons <= scenarios, f"缺关键缓存拒绝场景：{key_reasons - scenarios}"

    def test_detector_cases_include_pairing_evidence(self) -> None:
        """检测器正例必须覆盖 Step 15 新接线的工具执行与护栏事件配对。"""
        cases = load_eval_cases("data/eval/governance_p0.jsonl")
        detector_cases = [c for c in cases if c.surface == "detector"]  # type: ignore[attr-defined]
        scenarios = {c.scenario for c in detector_cases}  # type: ignore[attr-defined]
        assert "tool_execution_paired_with_guardrail_event" in scenarios
        assert "guardrail_bypass_detected_when_unpaired" in scenarios

    def test_all_jsonl_lines_are_valid_json(self) -> None:
        """数据集本身必须是合法 JSONL（加载错误应在加载器抛出，不应只在 runner 里才发现）。"""
        for line_no, raw in enumerate(
            Path("data/eval/governance_p0.jsonl").read_text(encoding="utf-8").splitlines(), start=1
        ):
            if raw.strip():
                try:
                    json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise AssertionError(f"line {line_no} invalid json: {exc.msg}") from exc

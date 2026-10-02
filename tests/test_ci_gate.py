"""Step 10 测试：CI 门禁 + 基线对比 + SKIP 语义。

覆盖（离线，无真实 API Key 可验证）：
- ``--variant real`` 无 Key → 所有用例 SKIP，退出码 0，输出含 ``skipped_all: true``
- 机制层（默认 variant）无 Key 正常运行（per-case variant）
- ``compare_baseline.py`` 阈值鉴定：通过率下降 ≤2pt 不触发，>2pt 触发
- ``compare_baseline.py`` 无新增核心失败（PTP 门禁）
- ``compare_baseline.py`` --init 生成基线
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from water_agent.harness.runner import HarnessRunner, main

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "data" / "eval" / "rag_p0.jsonl"
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


def test_verify_delegates_to_canonical_ci_gate() -> None:
    """统一验证入口必须复用完整门禁，避免 P2/mypy/baseline 规则漂移。"""
    verify_script = (REPO_ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
    assert 'exec bash "$ROOT/scripts/ci_gate.sh"' in verify_script


class TestRunnerVariantRealSKIP:
    """--variant real 无 Key 时应 SKIP 所有用例，退出码 0。"""

    def test_runner_real_variant_skips_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """全部 SKIP → 报告 skipped=12, failed=0, exit 0, output 含 skipped_all。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("DASHSCOPE_API_KEY", "")
        report_dir = tmp_path / "reports"
        trace_dir = tmp_path / "traces"
        runner = HarnessRunner(
            eval_path=EVAL_PATH,
            report_dir=report_dir,
            trace_dir=trace_dir,
            fixture_path=FIXTURE_PATH,
            variant="real",
        )
        report, report_path, _load_issues = runner.run()
        assert report.skipped == report.total == 12
        assert report.failed == 0
        assert report.passed == 0
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["skipped"] == 12
        assert report.cases[0].skipped is True
        assert report.cases[0].actual_status == "skipped"
        assert "SKIP" in (report.cases[0].failure_reason or "")

    def test_main_returns_zero_on_skip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """main() 退出码 0（SKIP 不算失败）。使用无 load_issues 的临时 eval 文件。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("DASHSCOPE_API_KEY", "")
        # 创建无缺陷的临时 eval 文件
        clean_eval = tmp_path / "clean_rag.jsonl"
        clean_eval.write_text(
            '{"id": "r1", "type": "rag", "variant": "mock", "query": "给水管道的设计秒流量", "gold_articles_any": ["FIX 1.1.1"], "expected_status": "succeeded"}\n'
            '{"id": "r2", "type": "rag", "variant": "mock", "query": "排水管道最小管径", "gold_articles_any": ["FIX 2.1.2"], "expected_status": "succeeded"}\n',
            encoding="utf-8",
        )
        report_dir = tmp_path / "reports"
        trace_dir = tmp_path / "traces"
        exit_code = main(
            [
                str(clean_eval),
                "--report-dir", str(report_dir),
                "--trace-dir", str(trace_dir),
                "--fixture-path", str(FIXTURE_PATH),
                "--variant", "real",
            ]
        )
        out = capsys.readouterr()
        assert exit_code == 0, f"SKIP 应退出 0，实际 {exit_code}; output: {out.out}"

    def test_main_mechanism_rag_p0_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """机制层（mock variant）正常跑：rag_p0 12/12、无 load_issues → exit 0。

        门禁据退出码判定（ci_gate.sh），因此门禁数据集不得带故意的坏行，陷阱用例也
        必须在正确实现下判通过——否则门禁只能 ``|| true`` 放弃退出码。
        """
        report_dir = tmp_path / "reports"
        trace_dir = tmp_path / "traces"
        exit_code = main(
            [
                str(EVAL_PATH),
                "--report-dir", str(report_dir),
                "--trace-dir", str(trace_dir),
                "--fixture-path", str(FIXTURE_PATH),
            ]
        )
        out = capsys.readouterr()
        assert exit_code == 0, out.out
        assert '"ok": true' in out.out
        assert '"failed": 0' in out.out
        assert '"load_issues": []' in out.out


class TestCompareBaseline:
    """compare_baseline.py 的阈值与基线对比逻辑。"""

    @pytest.fixture
    def baseline_path(self, tmp_path: Path) -> Path:
        """创建示例基线文件（含 passed_case_ids 供 PASS_TO_PASS 门禁）。"""
        baseline = {
            "schema_version": 1,
            "created_at": "2026-09-01T00:00:00Z",
            "suites": {
                "rag_p0": {
                    "variant": "mock",
                    "total": 12,
                    "passed": 11,
                    "failed": 1,
                    "skipped": 0,
                    "pass_rate": 0.9167,
                    "citation_ok_rate": 0.9167,
                    "retrieval_metrics": {"recall_at_1": 0.8333, "recall_at_5": 1.0, "mrr": 0.8750},
                    "avg_latency_ms": 4.5,
                    "passed_case_ids": ["rag-p0-0001", "rag-p0-0002", "rag-p0-0005"],
                },
                "calc_p0": {
                    "variant": "programmatic",
                    "total": 28,
                    "passed": 28,
                    "failed": 0,
                    "skipped": 0,
                    "pass_rate": 1.0,
                    "citation_ok_rate": 1.0,
                    "retrieval_metrics": {},
                    "avg_latency_ms": 1.2,
                    "passed_case_ids": [],
                },
            },
        }
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @pytest.fixture
    def good_report(self, tmp_path: Path) -> Path:
        """通过率接近基线的新报告（pass_rate=0.9，下降 1.67pt，<2pt 阈值）。"""
        report = {
            "total": 12,
            "passed": 11,
            "failed": 1,
            "skipped": 0,
            "pass_rate": 0.9167,
            "citation_ok_rate": 0.9167,
            "retrieval_metrics": {"recall_at_1": 0.8333, "recall_at_5": 1.0, "mrr": 0.8750},
            "avg_latency_ms": 5.0,
            "cases": [
                {"case_id": "rag-p0-0001", "passed": True, "expected_status": "succeeded", "actual_status": "succeeded", "actual_citations": ["FIX 1.1.1"], "context_articles": ["FIX 1.1.1"]},
                {"case_id": "rag-p0-0011", "passed": False, "expected_status": "succeeded", "actual_status": "failed", "actual_citations": ["FAKE 0.0.0"], "context_articles": ["FIX 1.1.1"]},
            ],
        }
        path = tmp_path / "good_report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @pytest.fixture
    def bad_report(self, tmp_path: Path) -> Path:
        """通过率大幅下降的新报告（pass_rate=0.5，下降 41.67pt，>2pt 阈值）。"""
        report = {
            "total": 12,
            "passed": 6,
            "failed": 6,
            "skipped": 0,
            "pass_rate": 0.5,
            "citation_ok_rate": 0.5,
            "retrieval_metrics": {"recall_at_1": 0.5, "recall_at_5": 0.75, "mrr": 0.5},
            "avg_latency_ms": 5.0,
            "cases": [
                {"case_id": "rag-p0-0001", "passed": True, "expected_status": "succeeded", "actual_status": "succeeded", "actual_citations": [], "context_articles": []},
                {"case_id": "rag-p0-0005", "passed": False, "expected_status": "succeeded", "actual_status": "failed", "actual_citations": ["FIX 3.1.1"], "context_articles": ["FIX 3.1.1"]},
            ],
        }
        path = tmp_path / "bad_report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def test_compare_passes_good_report(self, baseline_path: Path, good_report: Path) -> None:
        """通过率下降 ≤2pt → 通过。"""
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        result = compare_suite(
            suite_key="rag_p0",
            baseline={"rag_p0": json.loads(good_report.read_text())},
            new_report=json.loads(good_report.read_text()),
            thresholds=THRESHOLDS,
        )
        assert result["passed"] is True
        assert result["reason"] == ""

    def test_compare_fails_pass_rate_decline(self, baseline_path: Path, bad_report: Path) -> None:
        """通过率下降 >2pt → 失败。"""
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        baseline = json.loads(baseline_path.read_text())
        bad = json.loads(bad_report.read_text())
        result = compare_suite(
            suite_key="rag_p0",
            baseline=baseline["suites"],
            new_report=bad,
            thresholds=THRESHOLDS,
        )
        assert result["passed"] is False
        assert "pass_rate" in result["reason"]

    def test_compare_handles_no_new_core_failures(self, baseline_path: Path, good_report: Path) -> None:
        """PTP 门禁：基线的通过用例在新报告中不得失败。"""
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        baseline = json.loads(baseline_path.read_text())
        good = json.loads(good_report.read_text())
        # 制造一个 PTP 违反：baseline 的 passed 用例（rag-p0-0001）在新报告中 failed
        good["cases"][0]["passed"] = False
        good["cases"][0]["actual_status"] = "failed"
        result = compare_suite(
            suite_key="rag_p0",
            baseline=baseline["suites"],
            new_report=good,
            thresholds=THRESHOLDS,
        )
        assert result["passed"] is False
        assert "PASS_TO_PASS" in result["reason"] or "core_failure" in result["reason"]

    def test_compare_skips_skip_cases(self, baseline_path: Path) -> None:
        """SKIP 用例不计入基线比较。"""
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        baseline = json.loads(baseline_path.read_text())
        # 新报告全部 SKIP
        skip_report = {
            "total": 12,
            "passed": 0,
            "failed": 0,
            "skipped": 12,
            "pass_rate": 0.0,
            "cases": [
                {"case_id": f"rag-p0-{i:04d}", "passed": False, "skipped": True, "actual_status": "skipped", "failure_reason": "SKIP: no key"}
                for i in range(1, 13)
            ],
        }
        result = compare_suite(
            suite_key="rag_p0",
            baseline=baseline["suites"],
            new_report=skip_report,
            thresholds=THRESHOLDS,
        )
        assert result["passed"] is True, "全部 SKIP 不应触发基线失败"

    def test_compare_fallback_citation_ok_uses_zero_cite_semantics(self) -> None:
        """兜底计算与 runner 语义一致：有 expected_articles 的零引用计 citation failure。"""
        from scripts.compare_baseline import _compute_new_metrics

        report = {
            "total": 3,
            "passed": 2,
            "failed": 1,
            "skipped": 1,  # 含 skipped 才会触发 cases 兜底计算
            "pass_rate": 1.0,
            "citation_ok_rate": 1.0,
            "cases": [
                # 零引用但有 gold 引用要求 → citation failure
                {"case_id": "a", "passed": False, "skipped": False, "expected_articles": ["FIX 1.1.1"], "actual_citations": [], "context_articles": ["FIX 1.1.1"]},
                # 零引用但不需要引用 → 不误伤
                {"case_id": "b", "passed": True, "skipped": False, "expected_articles": [], "actual_citations": [], "context_articles": []},
                {"case_id": "s", "passed": False, "skipped": True, "actual_status": "skipped"},
            ],
        }
        _, citation_ok = _compute_new_metrics(report)
        assert citation_ok == 0.5, f"兜底 citation_ok 应排除零引用失败用例: {citation_ok}"

    def test_mechanism_suites_include_p2(self) -> None:
        """机制层默认套件：rag/calc + P2 docgen/material + P2.5 governance。

        governance_p0 早在 baseline.json 里，却不在默认对比套件中——删掉两条治理用例
        整条门禁仍绿（review 2026-09-30）。
        """
        from scripts.compare_baseline import MECHANISM_SUITES
        assert MECHANISM_SUITES == [
            "rag_p0", "calc_p0", "docgen_p0", "material_p0", "governance_p0", "agentic_p0",
        ]

    def test_compare_p2_suite_identical_report_passes(self) -> None:
        """docgen_p0 套件：与基线指标一致的新报告必须通过（零下降 + 无 PTP 违规）。"""
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        baseline = {
            "docgen_p0": {
                "total": 11, "passed": 11, "failed": 0, "skipped": 0,
                "pass_rate": 1.0, "citation_ok_rate": 0.2727,
                "passed_case_ids": [f"docgen-{i:04d}" for i in range(1, 12)],
            }
        }
        new_report = {
            "total": 11, "passed": 11, "failed": 0, "skipped": 0,
            "pass_rate": 1.0, "citation_ok_rate": 0.2727,
            "cases": [
                {"case_id": f"docgen-{i:04d}", "passed": True, "skipped": False}
                for i in range(1, 12)
            ],
        }
        result = compare_suite("docgen_p0", baseline, new_report, THRESHOLDS)
        assert result["passed"] is True, result["reason"]
        assert result["deltas"]["pass_delta_pt"] == 0.0

    def test_compare_p2_suite_ptp_violation_fails(self) -> None:
        """PTP 门禁对 P2 套件同样生效：基线通过用例（docgen-0002）新失败 → 判失败。"""
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        baseline = {
            "material_p0": {
                "total": 10, "passed": 10, "failed": 0, "skipped": 0,
                "pass_rate": 1.0, "citation_ok_rate": 1.0,
                "passed_case_ids": [f"mat-{i:04d}" for i in range(1, 11)],
            }
        }
        new_report = {
            "total": 10, "passed": 9, "failed": 1, "skipped": 0,
            "pass_rate": 0.9, "citation_ok_rate": 0.9,
            "cases": [
                {"case_id": "mat-0002", "passed": False, "skipped": False},
                *[
                    {"case_id": f"mat-{i:04d}", "passed": True, "skipped": False}
                    for i in range(1, 11) if i != 2
                ],
            ],
        }
        result = compare_suite("material_p0", baseline, new_report, THRESHOLDS)
        assert result["passed"] is False
        assert "PASS_TO_PASS" in result["reason"]

    # ── review 2026-09-30：用例被删/被跳过不得静默通过 ──

    @staticmethod
    def _suite_report(case_ids: list[str], *, skipped: tuple[str, ...] = ()) -> dict:
        cases = [
            {"case_id": cid, "passed": cid not in skipped, "skipped": cid in skipped}
            for cid in case_ids
        ]
        passed = sum(1 for c in cases if c["passed"])
        return {
            "total": len(cases), "passed": passed, "failed": 0, "skipped": len(skipped),
            "pass_rate": 1.0, "citation_ok_rate": 1.0, "cases": cases,
        }

    def test_baseline_passed_case_missing_from_new_report_fails(self) -> None:
        """基线通过的用例在新报告里消失（数据集删行/字段改名变成加载问题）→ 判失败。

        此前 PASS_TO_PASS 只看"新报告里存在且失败"的用例：calc_p0 静默少 4 条仍绿。
        """
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        baseline = {"calc_p0": {"pass_rate": 1.0, "citation_ok_rate": 1.0,
                                "passed_case_ids": ["c1", "c2", "c3"]}}
        result = compare_suite("calc_p0", baseline, self._suite_report(["c1", "c2"]), THRESHOLDS)
        assert result["passed"] is False
        assert "c3" in result["reason"] and "缺失" in result["reason"]

    def test_baseline_passed_case_skipped_in_new_report_fails(self) -> None:
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        baseline = {"calc_p0": {"pass_rate": 1.0, "citation_ok_rate": 1.0,
                                "passed_case_ids": ["c1", "c2"]}}
        result = compare_suite("calc_p0", baseline, self._suite_report(["c1", "c2"], skipped=("c2",)), THRESHOLDS)
        assert result["passed"] is False
        assert "c2" in result["reason"]

    def test_baseline_case_ids_cover_cases_that_were_failing(self) -> None:
        """``case_ids`` 记录基线全部用例：连基线里本就失败的用例被删也要报出。"""
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        baseline = {"rag_p0": {"pass_rate": 0.5, "citation_ok_rate": 1.0,
                               "passed_case_ids": ["r1"], "case_ids": ["r1", "r2"]}}
        result = compare_suite("rag_p0", baseline, self._suite_report(["r1"]), THRESHOLDS)
        assert result["passed"] is False
        assert "r2" in result["reason"]

    def test_suite_without_baseline_entry_fails(self) -> None:
        """要对比的套件在基线里没有条目 → 判失败，而不是"无基线记录，跳过"。"""
        from scripts.compare_baseline import THRESHOLDS, compare_suite
        result = compare_suite("agentic_p0", {}, self._suite_report(["a1"]), THRESHOLDS)
        assert result["passed"] is False
        assert "无基线记录" in result["reason"]

    def test_baselined_suite_without_new_report_fails(self, tmp_path: Path) -> None:
        """基线里有、这次却没产出报告（runner 没跑成）→ 门禁红，不是"无新报告，跳过"。"""
        from scripts.compare_baseline import main as compare_main
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({"schema_version": 1, "suites": {
            "rag_p0": {"pass_rate": 1.0, "citation_ok_rate": 1.0, "passed_case_ids": ["r1"]},
        }}), encoding="utf-8")
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        assert compare_main([str(baseline), "--report-dir", str(report_dir), "--suite", "rag_p0"]) == 1

    def test_report_path_requires_a_single_suite(self, tmp_path: Path) -> None:
        """--report-path 只能配一个 --suite：此前会被套用到每个套件（互相错比）。"""
        from scripts.compare_baseline import main as compare_main
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({"schema_version": 1, "suites": {}}), encoding="utf-8")
        report = tmp_path / "r.json"
        report.write_text(json.dumps(self._suite_report(["r1"])), encoding="utf-8")
        assert compare_main([str(baseline), "--report-path", str(report)]) == 2

    def test_build_baseline_records_every_case_id(self, tmp_path: Path) -> None:
        from scripts.compare_baseline import build_baseline
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        report = self._suite_report(["r1", "r2"])
        report["cases"][1]["passed"] = False
        (report_dir / "rag_p0_20260930T000000Z.json").write_text(json.dumps(report), encoding="utf-8")
        baseline = build_baseline(report_dir, ["rag_p0"])
        assert baseline["suites"]["rag_p0"]["case_ids"] == ["r1", "r2"]
        assert baseline["suites"]["rag_p0"]["passed_case_ids"] == ["r1"]

    def test_build_baseline_includes_p2_suites(self, tmp_path: Path) -> None:
        """--init 从真实报告生成基线：docgen_p0/material_p0 进入 suites，指标与溯源取自报告。"""
        from scripts.compare_baseline import MECHANISM_SUITES, build_baseline
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        docgen_report = {
            "input_path": "data/eval/docgen_p0.jsonl", "variant": "per-case",
            "total": 11, "passed": 11, "failed": 0, "skipped": 0,
            "pass_rate": 1.0, "citation_ok_rate": 0.2727, "avg_latency_ms": 0.01,
            "retrieval_metrics": {},
            "cases": [{"case_id": f"docgen-{i:04d}", "passed": True, "skipped": False} for i in range(1, 12)],
        }
        material_report = {
            "input_path": "data/eval/material_p0.jsonl", "variant": "per-case",
            "total": 10, "passed": 10, "failed": 0, "skipped": 0,
            "pass_rate": 1.0, "citation_ok_rate": 1.0, "avg_latency_ms": 0.01,
            "retrieval_metrics": {},
            "cases": [{"case_id": f"mat-{i:04d}", "passed": True, "skipped": False} for i in range(1, 11)],
        }
        (report_dir / "docgen_p0_20260904T000000Z.json").write_text(json.dumps(docgen_report), encoding="utf-8")
        (report_dir / "material_p0_20260904T000001Z.json").write_text(json.dumps(material_report), encoding="utf-8")

        baseline = build_baseline(report_dir, MECHANISM_SUITES)
        # rag_p0/calc_p0 无报告 → 跳过；docgen/material 必须进入
        assert set(baseline["suites"]) == {"docgen_p0", "material_p0"}
        assert baseline["suites"]["docgen_p0"]["total"] == 11
        assert baseline["suites"]["docgen_p0"]["pass_rate"] == 1.0
        assert baseline["suites"]["docgen_p0"]["source_report"] == "docgen_p0_20260904T000000Z.json"
        assert baseline["suites"]["docgen_p0"]["passed_case_ids"] == [f"docgen-{i:04d}" for i in range(1, 12)]
        assert baseline["suites"]["material_p0"]["total"] == 10
        assert baseline["suites"]["material_p0"]["source_report"] == "material_p0_20260904T000001Z.json"


class TestRunnerReportAggregates:
    """报告聚合字段正确性。"""

    def test_report_contains_retrieval_metrics(self, tmp_path: Path) -> None:
        runner = HarnessRunner(
            eval_path=EVAL_PATH,
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
            fixture_path=FIXTURE_PATH,
        )
        report, _report_path, _ = runner.run()
        assert report.retrieval_metrics is not None
        assert "recall_at_1" in report.retrieval_metrics
        assert "mrr" in report.retrieval_metrics
        assert report.total_cost_usd >= 0.0
        assert report.total_tokens >= 0
        assert report.avg_latency_ms > 0
        assert report.p50_latency_ms > 0

    def test_citation_ok_rate_computed(self, tmp_path: Path) -> None:
        runner = HarnessRunner(
            eval_path=EVAL_PATH,
            report_dir=tmp_path / "reports",
            trace_dir=tmp_path / "traces",
            fixture_path=FIXTURE_PATH,
        )
        report, _, _ = runner.run()
        # rag_p0: 12/12 pass；陷阱用例的最终 citations 仍含被拒的伪造引用，
        # citation_ok 口径（引用 ⊆ 检索上下文）照旧把它计为 1 条不合格 → 11/12。
        assert report.failed == 0
        assert report.citation_ok_rate == pytest.approx(11 / 12)

class TestRealComponentsFailureIsNotSkip:
    """有 Key 但真实组件构建失败：退出码 3（≠ SKIP 的 0，≠ 用例失败的 1），stderr 结构化。"""

    def test_main_returns_3_when_real_components_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        from water_agent.harness import runner as runner_mod

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-dummy")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dummy")
        monkeypatch.setattr(runner_mod.HarnessRunner, "_probe_real_skip", lambda self: None)

        def _boom(self):
            raise RuntimeError("embedding dimension mismatch")

        monkeypatch.setattr(runner_mod.HarnessRunner, "_build_real_retriever", _boom)
        clean_eval = tmp_path / "clean.jsonl"
        clean_eval.write_text(
            json.dumps({"id": "r-1", "type": "rag", "variant": "real", "query": "给水管道的设计秒流量", "gold_articles_any": ["FIX 1.1.1"]}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        code = main([str(clean_eval), "--variant", "real", "--report-dir", str(tmp_path / "r"), "--trace-dir", str(tmp_path / "t"), "--fixture-path", str(FIXTURE_PATH)])
        err = capsys.readouterr().err
        assert code == runner_mod.EXIT_REAL_COMPONENTS_FAILED == 3
        assert "real_components_failed" in err and "embedding dimension mismatch" in err


class TestRealVariantAgentic:
    """--variant real 默认走 agentic 图：引用由模型 cite_clauses 声明、校验器真判（离线用假模型验证机制）。"""

    @pytest.fixture
    def offline_real(self, monkeypatch: pytest.MonkeyPatch):
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage

        from water_agent.harness import runner as runner_mod
        from water_agent.retrieval.in_memory import InMemoryRetriever
        from water_agent.retrieval.loader import load_clauses_jsonl

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-dummy")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dummy")
        monkeypatch.setattr(runner_mod.HarnessRunner, "_probe_real_skip", lambda self: None)
        monkeypatch.setattr(
            runner_mod.HarnessRunner,
            "_build_real_retriever",
            lambda self: InMemoryRetriever(load_clauses_jsonl(FIXTURE_PATH)),
        )
        responses = [
            AIMessage(content="", tool_calls=[{"name": "retrieve_clauses", "args": {"query": "给水管道的设计秒流量", "top_k": 5}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "cite_clauses", "args": {"article_ids": ["FIX 1.1.1"]}, "id": "c2"}]),
            AIMessage(content="设计秒流量按用水定额与用水人数计算。"),
        ]
        monkeypatch.setattr(
            runner_mod.HarnessRunner,
            "_build_real_chat_model",
            lambda self: FakeMessagesListChatModel(responses=responses),
        )
        return runner_mod

    def _eval_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "real.jsonl"
        path.write_text(
            json.dumps({"id": "r-1", "type": "rag", "variant": "real", "query": "给水管道的设计秒流量", "gold_articles_any": ["FIX 1.1.1"]}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def test_agentic_real_case_scores_model_declared_citations(self, tmp_path: Path, offline_real) -> None:
        runner = HarnessRunner(
            eval_path=self._eval_file(tmp_path), report_dir=tmp_path / "r", trace_dir=tmp_path / "t",
            fixture_path=FIXTURE_PATH, variant="real",
        )
        report, _, _ = runner.run()
        case = report.cases[0]
        assert case.skipped is False and case.passed is True, case.failure_reason
        assert case.variant == "real"
        assert case.actual_citations == ["FIX 1.1.1"], "引用必须来自模型 cite_clauses 声明，而非系统代填"
        assert "FIX 1.1.1" in case.context_articles
        assert case.retrieval_metrics["recall_at_5"] == 1.0
        assert report.citation_ok_rate == 1.0
        # trace 由 agentic 图落盘，含 llm_call
        traces = list((tmp_path / "t").glob("*.json"))
        assert traces and case.trace_id
        doc = json.loads(traces[0].read_text(encoding="utf-8"))
        assert any(e["event_type"] == "llm_call" for e in doc["events"])

    def test_legacy_real_graph_still_available_for_comparison(self, tmp_path: Path, offline_real, monkeypatch: pytest.MonkeyPatch) -> None:
        from water_agent.generation.mock import MockGenerator

        monkeypatch.setattr(offline_real.HarnessRunner, "_build_real_generator", lambda self: MockGenerator())
        runner = HarnessRunner(
            eval_path=self._eval_file(tmp_path), report_dir=tmp_path / "r", trace_dir=tmp_path / "t",
            fixture_path=FIXTURE_PATH, variant="real", real_graph="legacy",
        )
        report, _, _ = runner.run()
        assert report.cases[0].passed is True and report.cases[0].variant == "real"

    def test_invalid_real_graph_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="real_graph"):
            HarnessRunner(eval_path=EVAL_PATH, report_dir=tmp_path, trace_dir=tmp_path, fixture_path=FIXTURE_PATH, real_graph="nope")


class TestRealQualityJudgement:
    """质量层 judge 真实反映到 quality_pass；低分不篡改机制判分（P1/Step 10 评审）。"""

    @pytest.fixture
    def quality_offline(self, monkeypatch: pytest.MonkeyPatch):
        from water_agent.harness import runner as runner_mod
        from water_agent.retrieval.in_memory import InMemoryRetriever
        from water_agent.retrieval.loader import load_clauses_jsonl

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-dummy")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dummy")
        monkeypatch.setattr(runner_mod.HarnessRunner, "_probe_real_skip", lambda self: None)
        monkeypatch.setattr(
            runner_mod.HarnessRunner,
            "_build_real_retriever",
            lambda self: InMemoryRetriever(load_clauses_jsonl(FIXTURE_PATH)),
        )
        return runner_mod

    def _run_with_judge(self, tmp_path: Path, quality_offline, monkeypatch: pytest.MonkeyPatch, dim_scores: dict[str, int]):
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage

        from water_agent.harness import llm_judge as judge_mod
        from water_agent.harness.llm_judge import JudgeResult, JudgeScore

        responses = [
            AIMessage(content="", tool_calls=[{"name": "retrieve_clauses", "args": {"query": "给水管道的设计秒流量", "top_k": 5}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "cite_clauses", "args": {"article_ids": ["FIX 1.1.1"]}, "id": "c2"}]),
            AIMessage(content="设计秒流量按用水定额与用水人数计算。"),
        ]
        monkeypatch.setattr(
            quality_offline.HarnessRunner,
            "_build_real_chat_model",
            lambda self: FakeMessagesListChatModel(responses=responses),
        )
        monkeypatch.setattr(
            judge_mod.LLMJudge,
            "evaluate",
            lambda self, query, answer, *, context=None, criteria=None, skip_on_missing_key=True: JudgeResult(
                scores={c: JudgeScore(score=dim_scores[c], reasoning="fixed") for c in dim_scores},
                overall_score=1.0,
            ),
        )
        path = tmp_path / "q.jsonl"
        path.write_text(
            json.dumps({"id": "q1", "type": "rag", "variant": "real", "query": "给水管道的设计秒流量", "gold_articles_any": ["FIX 1.1.1"]}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        runner = HarnessRunner(
            eval_path=path,
            report_dir=tmp_path / "r",
            trace_dir=tmp_path / "t",
            fixture_path=FIXTURE_PATH,
            variant="real",
            judge=True,
        )
        report, _, _ = runner.run()
        return report.cases[0], report

    def test_judge_1of5_case_is_not_quality_pass(self, tmp_path: Path, quality_offline, monkeypatch: pytest.MonkeyPatch) -> None:
        """阻断维度 judge=1/5 → quality_pass=False、低分计数 1、quality_pass_rate=0。"""
        case, report = self._run_with_judge(
            tmp_path, quality_offline, monkeypatch,
            {"faithfulness": 1, "citation_coverage": 5, "completeness": 5, "clarity": 5, "no_fabrication": 4},
        )
        assert case.passed is True, "机制层判分（确定性 citation 校验）不被 judge 篡改"
        assert case.quality_pass is False, "judge 阻断维度 1/5 不得显示为质量通过"
        assert any("faithfulness=1" in r for r in case.quality_fail_reasons), case.quality_fail_reasons
        assert report.quality_judged_count == 1
        assert report.quality_low_score_count == 1
        assert report.quality_pass_rate == 0.0
        assert report.judge_dimension_averages["faithfulness"] == 1.0
        assert report.judge_dimension_averages["no_fabrication"] == 4.0

    def test_judge_high_scores_quality_pass_but_keeps_mechanism_separate(self, tmp_path: Path, quality_offline, monkeypatch: pytest.MonkeyPatch) -> None:
        """阻断维度全 5/5 → quality_pass=True；mechanism passed 仍独立成立。"""
        case, report = self._run_with_judge(
            tmp_path, quality_offline, monkeypatch,
            {"faithfulness": 5, "citation_coverage": 5, "completeness": 4, "clarity": 4, "no_fabrication": 5},
        )
        assert case.passed is True
        assert case.quality_pass is True
        assert case.quality_fail_reasons == []
        assert report.quality_pass_rate == 1.0
        assert report.quality_low_score_count == 0
        assert report.judge_dimension_averages["faithfulness"] == 5.0
        assert report.judge_dimension_averages["completeness"] == 4.0


def test_gate_resolves_the_venv_instead_of_hardcoding_underscore_venv() -> None:
    """GitHub runner 上 uv sync 产出 .venv；门禁硬编码 _venv 时 p5-ci 必挂（review 09-28）。"""

    import re
    from pathlib import Path

    gate = Path("scripts/ci_gate.sh").read_text(encoding="utf-8")
    # 除解析块本身外，不得再出现写死的 _venv/bin/<tool>
    body = gate.split('PY="$VENV/bin/python"', 1)[1]
    assert not re.search(r"(?<![\w$])_venv/bin/", body), "门禁主体仍有写死的 _venv/bin 路径"
    assert '"$VENV/bin/pytest"' in gate
    assert '[ -x ".venv/bin/python" ]' in gate
    workflow = Path(".github/workflows/p5-ci.yml").read_text(encoding="utf-8")
    assert "VENV: .venv" in workflow


def test_gate_refuses_to_run_without_a_venv(tmp_path) -> None:
    """找不到虚拟环境时响亮失败（exit 2），不退回系统 python 跑出假绿。"""

    import shutil
    import subprocess
    from pathlib import Path

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(Path("scripts/ci_gate.sh"), scripts / "ci_gate.sh")
    env = {k: v for k, v in __import__("os").environ.items() if k != "VENV"}
    result = subprocess.run(
        ["bash", str(scripts / "ci_gate.sh")], cwd=tmp_path, capture_output=True, text=True, env=env
    )
    assert result.returncode == 2
    assert "找不到虚拟环境" in result.stderr


class TestGateScript:
    """ci_gate.sh 的判定方式（静态断言：跑一遍完整门禁要十几分钟，这里锁住写法）。"""

    GATE = Path(__file__).resolve().parents[1] / "scripts" / "ci_gate.sh"

    def test_runner_steps_judge_by_exit_code(self) -> None:
        """机制层 runner 步骤不得 ``2>/dev/null || true`` 吞掉退出码再只解析失败条数。"""
        gate = self.GATE.read_text(encoding="utf-8")
        for line in gate.splitlines():
            if "harness.runner" in line and not line.lstrip().startswith("#"):
                assert "|| true" not in line, line
        assert "run_suite rag_p0 data/eval/rag_p0.jsonl" in gate
        assert "run_suite governance_p0 data/eval/governance_p0.jsonl" in gate
        assert "run_suite agentic_p0 data/eval/agentic_p0.jsonl" in gate

    def test_missing_baseline_is_a_failure_unless_explicitly_allowed(self) -> None:
        """基线文件缺失 = 失败（此前只打印"首次运行"就放行）；首次接入须显式开关。"""
        gate = self.GATE.read_text(encoding="utf-8")
        assert "CI_GATE_ALLOW_MISSING_BASELINE" in gate
        for line in gate.splitlines():
            if "首次运行" in line and "echo" in line:
                # 唯一允许的"首次运行"提示在 missing_baseline() 的显式放行分支里
                assert "CI_GATE_ALLOW_MISSING_BASELINE=1" in line, line

    def test_compare_reads_only_this_runs_reports(self) -> None:
        """compare_baseline 只看本次门禁写的报告目录（旧拷贝不得压过本次结果）。"""
        gate = self.GATE.read_text(encoding="utf-8")
        assert 'GATE_REPORT_DIR="data/eval/reports/ci_gate"' in gate
        assert 'compare_baseline.py data/eval/baseline.json --report-dir "$GATE_REPORT_DIR"' in gate

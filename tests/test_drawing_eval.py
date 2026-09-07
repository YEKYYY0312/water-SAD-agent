"""P3 Step 22：drawing_p0 smoke 评测集 + runner + 门禁对比的测试。

覆盖：
- 套件完整性：suite.json 元数据诚实性（real_samples=false）、每个 gold 有对应 DXF、
  gold handle 真实存在于图中（生成器与入库图纸必须同步）；
- runner 端到端：8/8 通过、聚合指标、报告结构化可 diff；
- 判分反向例：改坏 gold handle → 用例失败且原因可读（不是静默通过）；
- `compare_drawing_p0.py`：PTP / recall 下降 / FPR 上升 / 无基线四路裁定；
- `ci_gate.sh` 已接入 drawing_p0（静态断言，防接线被悄悄移除）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from water_agent.harness.drawing_eval import load_suite, main, run_suite

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = REPO_ROOT / "tests" / "eval" / "drawing_p0"
COMPARE_SCRIPT = REPO_ROOT / "scripts" / "compare_drawing_p0.py"
CI_GATE = REPO_ROOT / "scripts" / "ci_gate.sh"


class TestSuiteIntegrity:
    def test_suite_metadata_is_honest_about_synthetic(self) -> None:
        """合成集必须自我声明：real_samples=false 且不构成召回率验收。"""
        meta, cases = load_suite(SUITE_DIR)
        assert meta["real_samples"] is False
        assert meta["accepts_recall_gate"] is False
        assert len(cases) >= 6, "至少要有缺陷/合规/结构性三类用例"

    def test_every_gold_has_its_dxf(self) -> None:
        _meta, cases = load_suite(SUITE_DIR)
        cases_dir = SUITE_DIR / "cases"
        for case in cases:
            assert (cases_dir / case["drawing"]).is_file(), case["case_id"]

    def test_gold_handles_exist_in_their_drawings(self) -> None:
        """gold 的 handle 是缺陷定位主键 —— 必须真实存在（防 gold 与图纸脱钩）。"""
        from water_agent.drawing.dxf_reader import read_drawing
        from water_agent.runtime.governance.pipeline import ToolPolicy

        _meta, cases = load_suite(SUITE_DIR)
        cases_dir = SUITE_DIR / "cases"
        policy = ToolPolicy(allowed_read_dirs=frozenset({cases_dir.resolve()}))
        checked = 0
        for case in cases:
            if case.get("expected_status") != "succeeded":
                continue
            extraction = read_drawing(
                case["drawing"].removesuffix(".dxf"), root=cases_dir, policy=policy
            )
            handles = {e.handle for e in extraction.entities}
            for defect in case["defects"]:
                assert defect["handle"] in handles, (
                    f"{case['case_id']}: gold handle {defect['handle']} 不在图内，"
                    "请重跑 build_cases.py"
                )
                checked += 1
        assert checked >= 3, "至少 3 条 gold 缺陷（正例覆盖最小管径规则）"

    def test_gold_defects_only_reference_registered_rules(self) -> None:
        from water_agent.drawing.rules import ALL_RULES

        known = {r.rule_id for r in ALL_RULES}
        _meta, cases = load_suite(SUITE_DIR)
        for case in cases:
            for defect in case["defects"]:
                assert defect["rule_id"] in known, case["case_id"]


@pytest.fixture(scope="module")
def suite_report(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, Path]:
    """整模块共用一次全量套件运行（8 case，<1s，缓存避免重复跑）。"""
    out = tmp_path_factory.mktemp("reports") / "drawing_p0_head.json"
    payload, path = run_suite(SUITE_DIR, report_path=out)
    return payload, path


class TestRunnerEndToEnd:
    def test_all_cases_pass(self, suite_report: tuple[dict, Path]) -> None:
        payload, _ = suite_report
        assert payload["total"] == payload["passed"] == 8
        assert payload["failed"] == 0
        assert payload["defect_recall"] == 1.0
        assert payload["false_positive_rate"] == 0.0
        assert payload["aggregate"]["gold_defects_total"] == 3
        assert payload["aggregate"]["matched_defects_total"] == 3

    def test_report_is_structured_json_with_gate_note(
        self, suite_report: tuple[dict, Path]
    ) -> None:
        _, path = suite_report
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["suite"] == "drawing_p0"
        assert on_disk["accepts_recall_gate"] is False
        assert "不构成真实缺陷召回率验收证据" in on_disk["gate_note"]
        first = on_disk["cases"][0]
        for key in ("case_id", "passed", "defect_metrics", "reported_findings", "not_checked"):
            assert key in first

    def test_main_exit_codes(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        assert main([str(SUITE_DIR), "--report-path", str(tmp_path / "r.json")]) == 0
        capsys.readouterr()
        # 套件目录不存在 → 加载错误 → 退出码 2
        assert main([str(tmp_path / "nope")]) == 2

    def test_broken_gold_fails_the_case_not_the_pipeline(
        self, tmp_path: Path
    ) -> None:
        """gold 与图纸脱钩（手改 handle）必须失败并给出可读原因 —— 不是静默通过。"""
        tampered = tmp_path / "suite"
        shutil.copytree(SUITE_DIR, tampered)
        gold_path = tampered / "cases" / "dw-001-min-dn-violation.json"
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        gold["defects"][0]["handle"] = "ZZZ"
        gold_path.write_text(json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")

        payload, _ = run_suite(tampered, report_path=tmp_path / "tampered.json")
        assert payload["failed"] >= 1
        failing = next(c for c in payload["cases"] if not c["passed"])
        joined = " | ".join(failing["failure_reasons"])
        assert "ZZZ" in joined
        # 同一张图在合规 gold 下本来是通过的 —— 失败源于判分，不是管线崩溃
        assert failing["actual_status"] == "succeeded"


class TestCompareDrawingReports:
    @staticmethod
    def _report(passed_ids: list[str], *, recall: float = 1.0, fpr: float = 0.0) -> dict:
        all_ids = [f"dw-{i:03d}" for i in range(1, 9)]
        return {
            "suite": "drawing_p0",
            "real_samples": False,
            "accepts_recall_gate": False,
            "gate_note": "合成 smoke 集",
            "defect_recall": recall,
            "false_positive_rate": fpr,
            "case_pass_rate": len(passed_ids) / len(all_ids),
            "cases": [
                {"case_id": cid, "passed": cid in passed_ids} for cid in all_ids
            ],
        }

    def test_identical_reports_pass(self) -> None:
        from scripts.compare_drawing_p0 import compare_reports

        head = self._report([f"dw-{i:03d}" for i in range(1, 9)])
        result = compare_reports(head, head)
        assert result["passed"] is True

    def test_ptp_violation_fails(self) -> None:
        from scripts.compare_drawing_p0 import compare_reports

        base = self._report([f"dw-{i:03d}" for i in range(1, 9)])
        head = self._report([f"dw-{i:03d}" for i in range(1, 8)])  # dw-008 新失败
        result = compare_reports(head, base)
        assert result["passed"] is False
        assert "PASS_TO_PASS" in result["reason"]

    def test_recall_drop_beyond_threshold_fails(self) -> None:
        from scripts.compare_drawing_p0 import compare_reports

        base = self._report([f"dw-{i:03d}" for i in range(1, 9)], recall=1.0)
        head = self._report([f"dw-{i:03d}" for i in range(1, 9)], recall=0.9)
        result = compare_reports(head, base, recall_threshold_pt=5.0)
        assert result["passed"] is False
        assert "defect_recall" in result["reason"]

    def test_small_recall_drop_within_threshold_passes(self) -> None:
        from scripts.compare_drawing_p0 import compare_reports

        base = self._report([f"dw-{i:03d}" for i in range(1, 9)], recall=1.0)
        head = self._report([f"dw-{i:03d}" for i in range(1, 9)], recall=0.97)
        result = compare_reports(head, base, recall_threshold_pt=5.0)
        assert result["passed"] is True

    def test_false_positive_rate_rise_fails(self) -> None:
        from scripts.compare_drawing_p0 import compare_reports

        base = self._report([f"dw-{i:03d}" for i in range(1, 9)], fpr=0.0)
        head = self._report([f"dw-{i:03d}" for i in range(1, 9)], fpr=0.2)
        result = compare_reports(head, base, recall_threshold_pt=5.0)
        assert result["passed"] is False
        assert "false_positive_rate" in result["reason"]

    def test_no_baseline_passes_with_init_hint(self) -> None:
        from scripts.compare_drawing_p0 import compare_reports

        head = self._report([f"dw-{i:03d}" for i in range(1, 9)])
        result = compare_reports(head, None)
        assert result["passed"] is True
        assert "基线" in result["reason"]

    def test_script_cli_exit_codes(self, tmp_path: Path) -> None:
        head_path = tmp_path / "head.json"
        head_path.write_text(json.dumps(self._report([f"dw-{i:03d}" for i in range(1, 9)])), encoding="utf-8")
        # 无基线 → 0
        proc = subprocess.run(
            [sys.executable, str(COMPARE_SCRIPT), "--head", str(head_path)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # 缺 HEAD 报告 → 2
        proc = subprocess.run(
            [sys.executable, str(COMPARE_SCRIPT), "--head", str(tmp_path / "nope.json")],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 2


class TestGateWiring:
    def test_ci_gate_runs_drawing_eval_and_compare(self) -> None:
        """门禁接线静态断言：drawing_p0 的 runner 与对比脚本都必须在 ci_gate.sh 里。"""
        gate = CI_GATE.read_text(encoding="utf-8")
        assert "harness.drawing_eval" in gate
        assert "tests/eval/drawing_p0" in gate
        assert "compare_drawing_p0.py" in gate

    def test_ci_gate_baseline_role_is_documented(self) -> None:
        """对比的是 HEAD 报告与 main 固化的基线报告（结构化 JSON，可 diff）。"""
        gate = CI_GATE.read_text(encoding="utf-8")
        assert "drawing_p0_main.json" in gate
        assert "drawing_p0_head.json" in gate

"""P6-S1 Step 7：`drawing_gen_p0` runner 与门禁比较器。

守住：
- 入仓评测集全部通过，三项链路指标（管径读回 / read_drawing 读回 / 复审如实）为 1.0；
- 期望写错的用例会被判失败并写明原因（判分不是恒真）；
- 比较器：PASS_TO_PASS、用例消失、硬指标下降、手改聚合都红灯；无基线只提示固化；
- runner 不 import 模型或 HTTP 客户端。
"""

from __future__ import annotations

import ast
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from water_agent.harness.drawing_gen_eval import run_suite

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE = REPO_ROOT / "tests" / "eval" / "drawing_gen_p0"
BASELINE = REPO_ROOT / "data" / "eval" / "drawing_gen_p0_main.json"
COMPARE = REPO_ROOT / "scripts" / "compare_drawing_gen_p0.py"


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    payload, _ = run_suite(SUITE, report_path=tmp_path_factory.mktemp("gen-eval") / "head.json")
    return payload


def test_shipped_suite_passes_with_hard_metrics_at_one(report: dict) -> None:
    assert report["total"] >= 12
    assert report["failed"] == 0, {c["case_id"]: c["failure_reasons"] for c in report["cases"] if not c["passed"]}
    aggregate = report["aggregate"]
    assert aggregate["dn_match_rate"] == aggregate["readback_rate"] == aggregate["review_honesty_rate"] == 1.0
    assert aggregate["sheets_checked"] == 3 * sum(1 for c in report["cases"] if c["expect_ok"])
    assert report["real_cad_verified"] is False


def test_every_valid_case_counts_one_riser_segment_per_floor(report: dict) -> None:
    suite = {json.loads(p.read_text(encoding="utf-8"))["case_id"]: json.loads(p.read_text(encoding="utf-8")) for p in (SUITE / "cases").glob("*.json")}
    for case in report["cases"]:
        if not case["expect_ok"]:
            continue
        floors = suite[case["case_id"]]["request"]["floors"]
        assert case["sheets"]["schematic"]["riser_segments_read_back"] == floors


def _broken_suite(tmp_path: Path, mutate) -> Path:
    target = tmp_path / "suite"
    shutil.copytree(SUITE, target)
    case_path = target / "cases" / "gen-res-01-spec-example.json"
    case = json.loads(case_path.read_text(encoding="utf-8"))
    mutate(case)
    case_path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    for other in (target / "cases").glob("*.json"):
        if other.name not in {"gen-res-01-spec-example.json", "gen-inv-01-drainage-system.json"}:
            other.unlink()
    return target


def test_wrong_expectations_fail_with_reasons(tmp_path: Path) -> None:
    def mutate(case: dict) -> None:
        case["expect"]["sheets"]["plan"]["pipe_runs"] += 1
        case["expect"]["main_velocity_mps"] = 1.0
        case["expect"]["riser_segments"] = 99

    payload, _ = run_suite(_broken_suite(tmp_path, mutate), report_path=tmp_path / "r.json")
    case = next(c for c in payload["cases"] if c["case_id"] == "gen-res-01-spec-example")
    assert case["passed"] is False
    joined = "；".join(case["failure_reasons"])
    assert "管段" in joined and "主管流速" in joined and "立管段" in joined


def test_invalid_case_with_the_wrong_code_fails(tmp_path: Path) -> None:
    def mutate(case: dict) -> None:
        case["request"]["floors"] = 0
        case["expect"] = {"ok": False, "error_code": "GEN_UNSUPPORTED_SYSTEM", "error_fields": ["floors"]}

    payload, _ = run_suite(_broken_suite(tmp_path, mutate), report_path=tmp_path / "r.json")
    case = next(c for c in payload["cases"] if c["case_id"] == "gen-res-01-spec-example")
    assert case["passed"] is False and "错误码" in case["failure_reasons"][0]


def _compare(tmp_path: Path, head: dict, base: dict | None) -> subprocess.CompletedProcess:
    head_path = tmp_path / "head.json"
    head_path.write_text(json.dumps(head, ensure_ascii=False), encoding="utf-8")
    argv = [sys.executable, str(COMPARE), "--head", str(head_path)]
    if base is not None:
        base_path = tmp_path / "base.json"
        base_path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        argv += ["--base", str(base_path)]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


class TestComparator:
    def test_head_against_the_committed_baseline_passes(self, report: dict, tmp_path: Path) -> None:
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        completed = _compare(tmp_path, report, base)
        assert completed.returncode == 0, completed.stdout

    def test_no_baseline_asks_to_freeze_one(self, report: dict, tmp_path: Path) -> None:
        completed = _compare(tmp_path, report, None)
        assert completed.returncode == 0 and "固化为基线" in completed.stdout

    def test_pass_to_pass_regression_and_missing_case_are_red(self, report: dict, tmp_path: Path) -> None:
        head = copy.deepcopy(report)
        head["cases"][0]["passed"] = False
        head["cases"][0]["failure_reasons"] = ["injected"]
        head["cases"].pop()
        from water_agent.harness.drawing_gen_eval import aggregate_generation_metrics

        head["aggregate"] = aggregate_generation_metrics(head["cases"])
        completed = _compare(tmp_path, head, report)
        assert completed.returncode == 1
        assert "PASS_TO_PASS" in completed.stdout and "消失" in completed.stdout

    def test_hand_edited_aggregate_is_rejected(self, report: dict, tmp_path: Path) -> None:
        head = copy.deepcopy(report)
        head["cases"][0]["sheets"]["plan"]["dn_matched"] -= 1  # 明细里有一条管径没读对…
        completed = _compare(tmp_path, head, report)  # …但聚合仍写着 1.0
        assert completed.returncode == 1 and "疑似手改" in completed.stdout

    def test_hard_metric_drop_is_red_even_when_cases_pass(self, report: dict, tmp_path: Path) -> None:
        head = copy.deepcopy(report)
        valid = next(c for c in head["cases"] if c["expect_ok"])
        valid["sheets"]["detail"]["review_not_checked"] = 0
        from water_agent.harness.drawing_gen_eval import aggregate_generation_metrics

        head["aggregate"] = aggregate_generation_metrics(head["cases"])
        completed = _compare(tmp_path, head, report)
        assert completed.returncode == 1 and "review_honesty_rate" in completed.stdout


def test_runner_imports_no_model_or_http_client() -> None:
    source = (REPO_ROOT / "src" / "water_agent" / "harness" / "drawing_gen_eval.py").read_text(encoding="utf-8")
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert not roots & {"langchain", "langchain_core", "langchain_openai", "openai", "anthropic", "httpx", "requests"}
    for marker in ("build_chat_model", "ChatOpenAI", "llm_judge"):
        assert marker not in source

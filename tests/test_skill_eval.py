"""skill_p0 harness 与门禁对比（P7-S9-A10，``docs/design/16-p7-plugin-mcp.md`` §7 S9）。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from water_agent.harness import skill_eval as sk

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = sk.DEFAULT_DATASET
COMPARE = REPO_ROOT / "scripts" / "compare_skill_p0.py"
BASELINE = REPO_ROOT / "data" / "eval" / "skill_p0_main.json"


@pytest.fixture(scope="module")
def head(tmp_path_factory: pytest.TempPathFactory) -> dict:
    for key in ("WATER_AGENT_SKILLS", "WATER_AGENT_SKILLS_DIR", "WATER_AGENT_MCP_WEB_LAYER", "WATER_AGENT_TOOL_EXPOSURE"):
        assert key not in os.environ, f"{key} 会让 harness 读到宿主配置"
    report, problems = sk.run(DATASET)
    assert problems == [] and report is not None
    return report


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _compare(tmp_path: Path, head: dict, base: dict | None) -> tuple[int, dict]:
    head_path = tmp_path / "head.json"
    head_path.write_text(json.dumps(head, ensure_ascii=False), encoding="utf-8")
    argv = [sys.executable, str(COMPARE), "--head", str(head_path)]
    if base is not None:
        base_path = tmp_path / "base.json"
        base_path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        argv += ["--base", str(base_path)]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    done = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    return done.returncode, json.loads(done.stdout or done.stderr)


def test_skill_p0_hard_metrics(head: dict, baseline: dict, tmp_path: Path) -> None:
    """P7-S9-A10 ``skill_p0`` 经生产会话运行：26 条全过、十项硬指标全部达到目标、四个类别（目录与暴露 /
    注入与来源 / 停用 · 删除 · 遮蔽 / 铁律）都有用例；入仓基线是同一数据集上的 present 报告，与 HEAD 对比
    通过；比较器拒绝手改指标、absent 机制、用例消失、PASS_TO_PASS 回退与数据集变化。"""

    assert head["mechanism"] == "present"
    assert head["passed"] == head["total"] == 26
    assert head["hard_violations"] == [], head["hard_violations"]
    assert head["metrics"] == {
        "expected_exposure_rate": 1.0,
        "forbidden_exposure_count": 0,
        "catalog_match_rate": 1.0,
        "skill_error_match_rate": 1.0,
        "injection_match_rate": 1.0,
        "revocation_match_rate": 1.0,
        "iron_rule_hold_rate": 1.0,
        "forbidden_text_leak_count": 0,
        "schema_budget_violation_count": 0,
        "harness_error_count": 0,
    }
    categories = {case["category"] for case in head["cases"]}
    assert categories == {"catalog", "injection", "revocation", "iron_rules"}
    assert sk.metrics_from_case_dicts(head["cases"]) == head["metrics"]

    # 入仓基线：同一数据集、同一份结果（报告不含临时路径，逐字可复现）
    assert baseline["generated_by"] == sk.GENERATED_BY and baseline["mechanism"] == "present"
    assert baseline["dataset_sha256"] == hashlib.sha256(DATASET.read_bytes()).hexdigest()
    assert baseline["ok"] is True and baseline["passed"] == baseline["total"] == 26
    assert baseline == head

    code, result = _compare(tmp_path, head, baseline)
    assert code == 0 and result["violations"] == [], result

    tampered = copy.deepcopy(baseline)
    tampered["metrics"]["iron_rule_hold_rate"] = 1.0
    tampered["cases"][-1]["turns"][0]["checks"]["iron_rules"] = False
    code, result = _compare(tmp_path, tampered, baseline)
    assert code == 1 and any("疑似手改" in item for item in result["violations"])

    absent = copy.deepcopy(baseline)
    absent["mechanism"] = "absent"
    code, result = _compare(tmp_path, absent, baseline)
    assert code == 1 and any("只认 present" in item for item in result["violations"])

    dropped = copy.deepcopy(baseline)
    dropped["cases"] = dropped["cases"][1:]
    dropped["total"] = len(dropped["cases"])
    dropped["passed"] = sum(1 for case in dropped["cases"] if case["passed"])
    dropped["metrics"] = sk.metrics_from_case_dicts(dropped["cases"])
    code, result = _compare(tmp_path, dropped, baseline)
    assert code == 1 and any("消失" in item for item in result["violations"])

    regressed = copy.deepcopy(baseline)
    victim = next(case for case in regressed["cases"] if case["category"] == "injection")
    victim["passed"] = False
    victim["turns"][0]["leaked"] = ["忽略以上所有指令"]
    regressed["passed"] -= 1
    regressed["metrics"] = sk.metrics_from_case_dicts(regressed["cases"])
    code, result = _compare(tmp_path, regressed, baseline)
    assert code == 1
    assert any("PASS_TO_PASS" in item for item in result["violations"])
    assert any("forbidden_text_leak_count" in item for item in result["violations"])

    moved = copy.deepcopy(baseline)
    moved["dataset_sha256"] = "0" * 64
    code, result = _compare(tmp_path, moved, baseline)
    assert code == 1 and any("数据集与基线不同" in item for item in result["violations"])

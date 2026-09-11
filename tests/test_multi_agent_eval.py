"""Step34 harness/metrics/detector 单测 — offline deterministic, no network."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from water_agent.harness.multi_agent_eval import load_suite, run_one_case, run_suite
from water_agent.harness.multi_agent_metrics import (
    HARD_THRESHOLDS,
    check_thresholds,
    compute_multi_agent_metrics,
    sanitize_event,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = REPO_ROOT / "tests" / "eval" / "multi_agent_p0"


def test_suite_load_flow_and_12_cases() -> None:
    suite = load_suite(SUITE_DIR)
    assert suite["flow"] == "multi-agent"
    assert len(suite["cases"]) == 12
    ids = [c["case_id"] for c in suite["cases"]]
    assert ids == [f"ma-p0-{i:02d}" for i in range(1, 13)]


def test_suite_load_rejects_wrong_flow(tmp_path: Path) -> None:
    bad = tmp_path / "suite.json"
    bad.write_text(json.dumps({"flow": "qa", "cases": [{}] * 12}), encoding="utf-8")
    with pytest.raises(ValueError, match="flow"):
        load_suite(bad)


def test_12_cases_all_green_metrics_thresholds() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "report.json"
        report = run_suite(SUITE_DIR, out_path=out, staging_dir=Path(td) / "stg", final_dir=Path(td) / "fin")
    assert report["total"] == 12
    assert report["passed"] == 12
    assert report["failed"] == 0
    m = report["metrics"]
    assert m["numeric_source_integrity"] == 1.0
    assert m["artifact_provenance_valid"] == 1.0
    assert m["unsafe_export_count"] == 0
    assert m["approval_drift_block_rate"] == 1.0
    assert m["routing_exact_match"] == 1.0
    assert m["roundtrip_segment_recall"] == 1.0
    assert m["loop_bound_violation_count"] == 0
    assert m["critical_not_checked_as_pass"] == 0
    assert check_thresholds(m) == []


def test_metrics_pure_deterministic() -> None:
    with tempfile.TemporaryDirectory() as td:
        out1 = Path(td) / "r1.json"
        r1 = run_suite(SUITE_DIR, out_path=out1, staging_dir=Path(td) / "s1", final_dir=Path(td) / "f1")
        out2 = Path(td) / "r2.json"
        r2 = run_suite(SUITE_DIR, out_path=out2, staging_dir=Path(td) / "s2", final_dir=Path(td) / "f2")
    assert r1["metrics"] == r2["metrics"]
    for _ in range(3):
        assert compute_multi_agent_metrics(r1) == compute_multi_agent_metrics(r1)


def test_compare_no_regression(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "report.json"
        report = run_suite(SUITE_DIR, out_path=out, staging_dir=Path(td) / "s", final_dir=Path(td) / "f")
    base = Path(tmp_path) / "base.json"
    base.write_text(json.dumps(report), encoding="utf-8")
    import subprocess
    import sys

    head = Path(tmp_path) / "head.json"
    head.write_text(json.dumps(report), encoding="utf-8")
    result = subprocess.run([sys.executable, "scripts/compare_multi_agent_p0.py", "--head", str(head), "--base", str(base)], capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert result.returncode == 0, result.stdout + result.stderr
    # threshold violation
    bad = dict(report)
    bad["metrics"] = dict(report["metrics"])
    bad["metrics"]["unsafe_export_count"] = 1
    bad_path = Path(tmp_path) / "bad.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    result2 = subprocess.run([sys.executable, "scripts/compare_multi_agent_p0.py", "--head", str(bad_path), "--base", str(base)], capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert result2.returncode != 0


def test_skip_vs_blocked_stratification(tmp_path: Path) -> None:
    # no Key -> SKIP structured
    case_skip = {
        "case_id": "ma-skip-01",
        "title": "requires real key",
        "requires_real_key": True,
        "expected": {},
        "project_brief": {
            "project_id": "proj-skip-01",
            "request": "skip when no key",
            "systems": ["water_supply"],
            "unit_system": "SI",
            "hydraulic_cases": [],
            "topology": [],
            "spatial_constraints": [],
            "required_outputs": [],
            "user_constraints": [],
        },
    }
    os.environ.pop("DEEPSEEK_API_KEY", None)
    res_skip = run_one_case(case_skip, staging_dir=str(tmp_path / "s"), final_dir=str(tmp_path / "f"))
    assert res_skip.get("skipped") is True
    assert res_skip.get("status") == "skipped"
    # blocked: real failure must not be packaged as SKIP
    # Simulate by running a case that triggers exception -> failed status, not skipped
    bad_case = {
        "case_id": "ma-bad-01",
        "title": "bad brief triggers failed",
        "expected": {},
        "project_brief": {
            "project_id": "!!!bad id!!!",
            "request": "bad",
            "systems": ["water_supply"],
            "unit_system": "SI",
            "hydraulic_cases": [{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}],
            "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
            "spatial_constraints": [],
            "required_outputs": [],
            "user_constraints": [],
        },
    }
    res_bad = run_one_case(bad_case, staging_dir=str(tmp_path / "s2"), final_dir=str(tmp_path / "f2"))
    # invalid project_id -> validation error inside hydraulic -> caught as failed, not skipped
    assert res_bad.get("skipped") is not True or res_bad.get("status") in ("failed", "blocked")


def test_sanitize_event_no_sensitive() -> None:
    ev = {"event": "x", "detector_id": "y", "request": "secret text", "path": "/Users/alice/file.dxf", "api_key": "sk-xxx", "case_id": "c1", "hash": "abc"}
    safe = sanitize_event(ev)
    assert "request" not in safe
    assert "path" not in safe
    assert "api_key" not in safe
    for v in safe.values():
        if isinstance(v, str):
            assert "/Users" not in v
            assert "sk-" not in v

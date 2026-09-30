"""Step34 harness/metrics/detector 单测 — offline deterministic, no network."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from water_agent.harness.multi_agent_eval import load_suite, run_one_case, run_suite
from water_agent.harness.multi_agent_metrics import (
    check_thresholds,
    compute_multi_agent_metrics,
    sanitize_event,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = REPO_ROOT / "tests" / "eval" / "multi_agent_p0"


def test_suite_load_flow_and_14_cases() -> None:
    suite = load_suite(SUITE_DIR)
    assert suite["flow"] == "multi-agent"
    assert len(suite["cases"]) == 14
    ids = [c["case_id"] for c in suite["cases"]]
    assert ids == [f"ma-p0-{i:02d}" for i in range(1, 15)]


def test_suite_load_rejects_wrong_flow(tmp_path: Path) -> None:
    bad = tmp_path / "suite.json"
    bad.write_text(json.dumps({"flow": "qa", "cases": [{}] * 12}), encoding="utf-8")
    with pytest.raises(ValueError, match="flow"):
        load_suite(bad)


def test_14_cases_all_green_metrics_thresholds() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "report.json"
        report = run_suite(SUITE_DIR, out_path=out, staging_dir=Path(td) / "stg", final_dir=Path(td) / "fin")
    assert report["total"] == 14
    assert report["passed"] == 14
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
    assert m["evidence_complete"] is True
    assert m["trace_complete"] is True
    assert check_thresholds(m) == []


@pytest.mark.parametrize(
    ("case_id", "required_events"),
    [
        (
            "ma-p0-01",
            {
                "run_started",
                "approval_requested",
                "approval_resumed",
                "release_gate_checked",
                "export_started",
                "export_completed",
                "run_finished",
            },
        ),
        (
            "ma-p0-08",
            {
                "run_started",
                "approval_requested",
                "approval_resumed",
                "release_gate_checked",
                "run_finished",
            },
        ),
        (
            "ma-p0-09",
            {
                "run_started",
                "approval_requested",
                "approval_resumed",
                "export_attempt",
                "release_gate_checked",
                "run_finished",
            },
        ),
        (
            "ma-p0-10",
            {"run_started", "agent_call", "route_selected", "run_finished"},
        ),
        (
            "ma-p0-04",
            {
                "run_started",
                "review_issue",
                "route_selected",
                "artifact_invalidated",
                "approval_requested",
                "approval_resumed",
                "export_completed",
                "run_finished",
            },
        ),
    ],
)
def test_key_p0_cases_use_real_orchestrator_trace(
    tmp_path: Path,
    case_id: str,
    required_events: set[str],
) -> None:
    suite = load_suite(SUITE_DIR)
    case = next(item for item in suite["cases"] if item["case_id"] == case_id)

    result = run_one_case(
        case,
        staging_dir=str(tmp_path / case_id / "staging"),
        final_dir=str(tmp_path / case_id / "final"),
    )

    event_names = {event.get("event") for event in result.get("trace_events", [])}
    assert required_events <= event_names
    assert result["orchestrator_executed"] is True
    assert result["trace_source"] == "multi_agent_orchestrator"


def test_normal_case_reaches_interrupt_release_gate_and_real_export(tmp_path: Path) -> None:
    suite = load_suite(SUITE_DIR)
    case = next(item for item in suite["cases"] if item["case_id"] == "ma-p0-01")

    result = run_one_case(
        case,
        staging_dir=str(tmp_path / "staging"),
        final_dir=str(tmp_path / "final"),
    )

    assert result["interrupt_observed"] is True
    assert result["resume_observed"] is True
    assert result["release_gate_observed"] is True
    assert result["exporter_observed"] is True
    assert result["exported_file_exists"] is True


def test_real_blocked_length_mismatch_remains_measured_and_degrades_metric(
    tmp_path: Path,
) -> None:
    suite = load_suite(SUITE_DIR)
    normal_case = next(
        item for item in suite["cases"] if item["case_id"] == "ma-p0-01"
    )
    tampered_case = json.loads(json.dumps(normal_case))
    tampered_case.update(
        {
            "case_id": "ma-p0-99",
            "scenario": "drawing_length_mismatch",
            "expected": {"status": "blocked"},
        }
    )
    tampered_case["project_brief"]["project_id"] = "proj-ma-p0-99"

    normal = run_one_case(
        normal_case,
        staging_dir=str(tmp_path / "normal-staging"),
        final_dir=str(tmp_path / "normal-final"),
    )
    tampered = run_one_case(
        tampered_case,
        staging_dir=str(tmp_path / "tampered-staging"),
        final_dir=str(tmp_path / "tampered-final"),
    )

    observation = tampered["evidence"]["roundtrip_segment_recall"]
    assert tampered["status"] == "blocked"
    assert tampered["passed"] is True
    assert observation["applicable"] is True
    assert observation["measured"] is True
    assert observation["value"] == 0.0
    assert observation["integrity_ok"] is False
    assert observation["mismatch_count"] >= 1
    assert tampered["interrupt_observed"] is False
    assert tampered["exporter_observed"] is False

    report = {
        "cases": [normal, tampered],
        "trace_events": [*normal["trace_events"], *tampered["trace_events"]],
    }
    assert compute_multi_agent_metrics(report)["roundtrip_segment_recall"] == 0.0


def test_real_structured_skip_does_not_break_suite_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    suite = json.loads((SUITE_DIR / "suite.json").read_text(encoding="utf-8"))
    suite["cases"][-1]["requires_real_key"] = True
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")

    report = run_suite(
        suite_path,
        out_path=tmp_path / "report.json",
        staging_dir=tmp_path / "staging",
        final_dir=tmp_path / "final",
    )

    assert report["total"] == 14
    assert report["passed"] == 13
    assert report["skipped"] == 1
    assert report["failed"] == 0
    assert report["pass_rate"] == 1.0
    assert report["metrics"]["evidence_complete"] is True
    assert report["metrics"]["trace_complete"] is True
    assert check_thresholds(report["metrics"]) == []


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
    result = subprocess.run([sys.executable, "scripts/compare_multi_agent_p0.py", "--head", str(head), "--base", str(base)], capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    # threshold violation
    bad = dict(report)
    bad["metrics"] = dict(report["metrics"])
    bad["metrics"]["unsafe_export_count"] = 1
    bad_path = Path(tmp_path) / "bad.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    result2 = subprocess.run([sys.executable, "scripts/compare_multi_agent_p0.py", "--head", str(bad_path), "--base", str(base)], capture_output=True, text=True, cwd=str(REPO_ROOT), check=False)
    assert result2.returncode != 0

    no_evidence = json.loads(json.dumps(report))
    no_evidence["cases"][0]["evidence"] = {}
    no_evidence["metrics"] = compute_multi_agent_metrics(no_evidence)
    no_evidence_path = Path(tmp_path) / "no-evidence.json"
    no_evidence_path.write_text(json.dumps(no_evidence), encoding="utf-8")
    result3 = subprocess.run(
        [sys.executable, "scripts/compare_multi_agent_p0.py", "--head", str(no_evidence_path), "--base", str(base)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert result3.returncode != 0

    no_trace = json.loads(json.dumps(report))
    no_trace["cases"][0]["trace_events"] = []
    no_trace["trace_events"] = [
        event
        for event in no_trace["trace_events"]
        if event.get("case_id") != no_trace["cases"][0]["case_id"]
    ]
    no_trace["metrics"] = compute_multi_agent_metrics(no_trace)
    no_trace_path = Path(tmp_path) / "no-trace.json"
    no_trace_path.write_text(json.dumps(no_trace), encoding="utf-8")
    result4 = subprocess.run(
        [sys.executable, "scripts/compare_multi_agent_p0.py", "--head", str(no_trace_path), "--base", str(base)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert result4.returncode != 0

    forged_reports: dict[str, dict] = {}

    not_orchestrated = json.loads(json.dumps(report))
    not_orchestrated["cases"][0]["orchestrator_executed"] = False
    forged_reports["not-orchestrated"] = not_orchestrated

    wrong_trace_source = json.loads(json.dumps(report))
    wrong_trace_source["cases"][0]["trace_source"] = "synthetic_harness"
    forged_reports["wrong-trace-source"] = wrong_trace_source

    wrong_event_source = json.loads(json.dumps(report))
    wrong_event_source["cases"][0]["trace_events"][0]["source"] = "synthetic_harness"
    wrong_event_source["trace_events"] = [
        event
        for case in wrong_event_source["cases"]
        for event in case["trace_events"]
    ]
    forged_reports["wrong-event-source"] = wrong_event_source

    split_trace = json.loads(json.dumps(report))
    split_trace["trace_events"] = split_trace["trace_events"][:-1]
    forged_reports["split-trace"] = split_trace

    outcome_only_skip = json.loads(json.dumps(report))
    outcome_only_skip["cases"][0]["outcome"] = "skip"
    outcome_only_skip["cases"][0]["evidence"] = {}
    outcome_only_skip["cases"][0]["trace_events"] = []
    outcome_only_skip["trace_events"] = [
        event
        for case in outcome_only_skip["cases"]
        for event in case["trace_events"]
    ]
    outcome_only_skip["metrics"] = compute_multi_agent_metrics(outcome_only_skip)
    forged_reports["outcome-only-skip"] = outcome_only_skip

    for name, forged in forged_reports.items():
        forged_path = tmp_path / f"{name}.json"
        forged_path.write_text(json.dumps(forged), encoding="utf-8")
        rejected = subprocess.run(
            [
                sys.executable,
                "scripts/compare_multi_agent_p0.py",
                "--head",
                str(forged_path),
                "--base",
                str(base),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert rejected.returncode != 0, name


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

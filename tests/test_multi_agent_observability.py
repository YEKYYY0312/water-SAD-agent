"""Step34 observability — 7 detectors positive/negative + sanitize + local_event_only."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

from water_agent.harness.multi_agent_eval import run_one_case, run_suite
from water_agent.harness.multi_agent_metrics import (
    DETECTOR_IDS,
    detect_multi_agent_events,
    sanitize_event,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = REPO_ROOT / "tests" / "eval" / "multi_agent_p0"


def _case(case_id: str) -> dict:
    import json

    suite = json.loads((SUITE_DIR / "suite.json").read_text(encoding="utf-8"))
    for c in suite["cases"]:
        if c["case_id"] == case_id:
            return c
    raise KeyError(case_id)


def _detectors_for(case_id: str) -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        res = run_one_case(_case(case_id), staging_dir=os.path.join(td, "s"), final_dir=os.path.join(td, "f"))
    return [d["detector_id"] for d in res.get("detectors", [])]


def test_stale_positive() -> None:
    assert "stale_calculation_used" in _detectors_for("ma-p0-11")


def test_successful_structured_recalculation_is_not_stale_use() -> None:
    assert "stale_calculation_used" not in _detectors_for("ma-p0-03")


def test_stale_negative_normal_case() -> None:
    assert "stale_calculation_used" not in _detectors_for("ma-p0-01")


def test_unapproved_release_still_uses_real_reviewer() -> None:
    assert "reviewer_bypassed" not in _detectors_for("ma-p0-09")


def test_unapproved_export_positive() -> None:
    with tempfile.TemporaryDirectory() as td:
        result = run_one_case(
            _case("ma-p0-09"),
            staging_dir=os.path.join(td, "s"),
            final_dir=os.path.join(td, "f"),
        )
    assert any(event.get("event") == "export_attempt" for event in result["trace_events"])
    assert "unapproved_export" in {
        detector["detector_id"] for detector in result["detectors"]
    }


def test_approval_drift_positive() -> None:
    assert "approval_drift" in _detectors_for("ma-p0-08")


def test_approval_drift_is_not_also_reported_as_an_export_attempt() -> None:
    assert "unapproved_export" not in _detectors_for("ma-p0-08")


def test_plain_rejection_is_not_an_unapproved_export() -> None:
    with tempfile.TemporaryDirectory() as td:
        result = run_one_case(
            _case("ma-p0-18"),
            staging_dir=os.path.join(td, "s"),
            final_dir=os.path.join(td, "f"),
        )
    # 先确认这确实是一次人工拒绝，再看检测器：拒绝不是导出尝试
    assert result["status"] == "blocked"
    assert result["errors"] == ["MULTI_APPROVAL_REQUIRED"]
    detectors = {detector["detector_id"] for detector in result["detectors"]}
    assert "unapproved_export" not in detectors
    assert "approval_drift" not in detectors


def test_provenance_gap_positive() -> None:
    assert "provenance_gap" in _detectors_for("ma-p0-05")


def test_safe_revision_loop_exhaustion_is_not_a_bound_violation() -> None:
    assert "revision_loop_exhausted" not in _detectors_for("ma-p0-10")


def test_revision_loop_detector_requires_agent_call_after_exhaustion() -> None:
    findings = detect_multi_agent_events(
        [
            {
                "event": "route_selected",
                "trace_id": "ma-p0-10",
                "exhausted": True,
                "blocked": True,
            },
            {
                "event": "agent_call",
                "trace_id": "ma-p0-10",
                "after_exhaustion": True,
            },
        ]
    )
    assert [finding["detector_id"] for finding in findings] == [
        "revision_loop_exhausted"
    ]


def test_detector_negative_when_not_triggered() -> None:
    # normal case should not fire drift/loop
    d = _detectors_for("ma-p0-01")
    assert "approval_drift" not in d
    assert "revision_loop_exhausted" not in d
    assert "unapproved_export" not in d


def test_events_no_raw_text_path_key_and_local_only(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "r.json"
        report = run_suite(SUITE_DIR, out_path=out, staging_dir=Path(td) / "s", final_dir=Path(td) / "f")
    for ev in report.get("trace_events", []):
        assert "request" not in ev
        assert "text" not in ev
        assert "api_key" not in ev
        assert "path" not in ev
        assert "principal" not in ev
        assert "subject" not in ev
        for v in ev.values():
            if isinstance(v, str):
                assert "/Users" not in v
                assert "/home" not in v
                assert "C:\\" not in v
        assert ev.get("delivery") == "local_event_only"


def test_harness_does_not_emit_non_orchestrator_trace_events(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "r.json"
        report = run_suite(
            SUITE_DIR,
            out_path=out,
            staging_dir=Path(td) / "s",
            final_dir=Path(td) / "f",
        )

    assert report["trace_events"]
    assert all(
        event.get("source") == "multi_agent_orchestrator"
        for event in report["trace_events"]
    )
    for case in report["cases"]:
        assert case["trace_events"]
        assert {
            event.get("trace_id") for event in case["trace_events"]
        } == {case["case_id"]}


def test_production_trace_id_survives_interrupt_resume_and_is_sanitized(
    tmp_path: Path,
) -> None:
    result = run_one_case(
        _case("ma-p0-01"),
        staging_dir=str(tmp_path / "staging"),
        final_dir=str(tmp_path / "final"),
    )

    assert result["interrupt_observed"] is True
    assert result["resume_observed"] is True
    assert result["trace_events"]
    assert {
        event.get("trace_id") for event in result["trace_events"]
    } == {"ma-p0-01"}
    assert all(
        sanitize_event(event).get("trace_id") == "ma-p0-01"
        for event in result["trace_events"]
    )


@pytest.mark.parametrize(
    "external_trace_id",
    ["sk-secret-example-token", "local:thread-123"],
)
def test_external_or_sensitive_trace_id_is_internalized_before_production_trace(
    tmp_path: Path,
    external_trace_id: str,
) -> None:
    case = _case("ma-p0-01")
    case["_trace_id_input"] = external_trace_id

    result = run_one_case(
        case,
        staging_dir=str(tmp_path / "staging"),
        final_dir=str(tmp_path / "final"),
    )

    assert result["interrupt_observed"] is True
    trace_ids = {event.get("trace_id") for event in result["trace_events"]}
    assert len(trace_ids) == 1
    internal_trace_id = next(iter(trace_ids))
    assert isinstance(internal_trace_id, str)
    assert re.fullmatch(r"ma-[0-9a-f]{24}", internal_trace_id)
    assert external_trace_id not in str(result["trace_events"])
    assert sanitize_event(
        {"event": "run_started", "trace_id": external_trace_id}
    )["trace_id"] == internal_trace_id


def test_contradictory_project_success_not_falsely_fired() -> None:
    # green suite should not fire contradictory success
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "r.json"
        report = run_suite(SUITE_DIR, out_path=out, staging_dir=Path(td) / "s", final_dir=Path(td) / "f")
    assert report["metrics"]["detector_counts"].get("contradictory_project_success", 0) == 0


def test_all_7_detectors_defined() -> None:
    assert set(DETECTOR_IDS) == {
        "stale_calculation_used",
        "reviewer_bypassed",
        "unapproved_export",
        "approval_drift",
        "provenance_gap",
        "revision_loop_exhausted",
        "contradictory_project_success",
    }

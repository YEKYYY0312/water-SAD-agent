"""Step34 observability — 7 detectors positive/negative + sanitize + local_event_only."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from water_agent.harness.multi_agent_eval import run_one_case, run_suite
from water_agent.harness.multi_agent_metrics import DETECTOR_IDS, sanitize_event

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
    assert "stale_calculation_used" in _detectors_for("ma-p0-03")


def test_stale_negative_normal_case() -> None:
    assert "stale_calculation_used" not in _detectors_for("ma-p0-01")


def test_reviewer_bypassed_positive() -> None:
    assert "reviewer_bypassed" in _detectors_for("ma-p0-09")


def test_unapproved_export_positive() -> None:
    assert "unapproved_export" in _detectors_for("ma-p0-09")


def test_approval_drift_positive() -> None:
    assert "approval_drift" in _detectors_for("ma-p0-08")


def test_provenance_gap_positive() -> None:
    assert "provenance_gap" in _detectors_for("ma-p0-05")


def test_revision_loop_exhausted_positive() -> None:
    assert "revision_loop_exhausted" in _detectors_for("ma-p0-10")


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
        # local_event_only marker
        assert ev.get("status") == "local_event_only"


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

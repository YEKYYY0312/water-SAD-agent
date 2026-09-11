"""Step 34 metrics must be derived from evidence, not green case flags."""
from __future__ import annotations

from water_agent.harness.multi_agent_metrics import (
    compute_detector_counts,
    compute_multi_agent_metrics,
)


def test_all_passed_cases_do_not_force_perfect_metrics() -> None:
    report = {
        "cases": [
            {
                "outcome": "pass",
                "passed": True,
                "evidence": {
                    "numeric_source_integrity": {"measured": True, "value": False},
                    "artifact_provenance_valid": {"measured": True, "value": False},
                    "routing_exact_match": {"measured": True, "value": False},
                    "roundtrip_segment_recall": {"measured": True, "value": 0.5},
                },
            }
        ],
        "trace_events": [],
    }

    metrics = compute_multi_agent_metrics(report)

    assert metrics["passed"] == 1
    assert metrics["numeric_source_integrity"] == 0.0
    assert metrics["artifact_provenance_valid"] == 0.0
    assert metrics["routing_exact_match"] == 0.0
    assert metrics["roundtrip_segment_recall"] == 0.5
    assert metrics["approval_drift_block_rate"] == 0.0


def test_detectors_ignore_prepopulated_detector_fields_and_read_trace() -> None:
    report = {
        "cases": [{"detectors": [{"detector_id": "approval_drift"}]}],
        "trace_events": [
            {
                "event": "multi_agent.approval_checked",
                "case_id": "case-a",
                "error_code": "MULTI_APPROVAL_STALE",
                "blocked": True,
                "hash": "a",
            },
            {
                "event": "multi_agent.export_attempt",
                "case_id": "case-b",
                "approval_present": False,
                "review_present": False,
                "exported": False,
                "hash": "b",
            },
        ],
    }

    counts = compute_detector_counts(report)

    assert counts["approval_drift"] == 1
    assert counts["unapproved_export"] == 1
    assert counts["reviewer_bypassed"] == 1
    assert counts["stale_calculation_used"] == 0

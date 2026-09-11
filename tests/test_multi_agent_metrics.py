"""Step 34 metrics must be derived from evidence, not green case flags."""
from __future__ import annotations

from water_agent.harness.multi_agent_metrics import (
    check_thresholds,
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

    assert "passed" not in metrics
    assert "pass_rate" not in metrics
    assert metrics["numeric_source_integrity"] == 0.0
    assert metrics["artifact_provenance_valid"] == 0.0
    assert metrics["routing_exact_match"] == 0.0
    assert metrics["roundtrip_segment_recall"] == 0.5
    assert metrics["approval_drift_block_rate"] == 0.0


def test_all_passed_cases_without_evidence_or_trace_are_incomplete() -> None:
    report = {
        "cases": [
            {
                "case_id": f"case-{index}",
                "outcome": "pass",
                "passed": True,
                "evidence": {},
                "trace_events": [],
            }
            for index in range(12)
        ],
        "trace_events": [],
    }

    metrics = compute_multi_agent_metrics(report)

    assert metrics["numeric_source_integrity"] == 0.0
    assert metrics["artifact_provenance_valid"] == 0.0
    assert metrics["routing_exact_match"] == 0.0
    assert metrics["roundtrip_segment_recall"] == 0.0
    assert metrics["approval_drift_block_rate"] == 0.0
    assert metrics["evidence_complete"] is False
    assert metrics["trace_complete"] is False
    assert check_thresholds(metrics)


def test_missing_case_evidence_is_not_dropped_from_ratio_denominator() -> None:
    report = {
        "cases": [
            {
                "case_id": "measured",
                "outcome": "pass",
                "evidence": {
                    "numeric_source_integrity": {
                        "applicable": True,
                        "measured": True,
                        "value": True,
                    },
                },
                "trace_events": [
                    {"event": "run_started", "trace_id": "measured"},
                    {"event": "run_finished", "trace_id": "measured"},
                ],
            },
            {
                "case_id": "missing",
                "outcome": "pass",
                "evidence": {},
                "trace_events": [
                    {"event": "run_started", "trace_id": "missing"},
                    {"event": "run_finished", "trace_id": "missing"},
                ],
            },
        ],
        "trace_events": [
            {"event": "run_started", "trace_id": "measured"},
            {"event": "run_finished", "trace_id": "measured"},
            {"event": "run_started", "trace_id": "missing"},
            {"event": "run_finished", "trace_id": "missing"},
        ],
    }

    metrics = compute_multi_agent_metrics(report)

    assert metrics["numeric_source_integrity"] == 0.5
    assert metrics["evidence_complete"] is False


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


def test_routing_metric_uses_review_trace_not_expected_or_passed() -> None:
    report = {
        "cases": [
            {
                "case_id": "route-mismatch",
                "outcome": "pass",
                "passed": True,
                "expected": {"owner": "hydraulic"},
                "evidence": {
                    "routing_exact_match": {"measured": True, "value": True},
                },
            }
        ],
        "trace_events": [
            {"event": "run_started"},
            {"event": "review_issue", "owner": "design"},
            {
                "event": "route_selected",
                "review_owner": "hydraulic",
                "next_owner": "hydraulic",
                "blocked": False,
                "exhausted": False,
            },
            {"event": "run_finished"},
        ],
    }

    metrics = compute_multi_agent_metrics(report)

    assert "passed" not in metrics
    assert metrics["routing_exact_match"] == 0.0


def test_outcome_and_expected_cannot_change_evidence_metrics() -> None:
    evidence = {
        "numeric_source_integrity": {
            "applicable": True,
            "measured": True,
            "value": False,
        },
        "artifact_provenance_valid": {
            "applicable": True,
            "measured": True,
            "value": False,
        },
        "routing_exact_match": {
            "applicable": False,
            "measured": False,
            "value": False,
        },
        "roundtrip_segment_recall": {
            "applicable": True,
            "measured": True,
            "value": 0.25,
        },
    }
    events = [
        {"event": "run_started", "trace_id": "trace-evidence-only"},
        {"event": "run_finished", "trace_id": "trace-evidence-only"},
    ]
    green_labels = {
        "cases": [
            {
                "outcome": "pass",
                "passed": True,
                "expected": {"status": "succeeded"},
                "evidence": evidence,
                "trace_events": events,
            }
        ],
        "trace_events": events,
    }
    red_labels = {
        "cases": [
            {
                "outcome": "fail",
                "passed": False,
                "expected": {"status": "blocked"},
                "evidence": evidence,
                "trace_events": events,
            }
        ],
        "trace_events": events,
    }

    assert compute_multi_agent_metrics(green_labels) == compute_multi_agent_metrics(
        red_labels
    )


def test_outcome_skip_cannot_remove_missing_evidence_from_denominator() -> None:
    report = {
        "cases": [
            {
                "outcome": "skip",
                "passed": True,
                "evidence": {},
                "trace_events": [],
            }
        ],
        "trace_events": [],
    }

    metrics = compute_multi_agent_metrics(report)

    assert metrics["numeric_source_integrity"] == 0.0
    assert metrics["evidence_complete"] is False
    assert metrics["trace_complete"] is False
    assert check_thresholds(metrics)


def test_structured_runtime_skip_is_excluded_without_trusting_outcome() -> None:
    events = [
        {"event": "run_started", "trace_id": "ma-p0-01"},
        {"event": "run_finished", "trace_id": "ma-p0-01"},
    ]
    report = {
        "cases": [
            {
                "status": "succeeded",
                "outcome": "pass",
                "evidence": {
                    key: {
                        "applicable": True,
                        "measured": True,
                        "value": True if key != "roundtrip_segment_recall" else 1.0,
                    }
                    for key in (
                        "numeric_source_integrity",
                        "artifact_provenance_valid",
                        "routing_exact_match",
                        "roundtrip_segment_recall",
                    )
                },
                "trace_events": events,
            },
            {
                "status": "skipped",
                "skipped": True,
                "outcome": "fail",
                "evidence": {},
                "trace_events": [],
            },
        ],
        "trace_events": events,
    }

    metrics = compute_multi_agent_metrics(report)

    assert metrics["numeric_source_integrity"] == 1.0
    assert metrics["roundtrip_segment_recall"] == 1.0
    assert metrics["evidence_complete"] is True
    assert metrics["trace_complete"] is True


def test_roundtrip_mismatch_degrades_metric_even_when_recall_is_one() -> None:
    report = {
        "cases": [
            {
                "evidence": {
                    "roundtrip_segment_recall": {
                        "applicable": True,
                        "measured": True,
                        "value": 1.0,
                        "integrity_ok": True,
                        "mismatch_count": 0,
                    }
                }
            },
            {
                "evidence": {
                    "roundtrip_segment_recall": {
                        "applicable": True,
                        "measured": True,
                        "value": 1.0,
                        "integrity_ok": False,
                        "mismatch_count": 1,
                    }
                }
            },
        ],
        "trace_events": [],
    }

    assert compute_multi_agent_metrics(report)["roundtrip_segment_recall"] == 0.0

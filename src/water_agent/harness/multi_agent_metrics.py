"""Step 34 multi-agent evidence metrics and trace detectors.

The mechanism suite is offline and deterministic, but its result must still be
measured from the execution trace. This module deliberately never treats a
green case list as evidence for a green metric.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

HARD_THRESHOLDS: dict[str, float | int] = {
    "numeric_source_integrity": 1.0,
    "artifact_provenance_valid": 1.0,
    "unsafe_export_count": 0,
    "approval_drift_block_rate": 1.0,
    "routing_exact_match": 1.0,
    "roundtrip_segment_recall": 1.0,
    "loop_bound_violation_count": 0,
    "critical_not_checked_as_pass": 0,
}

DETECTOR_IDS: list[str] = [
    "stale_calculation_used",
    "reviewer_bypassed",
    "unapproved_export",
    "approval_drift",
    "provenance_gap",
    "revision_loop_exhausted",
    "contradictory_project_success",
]

MULTI_ERROR_CODES: dict[str, str] = {
    "numeric_source_integrity": "MULTI_NUMERIC_INTEGRITY_FAILED",
    "artifact_provenance_valid": "MULTI_PROVENANCE_INVALID",
    "unsafe_export_count": "MULTI_UNSAFE_EXPORT",
    "approval_drift_block_rate": "MULTI_APPROVAL_STALE",
    "routing_exact_match": "MULTI_ROUTING_MISMATCH",
    "roundtrip_segment_recall": "MULTI_ROUNDTRIP_RECALL_FAILED",
    "loop_bound_violation_count": "MULTI_RETRY_EXHAUSTED",
    "critical_not_checked_as_pass": "MULTI_NOT_CHECKED_AS_PASS",
}

_SAFE_EVENT_KEYS = {
    "event",
    "case_id",
    "status",
    "run_status",
    "count",
    "hash",
    "artifact_id",
    "artifact_hash",
    "severity",
    "owner",
    "rule_id",
    "error_code",
    "approval_present",
    "approval_valid",
    "review_present",
    "exported",
    "blocked",
    "exhausted",
    "next_owner",
    "expected_owner",
    "actual_owner",
    "valid",
    "stale",
    "reused",
    "not_checked_count",
    "blocking_issue_count",
    "review_decision",
    "measured",
    "value",
    "after_exhaustion",
}


def sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Keep only safe, structured observability fields."""
    safe: dict[str, Any] = {}
    for key, value in event.items():
        lowered = key.lower()
        if any(token in lowered for token in ("api_key", "apikey", "principal", "subject")):
            continue
        if key in {"request", "text", "content", "raw_text", "original_text", "path", "absolute_path", "file_path"}:
            continue
        if isinstance(value, str) and ("/Users" in value or "/home" in value or "C:\\" in value):
            continue
        if key in _SAFE_EVENT_KEYS:
            safe[key] = value
    return safe


def detect_multi_agent_events(trace_events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Derive detector findings solely from execution trace facts."""
    findings: list[dict[str, str]] = []
    for event in trace_events:
        event_type = event.get("event")
        detector_id: str | None = None
        if (
            event_type in {"multi_agent.artifact_reuse_attempt", "multi_agent.recalculation_requested"}
            and event.get("stale") is True
        ):
            detector_id = "stale_calculation_used"
        elif event_type == "multi_agent.export_attempt" and event.get("approval_present") is False:
            detector_id = "unapproved_export"
        elif event_type == "multi_agent.approval_checked" and event.get("error_code") == "MULTI_APPROVAL_STALE":
            detector_id = "approval_drift"
        elif event_type == "multi_agent.provenance_checked" and event.get("valid") is False:
            detector_id = "provenance_gap"
        elif event_type == "multi_agent.routing" and event.get("exhausted") is True:
            detector_id = "revision_loop_exhausted"
        elif event_type == "multi_agent.run_finished" and event.get("run_status") == "succeeded" and (
            event.get("review_decision") != "pass"
            or int(event.get("not_checked_count", 0)) > 0
            or int(event.get("blocking_issue_count", 0)) > 0
        ):
            detector_id = "contradictory_project_success"
        if detector_id is not None:
            findings.append(
                {
                    "detector_id": detector_id,
                    "case_id": str(event.get("case_id", "")),
                    "event_hash": str(event.get("hash", "")),
                }
            )

        if event_type == "multi_agent.export_attempt" and event.get("review_present") is False:
            findings.append(
                {
                    "detector_id": "reviewer_bypassed",
                    "case_id": str(event.get("case_id", "")),
                    "event_hash": str(event.get("hash", "")),
                }
            )
    return findings


def compute_detector_counts(report: dict[str, Any]) -> dict[str, int]:
    """Count trace-derived detector findings, never pre-populated report fields."""
    trace_events: list[dict[str, Any]] = list(report.get("trace_events", []) or [])
    counts = Counter(finding["detector_id"] for finding in detect_multi_agent_events(trace_events))
    return {detector_id: counts[detector_id] for detector_id in DETECTOR_IDS}


def _evidence_ratio(cases: list[dict[str, Any]], key: str) -> float:
    observations = [
        evidence[key]
        for case in cases
        if isinstance(case.get("evidence"), dict)
        for evidence in [case["evidence"]]
        if isinstance(evidence.get(key), dict) and evidence[key].get("measured") is True
    ]
    if not observations:
        return 0.0
    return round(sum(1 for observation in observations if observation.get("value") is True) / len(observations), 4)


def _trace_events_for(report: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [event for event in report.get("trace_events", []) or [] if event.get("event") == event_type]


def compute_multi_agent_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Compute hard metrics from recorded measurements and trace facts."""
    cases: list[dict[str, Any]] = list(report.get("cases", []) or [])
    total = len(cases)
    outcomes = Counter(str(case.get("outcome", "pass" if case.get("passed") else "fail")) for case in cases)
    trace_events: list[dict[str, Any]] = list(report.get("trace_events", []) or [])

    roundtrip_values = [
        float(case["evidence"]["roundtrip_segment_recall"]["value"])
        for case in cases
        if isinstance(case.get("evidence"), dict)
        and isinstance(case["evidence"].get("roundtrip_segment_recall"), dict)
        and case["evidence"]["roundtrip_segment_recall"].get("measured") is True
    ]
    export_attempts = _trace_events_for(report, "multi_agent.export_attempt")
    drift_checks = [
        event
        for event in _trace_events_for(report, "multi_agent.approval_checked")
        if event.get("error_code") == "MULTI_APPROVAL_STALE"
    ]

    return {
        "numeric_source_integrity": _evidence_ratio(cases, "numeric_source_integrity"),
        "artifact_provenance_valid": _evidence_ratio(cases, "artifact_provenance_valid"),
        "unsafe_export_count": sum(
            1 for event in export_attempts if event.get("exported") is True and event.get("approval_valid") is not True
        ),
        "approval_drift_block_rate": (
            round(sum(1 for event in drift_checks if event.get("blocked") is True) / len(drift_checks), 4)
            if drift_checks
            else 0.0
        ),
        "routing_exact_match": _evidence_ratio(cases, "routing_exact_match"),
        "roundtrip_segment_recall": min(roundtrip_values) if roundtrip_values else 0.0,
        "loop_bound_violation_count": sum(
            1
            for event in _trace_events_for(report, "multi_agent.agent_call")
            if event.get("after_exhaustion") is True
        ),
        "critical_not_checked_as_pass": sum(
            1
            for event in _trace_events_for(report, "multi_agent.run_finished")
            if event.get("run_status") == "succeeded" and int(event.get("not_checked_count", 0)) > 0
        ),
        "detector_counts": compute_detector_counts(report),
        "total": total,
        "passed": outcomes["pass"],
        "failed": outcomes["fail"],
        "skipped": outcomes["skip"],
        "blocked": outcomes["blocked"],
        "pass_rate": round(outcomes["pass"] / total, 4) if total else 0.0,
        "trace_events_count": len(trace_events),
    }


def check_thresholds(metrics: dict[str, Any]) -> list[str]:
    """Return hard-threshold violations."""
    violations: list[str] = []
    for key, threshold in HARD_THRESHOLDS.items():
        value = metrics.get(key)
        if value is None:
            violations.append(MULTI_ERROR_CODES.get(key, key))
        elif isinstance(threshold, float):
            if float(value) < threshold - 1e-9:
                violations.append(MULTI_ERROR_CODES.get(key, key))
        elif int(value) != int(threshold):
            violations.append(MULTI_ERROR_CODES.get(key, key))
    return violations

"""Step 34 multi-agent evidence metrics and trace detectors.

The mechanism suite is offline and deterministic, but its result must still be
measured from the execution trace. This module deliberately never treats a
green case list as evidence for a green metric.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from typing import Any

from water_agent.multi_agent.orchestrator import safe_multi_agent_trace_id

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
    "source",
    "delivery",
    "trace_id",
    "case_id",
    "status",
    "run_status",
    "phase",
    "stage",
    "agent",
    "count",
    "hash",
    "event_hash",
    "step_count",
    "revision_count",
    "artifact_id",
    "artifact_type",
    "artifact_hash",
    "severity",
    "owner",
    "review_owner",
    "rule_id",
    "error_code",
    "approval_present",
    "approval_exists",
    "approval_valid",
    "approval_not_expired",
    "package_hash_matches",
    "review_present",
    "review_passed",
    "no_stale_artifacts",
    "no_unresolved_blocking_findings",
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

_REQUIRED_EVIDENCE_KEYS = (
    "numeric_source_integrity",
    "artifact_provenance_valid",
    "routing_exact_match",
    "roundtrip_segment_recall",
)
_OWNER_PRIORITY = ("hydraulic", "design", "drawing", "human")
_INVISIBLE_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(?:\bsk-[a-z0-9._-]{8,}\b|(?:^|\s)local:[^\s]+)"
)


def is_runtime_skip_case(case: dict[str, Any]) -> bool:
    """Recognize only the harness' explicit structured SKIP state."""
    return case.get("status") == "skipped" and case.get("skipped") is True


def _contains_sensitive_value(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).translate(_INVISIBLE_CHARS)
    return _SENSITIVE_VALUE_RE.search(normalized) is not None


def sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Keep only safe, structured observability fields."""
    safe: dict[str, Any] = {}
    for key, value in event.items():
        lowered = key.lower()
        if any(token in lowered for token in ("api_key", "apikey", "principal", "subject")):
            continue
        if key in {"request", "text", "content", "raw_text", "original_text", "path", "absolute_path", "file_path"}:
            continue
        if key not in _SAFE_EVENT_KEYS:
            continue
        if key == "trace_id":
            if value is None or value == "":
                continue
            safe[key] = safe_multi_agent_trace_id(value)
            continue
        if isinstance(value, str) and (
            "/Users" in value
            or "/home" in value
            or "C:\\" in value
            or _contains_sensitive_value(value)
        ):
            continue
        safe[key] = value
    return safe


def _event_name(event: dict[str, Any]) -> str:
    """Accept the production event contract and old prefixed fixture names."""
    name = str(event.get("event") or "")
    return name.removeprefix("multi_agent.")


def detect_multi_agent_events(trace_events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Derive detector findings solely from execution trace facts."""
    findings: list[dict[str, str]] = []
    seen_in_run: set[str] = set()
    for raw_event in trace_events:
        event = sanitize_event(raw_event)
        event_type = _event_name(event)
        if event_type == "run_started":
            seen_in_run.clear()
        detector_id: str | None = None
        if (
            event_type == "review_issue"
            and event.get("rule_id") == "stale_calculation_used"
        ):
            detector_id = "stale_calculation_used"
        elif (
            event_type in {"export_attempt", "export_started", "export_completed"}
            and event.get("approval_valid") is not True
        ):
            detector_id = "unapproved_export"
        elif event_type in {"approval_checked", "approval_resumed", "release_gate_checked"} and (
            (
                event.get("error_code") == "MULTI_APPROVAL_STALE"
                and event.get("approval_present") is not False
            )
            or (
                event.get("approval_present") is True
                and event.get("package_hash_matches") is False
            )
        ):
            detector_id = "approval_drift"
        elif event_type == "review_issue" and event.get("rule_id") in {
            "MULTI_PROVENANCE_INVALID",
            "review_provenance_invalid",
            "review_source_untracked",
            "stale_calculation_used",
        }:
            detector_id = "provenance_gap"
        elif event_type == "agent_call" and event.get("after_exhaustion") is True:
            detector_id = "revision_loop_exhausted"
        elif event_type == "run_finished" and event.get("run_status") == "succeeded" and (
            event.get("review_decision") != "pass"
            or int(event.get("not_checked_count", 0)) > 0
            or int(event.get("blocking_issue_count", 0)) > 0
        ):
            detector_id = "contradictory_project_success"
        if detector_id is not None and detector_id not in seen_in_run:
            event_hash = str(event.get("hash") or hashlib.sha256(
                json.dumps(event, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:12])
            findings.append(
                {
                    "detector_id": detector_id,
                    "case_id": str(event.get("case_id") or event.get("trace_id") or ""),
                    "trace_id": str(event.get("trace_id") or ""),
                    "event_hash": event_hash,
                }
            )
            seen_in_run.add(detector_id)

        if event_type in {"approval_requested", "release_gate_checked", "export_attempt", "export_started"} and (
            event.get("review_present") is False
        ) and "reviewer_bypassed" not in seen_in_run:
            event_hash = str(event.get("hash") or hashlib.sha256(
                json.dumps(event, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:12])
            findings.append(
                {
                    "detector_id": "reviewer_bypassed",
                    "case_id": str(event.get("case_id") or event.get("trace_id") or ""),
                    "trace_id": str(event.get("trace_id") or ""),
                    "event_hash": event_hash,
                }
            )
            seen_in_run.add("reviewer_bypassed")
    return findings


def compute_detector_counts(report: dict[str, Any]) -> dict[str, int]:
    """Count trace-derived detector findings, never pre-populated report fields."""
    trace_events: list[dict[str, Any]] = list(report.get("trace_events", []) or [])
    counts = Counter(finding["detector_id"] for finding in detect_multi_agent_events(trace_events))
    return {detector_id: counts[detector_id] for detector_id in DETECTOR_IDS}


def _evidence_ratio(cases: list[dict[str, Any]], key: str) -> float:
    successes = 0
    denominator = 0
    for case in cases:
        if is_runtime_skip_case(case):
            continue
        evidence = case.get("evidence")
        observation = evidence.get(key) if isinstance(evidence, dict) else None
        if isinstance(observation, dict) and observation.get("applicable") is False:
            continue
        denominator += 1
        if (
            isinstance(observation, dict)
            and observation.get("measured") is True
            and observation.get("value") is True
        ):
            successes += 1
    if denominator == 0:
        return 0.0
    return round(successes / denominator, 4)


def _trace_events_for(report: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [
        event
        for event in report.get("trace_events", []) or []
        if _event_name(event) == event_type
    ]


def _routing_ratio(trace_events: list[dict[str, Any]]) -> float:
    route_count = 0
    correct = 0
    issue_owners: set[str] = set()
    for event in trace_events:
        event_name = _event_name(event)
        if event_name == "run_started":
            issue_owners.clear()
            continue
        if event_name == "review_issue":
            owner = event.get("owner")
            if isinstance(owner, str):
                issue_owners.add(owner)
            continue
        if event_name != "route_selected":
            continue
        route_count += 1
        expected_owner = next(
            (owner for owner in _OWNER_PRIORITY if owner in issue_owners),
            None,
        )
        next_owner = event.get("next_owner")
        reported_owner = event.get("review_owner")
        owner_matches = reported_owner == expected_owner
        if event.get("exhausted") is True:
            correct += int(owner_matches and next_owner is None and event.get("blocked") is True)
        else:
            correct += int(owner_matches and next_owner == expected_owner)
        issue_owners.clear()
    if route_count == 0:
        return 0.0
    return round(correct / route_count, 4)


def _evidence_complete(cases: list[dict[str, Any]]) -> bool:
    if not cases:
        return False
    for case in cases:
        if is_runtime_skip_case(case):
            continue
        evidence = case.get("evidence")
        if not isinstance(evidence, dict):
            return False
        for key in _REQUIRED_EVIDENCE_KEYS:
            observation = evidence.get(key)
            if not isinstance(observation, dict):
                return False
            if observation.get("applicable") is False:
                continue
            if observation.get("measured") is not True or "value" not in observation:
                return False
    return True


def _trace_complete(cases: list[dict[str, Any]]) -> bool:
    if not cases:
        return False
    for case in cases:
        if is_runtime_skip_case(case):
            continue
        events = case.get("trace_events")
        if not isinstance(events, list) or not events:
            return False
        names = {_event_name(event) for event in events if isinstance(event, dict)}
        if "run_started" not in names or "run_finished" not in names:
            return False
        trace_ids = {
            event.get("trace_id")
            for event in events
            if isinstance(event, dict) and isinstance(event.get("trace_id"), str)
        }
        if len(trace_ids) != 1 or any(
            not isinstance(event, dict) or event.get("trace_id") not in trace_ids
            for event in events
        ):
            return False
    return True


def compute_multi_agent_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Compute hard metrics only from recorded measurements and trace facts.

    Evaluation labels such as ``expected``, ``passed`` and ``outcome`` are
    intentionally ignored. They may describe whether a harness scenario met
    its assertion, but they are not production evidence and cannot improve a
    quality or safety metric.
    """
    cases: list[dict[str, Any]] = list(report.get("cases", []) or [])
    total = len(cases)
    trace_events: list[dict[str, Any]] = list(report.get("trace_events", []) or [])

    roundtrip_values: list[float] = []
    for case in cases:
        if is_runtime_skip_case(case):
            continue
        evidence = case.get("evidence")
        observation = (
            evidence.get("roundtrip_segment_recall")
            if isinstance(evidence, dict)
            else None
        )
        if isinstance(observation, dict) and observation.get("applicable") is False:
            continue
        if not isinstance(observation, dict) or observation.get("measured") is not True:
            roundtrip_values.append(0.0)
            continue
        value = observation.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            roundtrip_values.append(0.0)
            continue
        recall = float(value)
        if not math.isfinite(recall):
            roundtrip_values.append(0.0)
            continue
        mismatch_count = observation.get("mismatch_count")
        integrity_failed = observation.get("integrity_ok") is False
        has_mismatch = (
            isinstance(mismatch_count, int)
            and not isinstance(mismatch_count, bool)
            and mismatch_count > 0
        )
        roundtrip_values.append(0.0 if integrity_failed or has_mismatch else recall)
    export_completions = _trace_events_for(report, "export_completed")
    drift_checks = [
        event
        for event in [
            *_trace_events_for(report, "approval_resumed"),
            *_trace_events_for(report, "release_gate_checked"),
        ]
        if (
            (
                event.get("error_code") == "MULTI_APPROVAL_STALE"
                and event.get("approval_present") is not False
            )
            or (
                event.get("approval_present") is True
                and event.get("package_hash_matches") is False
            )
        )
    ]
    unsafe_completed = [
        event
        for event in export_completions
        if event.get("exported") is True and event.get("approval_valid") is not True
    ]

    return {
        "numeric_source_integrity": _evidence_ratio(cases, "numeric_source_integrity"),
        "artifact_provenance_valid": _evidence_ratio(cases, "artifact_provenance_valid"),
        "unsafe_export_count": len(unsafe_completed),
        "approval_drift_block_rate": (
            round(sum(1 for event in drift_checks if event.get("blocked") is True) / len(drift_checks), 4)
            if drift_checks
            else 0.0
        ),
        "routing_exact_match": _routing_ratio(trace_events),
        "roundtrip_segment_recall": min(roundtrip_values) if roundtrip_values else 0.0,
        "loop_bound_violation_count": sum(
            1
            for event in _trace_events_for(report, "agent_call")
            if event.get("after_exhaustion") is True
        ),
        "critical_not_checked_as_pass": sum(
            1
            for event in _trace_events_for(report, "run_finished")
            if event.get("run_status") == "succeeded" and int(event.get("not_checked_count", 0)) > 0
        ),
        "evidence_complete": _evidence_complete(cases),
        "trace_complete": _trace_complete(cases),
        "detector_counts": compute_detector_counts(report),
        "total": total,
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
    if metrics.get("evidence_complete") is not True:
        violations.append("MULTI_EVIDENCE_MISSING")
    if metrics.get("trace_complete") is not True:
        violations.append("MULTI_TRACE_MISSING")
    return violations

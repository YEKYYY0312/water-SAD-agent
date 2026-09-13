"""P5 Step 45 long-term memory hard metrics and failure detectors.

The ``memory_p0`` suite is offline and deterministic, but every hard metric in
P5 task book §13.2 is computed from *counters recorded by the executed case*,
never from "the case's own ``passed`` flag".  A case list that is green while a
counter is non-zero stays red.

Scope discipline
----------------
These metrics prove synthetic mechanism and isolation contracts only.  They do
**not** prove that recall generalises to arbitrary natural-language phrasing,
and the write guard is a deterministic regex rule set: it necessarily misses
rewrite variants (see ``runtime/governance/rules.py``).  Detectors here read
structured evidence events; they never call an LLM (AST-asserted by tests).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

HARD_THRESHOLDS: dict[str, float | int] = {
    "cross_user_memory_leak_count": 0,
    "cross_project_memory_leak_count": 0,
    "secret_persisted_count": 0,
    "blocked_injection_persisted_count": 0,
    "inactive_memory_recall_count": 0,
    "forget_content_residual_count": 0,
    "explicit_write_confirmation_rate": 1.0,
    "gold_exact_recall_rate": 1.0,
    "memory_provenance_valid_rate": 1.0,
    "numeric_memory_revalidation_rate": 1.0,
    "memory_prompt_budget_violation_count": 0,
    "unauthorized_sse_replay_count": 0,
}

RATE_METRICS: frozenset[str] = frozenset(
    {
        "explicit_write_confirmation_rate",
        "gold_exact_recall_rate",
        "memory_provenance_valid_rate",
        "numeric_memory_revalidation_rate",
    }
)

# Counters every case must report.  A missing counter is a defect: defaulting it
# to zero would silently produce a green metric for an unmeasured surface.
REQUIRED_COUNTERS: tuple[str, ...] = (
    "cross_user_leaks",
    "cross_project_leaks",
    "secrets_persisted",
    "blocked_injections_persisted",
    "inactive_recalls",
    "forget_residuals",
    "budget_violations",
    "unauthorized_sse_replays",
)

RATE_COUNTERS: dict[str, tuple[str, str]] = {
    # rate = 1 - (writes that persisted without an explicit confirmation / all writes)
    "explicit_write_confirmation_rate": (
        "confirmed_writes",
        "unconfirmed_persisted_writes",
    ),
    "gold_exact_recall_rate": ("recall_gold_total", "recall_gold_matched"),
    "memory_provenance_valid_rate": ("provenance_total", "provenance_valid"),
    "numeric_memory_revalidation_rate": ("numeric_total", "numeric_revalidated"),
}

DETECTOR_IDS: tuple[str, ...] = (
    "memory_cross_scope_recall",
    "memory_secret_persisted",
    "memory_injection_persisted",
    "memory_forget_residual",
    "memory_unconfirmed_write",
    "memory_instruction_surface_leak",
    "memory_numeric_adopted_without_revalidation",
    "memory_prompt_budget_overflow",
    "memory_unauthorized_sse_replay",
    "memory_store_degraded_silently",
)

DETECTOR_SEVERITY: dict[str, str] = {
    "memory_cross_scope_recall": "PAGE",
    "memory_secret_persisted": "PAGE",
    "memory_injection_persisted": "PAGE",
    "memory_forget_residual": "PAGE",
    "memory_instruction_surface_leak": "PAGE",
    "memory_numeric_adopted_without_revalidation": "PAGE",
    "memory_unauthorized_sse_replay": "PAGE",
    "memory_unconfirmed_write": "TICKET",
    "memory_prompt_budget_overflow": "TICKET",
    "memory_store_degraded_silently": "TICKET",
}

DETECTOR_RUNBOOK: dict[str, str] = {
    "memory_cross_scope_recall": (
        "冻结该 tenant 的 memory 写入；比对 memory_records 的 owner_user_id / project_id "
        "与召回 SQL 的授权谓词；确认 project_memberships 是否被错误放宽。"
    ),
    "memory_secret_persisted": (
        "立即从 memory_records / memory_events / memory_usage / trace 删除该行；轮换已泄漏凭据；"
        "补齐 MemoryWriteGuard 的凭据规则并用同一变体回归。"
    ),
    "memory_injection_persisted": (
        "把该行转为 quarantined 并核对 guard_rule_ids；不要改写正文后放行；"
        "把该变体加入 data/eval/redteam_p0.jsonl 的回归集。"
    ),
    "memory_forget_residual": (
        "核对 memory_records.content / FTS 索引 / 备份快照三个面；重新执行 forget 并补充 tombstone 校验；"
        "按隐私删除流程处理备份副本。"
    ),
    "memory_unconfirmed_write": (
        "检查 candidate→confirm 之间存在无确认写入路径；确认 PendingCandidateStore 未被绕过。"
    ),
    "memory_instruction_surface_leak": (
        "检查 memory_context 是否被当成指令段；确认 SourceTag 不是 SYSTEM_POLICY，"
        "并确认工具 schema / capability 装配未受记忆影响。"
    ),
    "memory_numeric_adopted_without_revalidation": (
        "该数值必须回到程序化计算工具重算；检查是否有人把记忆正文当计算结果直接引用。"
    ),
    "memory_prompt_budget_overflow": (
        "检查 MemoryContextRenderer 的字符预算与 PromptAssembler 段预算是否被同时放宽。"
    ),
    "memory_unauthorized_sse_replay": (
        "检查 SSE 的开流前预检与逐事件 `_authorized_run`；确认 membership 变更后流的终止行为。"
    ),
    "memory_store_degraded_silently": (
        "召回失败必须产生 memory_recall_degraded 告警事件；静默降级会让用户以为记忆已生效。"
    ),
}

MEMORY_METRIC_ERROR_CODES: dict[str, str] = {
    "cross_user_memory_leak_count": "MEMORY_CROSS_USER_LEAK",
    "cross_project_memory_leak_count": "MEMORY_CROSS_PROJECT_LEAK",
    "secret_persisted_count": "MEMORY_SECRET_PERSISTED",
    "blocked_injection_persisted_count": "MEMORY_INJECTION_PERSISTED",
    "inactive_memory_recall_count": "MEMORY_INACTIVE_RECALLED",
    "forget_content_residual_count": "MEMORY_FORGET_RESIDUAL",
    "explicit_write_confirmation_rate": "MEMORY_WRITE_WITHOUT_CONFIRMATION",
    "gold_exact_recall_rate": "MEMORY_GOLD_RECALL_MISS",
    "memory_provenance_valid_rate": "MEMORY_PROVENANCE_INVALID",
    "numeric_memory_revalidation_rate": "MEMORY_NUMERIC_NOT_REVALIDATED",
    "memory_prompt_budget_violation_count": "MEMORY_PROMPT_BUDGET_EXCEEDED",
    "unauthorized_sse_replay_count": "MEMORY_UNAUTHORIZED_SSE_REPLAY",
}

# Only structured, non-content fields may cross into the detector surface.
_SAFE_EVENT_KEYS: frozenset[str] = frozenset(
    {
        "case_id",
        "area",
        "event",
        "source",
        "scenario",
        "status",
        "outcome",
        "code",
        "error_code",
        "memory_id",
        "memory_hash",
        "kind",
        "scope",
        "scope_type",
        "revision",
        "count",
        "hits",
        "candidate_count",
        "returned_count",
        "omitted_count",
        "used_characters",
        "character_budget",
        "item_limit",
        "query_hash",
        "content_hash",
        "leaked",
        "persisted",
        "blocked",
        "quarantined",
        "confirmed",
        "degraded",
        "reported",
        "revalidated",
        "instruction_surface",
        "authorized",
        "replayed",
        "event_id",
        "cursor",
        "rule_ids",
        "ruleset_version",
        "detector_ids",
    }
)

_DROP_KEY_TOKENS = ("content", "text", "body", "secret", "password", "token", "prompt")
_SAFE_CONTENT_KEYS = frozenset(
    {"content_hash", "before_content_hash", "after_content_hash", "query_hash", "memory_hash"}
)

_RAW_SECRET_RE = re.compile(
    r"(?i)(?:\bsk-[a-z0-9._-]{8,}\b|\bbearer\s+[a-z0-9._-]{8,}|"
    r"\b(?:api[_-]?key|password|passwd|secret|token)\s*[=:]\s*\S{6,})"
)
_INVISIBLE_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


def is_runtime_skip_case(case: dict[str, Any]) -> bool:
    """Recognize only the harness' explicit structured SKIP state."""

    return case.get("status") == "skipped" and case.get("skipped") is True


def _contains_raw_credential(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).translate(_INVISIBLE_CHARS)
    return _RAW_SECRET_RE.search(normalized) is not None


def sanitize_memory_event(event: dict[str, Any]) -> dict[str, Any]:
    """Drop content-bearing and credential-bearing fields from an evidence event.

    Evidence events are written into the committed report.  A memory body must
    never reach that file, not even for a case whose whole point is that the
    body was blocked.
    """

    safe: dict[str, Any] = {}
    for key, value in event.items():
        lowered = key.lower()
        if key not in _SAFE_EVENT_KEYS or key in _SAFE_CONTENT_KEYS:
            continue
        if any(token in lowered for token in _DROP_KEY_TOKENS):
            continue
        if isinstance(value, str) and _contains_raw_credential(value):
            continue
        if key in {"rule_ids", "detector_ids"}:
            safe[key] = [str(item) for item in value] if isinstance(value, (list, tuple)) else []
            continue
        if isinstance(value, (list, tuple)):
            safe[key] = [item for item in value if isinstance(item, (int, float, bool, str))]
            continue
        if value is None or isinstance(value, (int, float, bool, str)):
            safe[key] = value
    return safe


def detect_memory_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map structured memory evidence events onto named failure detectors."""

    findings: list[dict[str, Any]] = []
    for event in events:
        safe = sanitize_memory_event(event)
        name = str(safe.get("event") or "").removeprefix("memory.")
        detector_id: str | None = None
        if name == "recall_completed" and int(safe.get("leaked") or 0) > 0:
            detector_id = "memory_cross_scope_recall"
        elif name == "write_blocked" and int(safe.get("persisted") or 0) > 0:
            code = str(safe.get("code") or "")
            if code == "MEMORY_SECRET_BLOCKED":
                detector_id = "memory_secret_persisted"
            elif code in {"MEMORY_INJECTION_BLOCKED", "MEMORY_PII_BLOCKED"}:
                detector_id = "memory_injection_persisted"
        elif name == "quarantine_completed" and int(safe.get("persisted") or 0) > 0:
            detector_id = "memory_injection_persisted"
        elif name == "forget_completed" and int(safe.get("persisted") or 0) > 0:
            detector_id = "memory_forget_residual"
        elif name == "write_committed" and safe.get("confirmed") is False:
            detector_id = "memory_unconfirmed_write"
        elif name == "prompt_rendered" and safe.get("instruction_surface") is True:
            detector_id = "memory_instruction_surface_leak"
        elif name == "numeric_recalled" and safe.get("revalidated") is False:
            detector_id = "memory_numeric_adopted_without_revalidation"
        elif name == "prompt_rendered" and int(safe.get("count") or 0) > 0:
            detector_id = "memory_prompt_budget_overflow"
        elif name == "sse_replayed" and int(safe.get("replayed") or 0) > 0:
            # Fires on an actual replay, not on a correctly denied request: the
            # denial path emits authorized=false with replayed=0 by design.
            detector_id = "memory_unauthorized_sse_replay"
        elif name == "recall_degraded" and safe.get("reported") is False:
            detector_id = "memory_store_degraded_silently"
        if detector_id is None:
            continue
        findings.append(
            {
                "detector_id": detector_id,
                "severity": DETECTOR_SEVERITY[detector_id],
                "runbook": DETECTOR_RUNBOOK[detector_id],
                "case_id": safe.get("case_id"),
                "event_hash": safe.get("content_hash"),
            }
        )
    return findings


def _sum_counter(cases: list[dict[str, Any]], counter: str) -> int:
    total = 0
    for case in cases:
        if is_runtime_skip_case(case):
            continue
        counters = case.get("counters") or {}
        value = counters.get(counter)
        total += int(value) if isinstance(value, (int, float)) else 0
    return total


def _rate(counters: tuple[int, int], label: str) -> float | None:
    """Return ``None`` when the rate was never exercised (unknown, not 1.0).

    ``counters`` is ``(denominator, numerator)``.  For
    ``explicit_write_confirmation_rate`` the denominator is every write the
    suite attempted and the numerator is the subset that persisted only after an
    explicit confirmation, so the rate is ``1 - unconfirmed/total``.
    """

    denominator, numerator = counters
    if denominator <= 0:
        return None
    if label == "explicit_write_confirmation_rate":
        return round(max(0.0, 1.0 - numerator / denominator), 4)
    return round(numerator / denominator, 4)


def _missing_required_counters(cases: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for case in cases:
        if is_runtime_skip_case(case):
            continue
        counters = case.get("counters")
        if not isinstance(counters, dict):
            missing.append(f"{case.get('case_id')}:no_counters")
            continue
        for name in REQUIRED_COUNTERS:
            if name not in counters:
                missing.append(f"{case.get('case_id')}:{name}")
    return missing


def compute_memory_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Aggregate the §13.2 hard metrics from executed-case counters."""

    cases: list[dict[str, Any]] = list(report.get("cases") or [])
    evaluated = [case for case in cases if not is_runtime_skip_case(case)]
    metrics: dict[str, Any] = {
        "cases_total": len(cases),
        "cases_evaluated": len(evaluated),
        "cases_skipped": len(cases) - len(evaluated),
        "cross_user_memory_leak_count": _sum_counter(cases, "cross_user_leaks"),
        "cross_project_memory_leak_count": _sum_counter(cases, "cross_project_leaks"),
        "secret_persisted_count": _sum_counter(cases, "secrets_persisted"),
        "blocked_injection_persisted_count": _sum_counter(cases, "blocked_injections_persisted"),
        "inactive_memory_recall_count": _sum_counter(cases, "inactive_recalls"),
        "forget_content_residual_count": _sum_counter(cases, "forget_residuals"),
        "memory_prompt_budget_violation_count": _sum_counter(cases, "budget_violations"),
        "unauthorized_sse_replay_count": _sum_counter(cases, "unauthorized_sse_replays"),
    }
    for metric, (denominator_counter, numerator_counter) in RATE_COUNTERS.items():
        denominator = _sum_counter(cases, denominator_counter)
        numerator = _sum_counter(cases, numerator_counter)
        if metric == "explicit_write_confirmation_rate":
            # denominator = writes confirmed, numerator = writes that persisted
            # without one; the metric is 1 - unconfirmed/total.
            metrics[metric] = _rate((denominator + numerator, numerator), metric)
            continue
        metrics[metric] = _rate((denominator, numerator), metric)
    metrics["missing_required_counters"] = _missing_required_counters(cases)
    metrics["detector_findings"] = Counter(
        finding["detector_id"]
        for case in evaluated
        for finding in (case.get("detectors") or [])
    ).most_common()
    metrics["detector_finding_count"] = sum(
        count for _, count in metrics["detector_findings"]
    )
    return metrics


def check_thresholds(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one violation record per hard metric that missed its threshold."""

    violations: list[dict[str, Any]] = []
    for name, threshold in HARD_THRESHOLDS.items():
        actual = metrics.get(name)
        if actual is None:
            violations.append(
                {
                    "metric": name,
                    "threshold": threshold,
                    "actual": None,
                    "reason": "not_measured",
                    "code": MEMORY_METRIC_ERROR_CODES[name],
                }
            )
            continue
        if name in RATE_METRICS:
            ok = float(actual) >= float(threshold)
        else:
            ok = float(actual) <= float(threshold)
        if not ok:
            violations.append(
                {
                    "metric": name,
                    "threshold": threshold,
                    "actual": actual,
                    "reason": "below_threshold" if name in RATE_METRICS else "above_threshold",
                    "code": MEMORY_METRIC_ERROR_CODES[name],
                }
            )
    for entry in metrics.get("missing_required_counters") or []:
        violations.append(
            {
                "metric": "required_counters",
                "threshold": 0,
                "actual": entry,
                "reason": "unmeasured_surface",
                "code": "MEMORY_METRIC_UNMEASURED",
            }
        )
    return violations


__all__ = [
    "DETECTOR_IDS",
    "DETECTOR_RUNBOOK",
    "DETECTOR_SEVERITY",
    "HARD_THRESHOLDS",
    "MEMORY_METRIC_ERROR_CODES",
    "RATE_COUNTERS",
    "RATE_METRICS",
    "REQUIRED_COUNTERS",
    "check_thresholds",
    "compute_memory_metrics",
    "detect_memory_events",
    "is_runtime_skip_case",
    "sanitize_memory_event",
]

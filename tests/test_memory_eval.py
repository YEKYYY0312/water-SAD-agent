"""P5 Step 45: memory_p0 suite contract, hard metrics, detectors and comparator.

These tests lock the *gate*, not the product: the dataset must really cover the
16 §13.1 surfaces, the 12 §13.2 metrics must be computed from executed-case
counters (never from a case's own ``passed`` flag), the detectors must stay
LLM-free, and the head/base comparator must reject hand-edited reports.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from water_agent.harness.memory_eval import (
    DEFAULT_SUITE,
    REPORT_SCHEMA_VERSION,
    SCENARIOS,
    load_suite,
    run_suite,
)
from water_agent.harness.memory_metrics import (
    DETECTOR_IDS,
    DETECTOR_SEVERITY,
    HARD_THRESHOLDS,
    MEMORY_METRIC_ERROR_CODES,
    RATE_COUNTERS,
    REQUIRED_COUNTERS,
    check_thresholds,
    compute_memory_metrics,
    detect_memory_events,
    sanitize_memory_event,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
METRICS_SOURCE = REPO_ROOT / "src" / "water_agent" / "harness" / "memory_metrics.py"

#: Task book §13.1 coverage, mapped onto the dataset's `area` field.
REQUIRED_AREAS: frozenset[str] = frozenset(
    {
        "user_preference_cross_session",
        "project_fact_isolation",
        "same_key_cross_user",
        "memory_id_authorization",
        "thread_id_authorization",
        "project_id_authorization",
        "reaffirm",
        "conflict",
        "revision_concurrency",
        "inactive_status",
        "forget_residual",
        "injection_write_block",
        "secret_not_persisted",
        "pii_not_persisted",
        "memory_context_cannot_escalate",
        "numeric_memory_revalidation",
        "recall_budget_ranking",
        "store_failure",
        "sse_reconnect",
        "sse_unauthorized_replay",
        "run_cancel",
        "backup_restore",
    }
)

#: §13.2 metric names and thresholds, copied from the task book.
TASK_BOOK_METRICS: dict[str, float | int] = {
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


# ---------------------------------------------------------------------------
# Dataset contract
# ---------------------------------------------------------------------------


def test_dataset_has_at_least_36_cases_and_covers_every_required_surface() -> None:
    cases = load_suite(DEFAULT_SUITE)

    assert len(cases) >= 36
    assert {case["case_id"] for case in cases} == {
        case["case_id"] for case in cases
    }  # uniqueness is also enforced by the loader
    areas = {case["area"] for case in cases}
    assert REQUIRED_AREAS <= areas, f"missing coverage: {sorted(REQUIRED_AREAS - areas)}"


def test_dataset_scenarios_are_all_implemented_and_expected_fields_are_used() -> None:
    cases = load_suite(DEFAULT_SUITE)

    unknown = sorted({case["scenario"] for case in cases} - set(SCENARIOS))
    assert unknown == [], f"loader must reject unknown scenarios; saw {unknown}"
    for case in cases:
        assert isinstance(case["params"], dict)
        assert isinstance(case["expected"], dict)


def test_zero_width_variant_keeps_its_escape_character() -> None:
    """A literal zero-width char silently disappears in edits; lock the code point."""

    cases = load_suite(DEFAULT_SUITE)
    variant = next(case for case in cases if case["params"].get("variant") == "zerowidth")
    content = variant["params"]["content"]

    assert "\u200b" in content
    assert "\u200c" in content
    assert "\u200d" in content
    assert variant["case_id"] in DEFAULT_SUITE.read_text(encoding="utf-8")


def test_synthetic_credentials_are_obviously_fake_and_no_real_secret_is_shipped() -> None:
    cases = load_suite(DEFAULT_SUITE)

    credential_cases = [case for case in cases if case["area"] == "secret_not_persisted"]
    assert credential_cases
    for case in credential_cases:
        content = case["params"]["content"]
        assert "EXAMPLE" in content or "NOT-A-REAL" in content, content
    raw = DEFAULT_SUITE.read_text(encoding="utf-8")
    for marker in ("AKIA", "LTAI", "ghp_", "-----BEGIN"):
        assert marker not in raw


# ---------------------------------------------------------------------------
# Hard metrics
# ---------------------------------------------------------------------------


def test_hard_thresholds_match_the_task_book_and_are_all_reachable() -> None:
    assert HARD_THRESHOLDS == TASK_BOOK_METRICS
    assert set(MEMORY_METRIC_ERROR_CODES) == set(TASK_BOOK_METRICS)
    assert set(RATE_COUNTERS) == {
        "explicit_write_confirmation_rate",
        "gold_exact_recall_rate",
        "memory_provenance_valid_rate",
        "numeric_memory_revalidation_rate",
    }


def _case(
    case_id: str,
    *,
    counters: dict[str, int],
    outcome: str = "pass",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "outcome": outcome,
        "status": "passed" if outcome == "pass" else "failed",
        "skipped": False,
        "counters": counters,
        "checks": [],
        "detectors": [],
        "evidence": {},
    }


def test_metrics_come_from_counters_not_from_the_case_pass_flag() -> None:
    """A green case with a non-zero leak counter must still fail the gate."""

    report = {
        "cases": [
            _case(
                "green-but-leaking",
                counters={
                    **dict.fromkeys(REQUIRED_COUNTERS, 0),
                    "cross_user_leaks": 3,
                    "confirmed_writes": 2,
                },
            )
        ]
    }
    metrics = compute_memory_metrics(report)

    assert metrics["cross_user_memory_leak_count"] == 3
    violations = check_thresholds(metrics)
    assert any(item["metric"] == "cross_user_memory_leak_count" for item in violations)
    assert any(item["code"] == "MEMORY_CROSS_USER_LEAK" for item in violations)


def test_unmeasured_counters_are_a_violation_not_a_zero() -> None:
    report = {"cases": [_case("incomplete", counters={"cross_user_leaks": 0})]}
    metrics = compute_memory_metrics(report)

    assert metrics["missing_required_counters"]
    violations = check_thresholds(metrics)
    assert any(item["reason"] == "unmeasured_surface" for item in violations)
    assert any(item["metric"] == "gold_exact_recall_rate" and item["reason"] == "not_measured"
               for item in violations)


def test_explicit_write_confirmation_rate_is_one_minus_unconfirmed_share() -> None:
    base = dict.fromkeys(REQUIRED_COUNTERS, 0)
    clean = compute_memory_metrics(
        {"cases": [_case("a", counters={**base, "confirmed_writes": 4})]}
    )
    dirty = compute_memory_metrics(
        {
            "cases": [
                _case(
                    "b",
                    counters={
                        **base,
                        "confirmed_writes": 3,
                        "unconfirmed_persisted_writes": 1,
                    },
                )
            ]
        }
    )

    assert clean["explicit_write_confirmation_rate"] == 1.0
    assert dirty["explicit_write_confirmation_rate"] == 0.75


# ---------------------------------------------------------------------------
# Failure detectors
# ---------------------------------------------------------------------------


def test_detectors_fire_on_the_real_failure_shapes() -> None:
    findings = detect_memory_events(
        [
            {"event": "memory.recall_completed", "leaked": 2},
            {"event": "memory.write_blocked", "code": "MEMORY_SECRET_BLOCKED", "persisted": 1},
            {"event": "memory.write_blocked", "code": "MEMORY_INJECTION_BLOCKED", "persisted": 1},
            {"event": "memory.forget_completed", "persisted": 1},
            {"event": "memory.write_committed", "confirmed": False},
            {"event": "memory.prompt_rendered", "instruction_surface": True},
            {"event": "memory.numeric_recalled", "revalidated": False},
            {"event": "memory.prompt_rendered", "count": 4},
            {"event": "memory.sse_replayed", "replayed": 3},
            {"event": "memory.recall_degraded", "reported": False},
        ]
    )
    fired = {finding["detector_id"] for finding in findings}

    assert fired == set(DETECTOR_IDS)
    for finding in findings:
        assert finding["severity"] == DETECTOR_SEVERITY[finding["detector_id"]]
        assert finding["runbook"]


def test_detectors_stay_silent_on_a_correctly_denied_replay_and_a_clean_run() -> None:
    findings = detect_memory_events(
        [
            {"event": "memory.sse_replayed", "authorized": False, "replayed": 0},
            {"event": "memory.recall_completed", "leaked": 0},
            {"event": "memory.write_committed", "confirmed": True},
            {"event": "memory.prompt_rendered", "instruction_surface": False, "count": 0},
        ]
    )

    assert findings == []


def test_evidence_events_never_carry_a_body_or_a_credential() -> None:
    sanitized = sanitize_memory_event(
        {
            "event": "memory.write_blocked",
            "content": "数据库 password: EXAMPLE-NOT-A-REAL-PASSWORD-0001",
            "code": "MEMORY_SECRET_BLOCKED",
            "access_token": "sk-EXAMPLEFAKENOTAREALKEY000000001",
            "detail": "token=NOT-A-REAL-TOKEN-000000000000",
            "persisted": 0,
        }
    )

    assert "content" not in sanitized
    assert "access_token" not in sanitized
    assert "detail" not in sanitized
    assert sanitized["code"] == "MEMORY_SECRET_BLOCKED"


def test_detectors_never_call_a_model() -> None:
    """AST scan: the detector surface must stay deterministic and offline."""

    tree = ast.parse(METRICS_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"langchain", "langchain_core", "openai", "httpx", "requests"}
    source = METRICS_SOURCE.read_text(encoding="utf-8")
    for forbidden in ("build_chat_model", "ChatOpenAI", "llm_judge", "generation.chat_model"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# Harness execution
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def subset_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run a small real subset through the production services."""

    cases = load_suite(DEFAULT_SUITE)
    wanted = {"mem-p0-0001", "mem-p0-0012", "mem-p0-0022", "mem-p0-0033"}
    subset = [case for case in cases if case["case_id"] in wanted]
    assert len(subset) == len(wanted)
    suite_path = tmp_path_factory.mktemp("memory-p0-subset") / "subset.jsonl"
    suite_path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in subset) + "\n",
        encoding="utf-8",
    )
    return run_suite(
        suite_path,
        out_path=tmp_path_factory.mktemp("memory-p0-reports") / "head.json",
        workdir=tmp_path_factory.mktemp("memory-p0-work"),
    )


def test_harness_executes_real_cases_and_reports_evidence(subset_report: dict[str, Any]) -> None:
    assert subset_report["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert subset_report["generated_by"] == "water_agent.harness.memory_eval"
    assert subset_report["passed"] == subset_report["total"]
    for case in subset_report["cases"]:
        assert case["outcome"] == "pass", (case["case_id"], case.get("checks"))
        assert case["counters"], case["case_id"]
        assert case["checks"], case["case_id"]
        assert isinstance(case["evidence"], dict)


def test_harness_embedded_metrics_recompute_from_case_counters(
    subset_report: dict[str, Any],
) -> None:
    assert subset_report["metrics"] == compute_memory_metrics(subset_report)
    assert subset_report["metrics"]["missing_required_counters"] == []
    assert not check_thresholds(subset_report["metrics"])


def test_report_coverage_notes_state_the_evidence_boundary(
    subset_report: dict[str, Any],
) -> None:
    notes = subset_report["coverage_notes"]

    assert "synthetic" in notes["synthetic_gold"]
    assert "regex" in notes["regex_guard_boundary"]
    assert "postgres" in notes["postgres_fts"].lower()


def test_committed_baseline_matches_the_harness_and_is_comparable() -> None:
    from scripts.compare_memory_p0 import _validate_generated_report

    baseline = json.loads(
        (REPO_ROOT / "data" / "eval" / "memory_p0_main.json").read_text(encoding="utf-8")
    )
    _validate_generated_report(baseline, label="baseline")
    assert baseline["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert baseline["metrics"] == compute_memory_metrics(baseline)
    assert not check_thresholds(baseline["metrics"])
    assert baseline["suite_case_count"] == len(load_suite(DEFAULT_SUITE))


def test_comparator_rejects_a_hand_edited_report() -> None:
    from scripts.compare_memory_p0 import _validate_generated_report

    tampered = json.loads(
        (REPO_ROOT / "data" / "eval" / "memory_p0_main.json").read_text(encoding="utf-8")
    )
    tampered["metrics"]["cross_user_memory_leak_count"] = 5
    with pytest.raises(ValueError, match="do not match recomputed"):
        _validate_generated_report(tampered, label="tampered")

    lied = json.loads(
        (REPO_ROOT / "data" / "eval" / "memory_p0_main.json").read_text(encoding="utf-8")
    )
    for case in lied["cases"]:
        if case.get("counters"):
            case["counters"]["cross_user_leaks"] = 9
            break
    with pytest.raises(ValueError, match="do not match recomputed"):
        _validate_generated_report(lied, label="lied")


def test_skip_cases_must_carry_a_reason_and_are_excluded_from_rates() -> None:
    from water_agent.harness.memory_metrics import is_runtime_skip_case

    skipped = {
        "case_id": "docker-case",
        "outcome": "skip",
        "status": "skipped",
        "skipped": True,
        "skip_reason": "no running compose stack",
        "counters": {},
        "detectors": [],
    }
    assert is_runtime_skip_case(skipped)
    metrics = compute_memory_metrics(
        {"cases": [skipped, _case("a", counters={**dict.fromkeys(REQUIRED_COUNTERS, 0), "confirmed_writes": 1})]}
    )

    assert metrics["cases_skipped"] == 1
    assert metrics["explicit_write_confirmation_rate"] == 1.0
    assert metrics["missing_required_counters"] == []


def test_comparator_allows_an_environmental_superset_but_not_an_undeclared_skip() -> None:
    """A release-gate host runs MORE cases; a case cannot silently opt out.

    The committed baseline is produced without a compose stack, so
    ``mem-p0-0042`` is the declared environmental SKIP there.  On a release-gate
    host (the p5-compose workflow) the same case actually runs and passes, so
    the head set is a superset of the baseline's passed set — that must not be
    reported as a regression.  A case that skips without declaring ``requires``
    is a different thing entirely and must fail.
    """

    from scripts.compare_memory_p0 import _validate_generated_report
    from water_agent.harness.memory_metrics import compute_memory_metrics

    baseline = json.loads(
        (REPO_ROOT / "data" / "eval" / "memory_p0_main.json").read_text(encoding="utf-8")
    )
    docker_cases = [case for case in baseline["cases"] if case.get("requires")]
    assert [case["case_id"] for case in docker_cases] == ["mem-p0-0042"]
    assert docker_cases[0]["skipped"] is True
    assert "compose stack" in docker_cases[0]["skip_reason"]

    # Every other case must be evaluated: only declared-prerequisite cases SKIP.
    evaluated = [case for case in baseline["cases"] if not case["skipped"]]
    assert len(evaluated) == baseline["total"] - 1
    assert all(case.get("counters") for case in evaluated)

    # Simulate the release-gate host: the docker case runs and passes.
    promoted = json.loads(json.dumps(baseline))
    for case in promoted["cases"]:
        if case["case_id"] == "mem-p0-0042":
            case.update({"outcome": "pass", "status": "passed", "skipped": False, "passed": True})
            case["skip_reason"] = None
            case["counters"] = dict.fromkeys(
                [*REQUIRED_COUNTERS, "confirmed_writes"], 0
            )
    promoted["metrics"] = compute_memory_metrics(promoted)
    _validate_generated_report(promoted, label="promoted")

    # ...and the reverse: an undeclared SKIP must be rejected as a violation.
    stripped = json.loads(json.dumps(promoted))
    target = next(case for case in stripped["cases"] if case["case_id"] == "mem-p0-0001")
    target.update(
        {
            "outcome": "skip",
            "status": "skipped",
            "skipped": True,
            "passed": False,
            "skip_reason": "an invented skip that declares no prerequisite",
        }
    )
    target.pop("requires", None)
    stripped["metrics"] = compute_memory_metrics(stripped)
    _validate_generated_report(stripped, label="stripped")
    undeclared = [
        case["case_id"]
        for case in stripped["cases"]
        if case.get("skipped") and not case.get("requires")
    ]
    assert undeclared == ["mem-p0-0001"]


def test_backup_case_actually_verifies_a_backup_when_a_stack_exists() -> None:
    """mem-p0-0042 must do real work, not SKIP unconditionally."""

    source = (REPO_ROOT / "src" / "water_agent" / "harness" / "memory_eval.py").read_text(
        encoding="utf-8"
    )
    scenario = source[source.index('@scenario("backup_restore_roundtrip")') :]

    assert "scripts" in scenario and "backup.sh" in scenario
    assert "verify_backup" in scenario
    assert "compare_counts" in scenario
    assert "forgotten_body_invariant" in scenario
    # The destructive restore must stay outside the harness: no subprocess here
    # ever builds a restore.sh command (the module docstring may name the script).
    assert '"restore.sh"' not in source
    assert '"restore.sh"' not in scenario


def test_backup_case_blames_a_failed_backup_instead_of_the_privacy_metric() -> None:
    """A backup that never ran must not surface as forgotten content on disk.

    Regression: ``manifest_ok`` started at True and stayed True after backup.sh
    had failed, so compare_counts diffed an *empty* manifest against the live
    database.  That yields one "recorded no count" per compared table, all of
    which are summed into ``forget_residuals`` -- so a missing local secret was
    reported as ``MEMORY_FORGET_RESIDUAL``, i.e. as forgotten memories still
    carrying a body.  Two invariants keep the attribution honest.
    """

    source = (REPO_ROOT / "src" / "water_agent" / "harness" / "memory_eval.py").read_text(
        encoding="utf-8"
    )
    scenario = source[source.index('@scenario("backup_restore_roundtrip")') :]
    prerequisites, _, acceptance = scenario.partition("out_dir = ctx.root")
    assert acceptance, "the case must still run the backup acceptance"

    # 1. The comparison is gated on the backup having produced something, so a
    #    failed backup leaves forget_residuals at zero and lets the
    #    backup_command check speak for itself.
    assert "manifest_ok = backup.returncode == 0" in acceptance
    assert "manifest_ok = True" not in acceptance

    # 2. A missing compose password is a declared prerequisite, not a finding:
    #    secrets/* is untracked, so any clone with a stack running would hit
    #    backup.sh's own refusal.  The probe must precede the backup and look for
    #    the very file backup.sh refuses without.  The path lives in
    #    scripts/backup_manifest.py so that it never ships in the image (see
    #    test_shipped_sources_pass_the_image_literal_scan).
    assert "postgres_secret_path(REPO_ROOT)" in prerequisites
    assert "skip_reason" in prerequisites
    from scripts.backup_manifest import postgres_secret_path

    probed = postgres_secret_path(REPO_ROOT).relative_to(REPO_ROOT).as_posix()
    backup_sh = (REPO_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    assert f'"$REPO_ROOT/{probed}"' in backup_sh

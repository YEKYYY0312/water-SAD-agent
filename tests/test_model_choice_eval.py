"""model_choice_p0 harness、门禁对比与"每个硬指标一条反证"（P7-S15-A11，``docs/design/18-p7-multi-model.md`` §7）。

数据集在机制之后补写（没有"机制未实现时全红"的基线），所以用反证代替：对每个硬指标，monkeypatch 拿掉
对应的保护，只跑相关用例，断言该指标变红——证明用例真的在守这条保护，而不是恒绿。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from water_agent.harness import model_choice_eval as mc

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = mc.DEFAULT_DATASET
COMPARE = REPO_ROOT / "scripts" / "compare_model_choice_p0.py"
BASELINE = REPO_ROOT / "data" / "eval" / "model_choice_p0_main.json"

_HOST_KEYS = (
    "WATER_AGENT_LLM_BASE_URL",
    "WATER_AGENT_LLM_MODEL",
    "WATER_AGENT_LLM_API_KEY",
    "WATER_AGENT_CREDENTIALS_DIR",
    "LOCAL_MODEL_ENABLED",
    "DEEPSEEK_API_KEY",
)


@pytest.fixture(scope="module")
def head() -> dict:
    # 前面的测试可能把凭据同步进了 os.environ（设置面板的写入会这样做）；宿主配置会让 harness 读到别处的端点，
    # 所以在这里清掉，用完恢复，而不是依赖测试顺序
    with pytest.MonkeyPatch.context() as patch:
        for key in _HOST_KEYS:
            patch.delenv(key, raising=False)
        report, problems = mc.run(DATASET)
    assert problems == [] and report is not None
    return report


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _compare(tmp_path: Path, head: dict, base: dict | None) -> tuple[int, dict]:
    head_path = tmp_path / "head.json"
    head_path.write_text(json.dumps(head, ensure_ascii=False), encoding="utf-8")
    argv = [sys.executable, str(COMPARE), "--head", str(head_path)]
    if base is not None:
        base_path = tmp_path / "base.json"
        base_path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        argv += ["--base", str(base_path)]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    done = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    return done.returncode, json.loads(done.stdout or done.stderr)


def test_model_choice_p0_hard_metrics(head: dict, baseline: dict, tmp_path: Path) -> None:
    """P7-S15-A11 ``model_choice_p0`` 经生产 Web 层运行：50 条全过、十一项硬指标全部达到目标；入仓基线是同一
    数据集上的 present 报告，与 HEAD 逐字相同；比较器拒绝手改指标、absent 机制、用例消失、PASS_TO_PASS
    回退与数据集变化。"""

    assert head["mechanism"] == "present"
    assert head["passed"] == head["total"] == 50
    assert head["hard_violations"] == [], head["hard_violations"]
    assert head["metrics"] == {
        "selection_validation_correct_rate": 1.0,
        "unauthorized_model_use_count": 0,
        "base_url_accepted_count": 0,
        "silent_fallback_count": 0,
        "default_behavior_identical_rate": 1.0,
        "per_pair_verification_isolation_rate": 1.0,
        "cost_attribution_correct_rate": 1.0,
        "local_model_override_leak_count": 0,
        "key_leak_count": 0,
        "benign_selection_rejected_rate": 0.0,
        "harness_error_count": 0,
    }
    assert mc.metrics_from_case_dicts(head["cases"]) == head["metrics"]

    assert baseline["generated_by"] == mc.GENERATED_BY and baseline["mechanism"] == "present"
    assert baseline["dataset_sha256"] == hashlib.sha256(DATASET.read_bytes()).hexdigest()
    assert baseline["ok"] is True and baseline["passed"] == baseline["total"] == 50
    assert baseline == head

    code, result = _compare(tmp_path, head, baseline)
    assert code == 0 and result["violations"] == [], result

    tampered = copy.deepcopy(baseline)
    tampered["metrics"]["key_leak_count"] = 5
    code, result = _compare(tmp_path, tampered, baseline)
    assert code == 1 and any("疑似手改" in item for item in result["violations"])

    absent = copy.deepcopy(baseline)
    absent["mechanism"] = "absent"
    code, result = _compare(tmp_path, absent, baseline)
    assert code == 1 and any("只认 present" in item for item in result["violations"])

    dropped = copy.deepcopy(baseline)
    dropped["cases"] = dropped["cases"][1:]
    dropped["total"] = len(dropped["cases"])
    dropped["passed"] = sum(1 for case in dropped["cases"] if case["passed"])
    dropped["metrics"] = mc.metrics_from_case_dicts(dropped["cases"])
    code, result = _compare(tmp_path, dropped, baseline)
    assert code == 1 and any("消失" in item for item in result["violations"])

    regressed = copy.deepcopy(baseline)
    victim = next(case for case in regressed["cases"] if case["kind"] == "web")
    victim["passed"] = False
    victim["counters"]["key_leaks"] = 1
    regressed["passed"] -= 1
    regressed["metrics"] = mc.metrics_from_case_dicts(regressed["cases"])
    code, result = _compare(tmp_path, regressed, baseline)
    assert code == 1
    assert any("PASS_TO_PASS" in item for item in result["violations"])
    assert any("key_leak_count" in item for item in result["violations"])

    moved = copy.deepcopy(baseline)
    moved["dataset_sha256"] = "0" * 64
    code, result = _compare(tmp_path, moved, baseline)
    assert code == 1 and any("数据集与基线不同" in item for item in result["violations"])


def test_comparator_direction_of_each_metric() -> None:
    """计数类与 ``benign_selection_rejected_rate`` 越小越好，其余速率越大越好：比较器按此判"变差"。"""

    import importlib.util

    spec = importlib.util.spec_from_file_location("compare_model_choice_p0", COMPARE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in mc.HARD_TARGETS:
        lower = name in module._LOWER_IS_BETTER
        assert lower == (name.endswith("_count") or name == "benign_selection_rejected_rate"), name
        assert module._worse(name, 1.0, 0.5) is (not lower)
        assert module._worse(name, 0.5, 1.0) is lower
        assert module._worse(name, 1.0, 1.0) is False


# ---------------------------------------------------------------------------
# 每个硬指标一条反证
# ---------------------------------------------------------------------------


def _ids(prefix: str) -> list[str]:
    cases, _ = mc.load_cases(DATASET)
    return [case.id for case in cases if case.id.startswith(prefix)]


def _metrics_for(ids: list[str], workdir: Path, *, mutate_case: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    cases, _ = mc.load_cases(DATASET)
    wanted = [case for case in cases if case.id in set(ids)]
    assert len(wanted) == len(ids), "用例 id 与数据集不一致"
    if mutate_case is not None:
        wanted = [mutate_case(case) for case in wanted]
    results = [mc.run_case(case, workdir) for case in wanted]
    return mc.metrics_from_case_dicts([result.as_dict() for result in results])


def _off_target(metrics: dict[str, Any]) -> set[str]:
    return {name for name, target in mc.HARD_TARGETS.items() if metrics.get(name) is not None and metrics[name] != target}


def _problem(code: str = "WEB_MODEL_UNKNOWN") -> Any:
    from water_agent.webapp.model_choice import ModelChoiceProblem

    return ModelChoiceProblem(code, "x", 422)


def _patch_no_validation(mp: pytest.MonkeyPatch) -> None:
    from water_agent.webapp import run_routes

    mp.setattr(run_routes, "check_model_choice", lambda *a, **k: None)


def _patch_decision_after_use(mp: pytest.MonkeyPatch) -> None:
    """授权判定太晚：先按所选模型建出会话（工厂已收到模型），再回 4xx。"""

    from water_agent.webapp import run_routes

    created: list[Any] = []
    original_make = mc._make_factories

    def make(*args: Any, **kwargs: Any) -> dict[str, Any]:
        wrapped: dict[str, Any] = {}
        for name, cls in original_make(*args, **kwargs).items():

            class Tracked(cls):  # type: ignore[valid-type, misc]
                def __init__(self, *a: Any, **k: Any) -> None:
                    super().__init__(*a, **k)
                    created.append(self)

            wrapped[name] = Tracked
        return wrapped

    original_check = run_routes.check_model_choice

    def late(model_id: str, **kwargs: Any) -> Any:
        problem = original_check(model_id, **kwargs)
        if problem is not None and created:
            created[-1].calls.append(model_id)
        return problem

    mp.setattr(mc, "_make_factories", make)
    mp.setattr(run_routes, "check_model_choice", late)


def _patch_strip_endpoint_fields(mp: pytest.MonkeyPatch) -> None:
    from water_agent.webapp import settings_routes

    original = settings_routes.parse_entries

    def lenient(raw: Any) -> Any:
        cleaned = [{k: v for k, v in item.items() if k not in {"base_url", "api_key"}} if isinstance(item, dict) else item for item in raw]
        return original(cleaned)

    mp.setattr(settings_routes, "parse_entries", lenient)


def _patch_resume_drops_model(mp: pytest.MonkeyPatch) -> None:
    from water_agent.webapp.run_manager import RunManager

    original = RunManager.resume

    def resume(self: Any, **kwargs: Any) -> Any:
        kwargs["model"] = None
        return original(self, **kwargs)

    mp.setattr(RunManager, "resume", resume)


def _patch_payload_always_has_model(mp: pytest.MonkeyPatch) -> None:
    from water_agent.webapp import workspace_routes

    original = workspace_routes._session_payload

    def payload(session: Any, runs: list | None = None) -> dict[str, object]:
        result = original(session, runs)
        result.setdefault("model", getattr(session, "model", None))
        return result

    mp.setattr(workspace_routes, "_session_payload", payload)


def _patch_shared_pair_report(mp: pytest.MonkeyPatch, tmp: Path) -> None:
    from water_agent.generation import compatibility

    tmp.mkdir(parents=True, exist_ok=True)
    mp.setattr(compatibility, "pair_report_path", lambda base_url, model: tmp / "shared-report.json")


def _patch_cost_ignores_model(mp: pytest.MonkeyPatch) -> None:
    from water_agent.generation import cost
    from water_agent.graphs import agent_rag

    def flat(_model: str, input_tokens: int, output_tokens: int) -> float:
        return cost.estimate_deepseek_cost(input_tokens, output_tokens)

    mp.setattr(cost, "estimate_model_cost", flat)
    mp.setattr(agent_rag, "estimate_model_cost", flat)


def _patch_lock_ignored(mp: pytest.MonkeyPatch) -> None:
    from water_agent.webapp import model_choice

    mp.setattr(model_choice, "locked_reason", lambda: None)
    mp.setattr(model_choice, "chat_model_problem", lambda **_k: None)


def _patch_audit_leaks_key(mp: pytest.MonkeyPatch) -> None:
    from water_agent.webapp import settings_routes
    from water_agent.webapp.auth import audit

    original = settings_routes.audit_llm_models_updated

    def leaky(**kwargs: Any) -> None:
        original(**kwargs)
        audit._emit("settings.llm_models_updated", {"debug": mc.KEY_MARKER})

    mp.setattr(settings_routes, "audit_llm_models_updated", leaky)


def _patch_everything_rejected(mp: pytest.MonkeyPatch) -> None:
    from water_agent.webapp import run_routes

    mp.setattr(run_routes, "check_model_choice", lambda *a, **k: _problem())


def _unknown_kind(case: Any) -> Any:
    return case.model_copy(update={"kind": "no-such-kind"})


#: (metric, 反证补丁, 相关用例 id 列表)
_COUNTEREXAMPLES: list[tuple[str, Callable[..., None], list[str], str | None]] = [
    ("selection_validation_correct_rate", lambda mp, tmp: _patch_no_validation(mp), [*_ids("mc-sel-unknown"), *_ids("mc-sel-disabled"), *_ids("mc-sel-role-forbidden")], None),
    ("unauthorized_model_use_count", lambda mp, tmp: _patch_decision_after_use(mp), _ids("mc-sel-role-forbidden") + _ids("mc-sel-roundtrip"), None),
    ("base_url_accepted_count", lambda mp, tmp: _patch_strip_endpoint_fields(mp), _ids("mc-cat-endpoint-field") + _ids("mc-cat-secret-field"), None),
    ("silent_fallback_count", lambda mp, tmp: _patch_resume_drops_model(mp), _ids("mc-resume-restores"), None),
    ("default_behavior_identical_rate", lambda mp, tmp: _patch_payload_always_has_model(mp), _ids("mc-cat-empty-identical") + _ids("mc-prio-none"), None),
    ("per_pair_verification_isolation_rate", lambda mp, tmp: _patch_shared_pair_report(mp, tmp), _ids("mc-ver-"), None),
    ("cost_attribution_correct_rate", lambda mp, tmp: _patch_cost_ignores_model(mp), _ids("mc-gov-cost"), None),
    ("local_model_override_leak_count", lambda mp, tmp: _patch_lock_ignored(mp), _ids("mc-sel-local-model") + _ids("mc-sel-env-locked"), None),
    ("key_leak_count", lambda mp, tmp: _patch_audit_leaks_key(mp), _ids("mc-sec-no-key"), None),
    ("benign_selection_rejected_rate", lambda mp, tmp: _patch_everything_rejected(mp), _ids("mc-sel-benign"), None),
    ("harness_error_count", lambda mp, tmp: None, _ids("mc-cat-empty-identical"), "unknown-kind"),
]


@pytest.mark.parametrize(
    ("metric", "patch", "ids", "mutation"),
    _COUNTEREXAMPLES,
    ids=[item[0] for item in _COUNTEREXAMPLES],
)
def test_each_hard_metric_goes_red_when_its_protection_is_removed(
    metric: str,
    patch: Callable[..., None],
    ids: list[str],
    mutation: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P7-S15-A11 反证：拿掉对应保护，只跑相关用例，该硬指标必须离开目标值；不拿掉时同一批用例全在目标上。"""

    assert ids, "相关用例 id 为空：数据集改名了？"
    control = _metrics_for(ids, tmp_path / "control")
    assert _off_target(control) == set(), control

    with monkeypatch.context() as mp:
        patch(mp, tmp_path / "pair-report")
        broken = _metrics_for(
            ids, tmp_path / "broken", mutate_case=_unknown_kind if mutation == "unknown-kind" else None
        )
    assert metric in _off_target(broken), (metric, broken)

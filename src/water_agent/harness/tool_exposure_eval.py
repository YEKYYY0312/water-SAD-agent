"""tool_exposure_p0：按意图最小暴露的机制层评测（P7-S4，规格 §7 P7-S4-A01 / A09）。

测的是**模型这一次实际看到了什么**：每条用例走生产 ``AgenticSession``（真实
``assemble_tools``、提示装配、护栏、checkpoint），只把模型换成脚本模型；脚本模型记下每次
调用绑定的 ``tools`` 与系统提示，harness 据此判定：

- 本轮需要的工具是否开放了、是否写进了工具指引（指引被截断的那几行等于没写）；
- 明显无关的工具是否被开放；
- 一次调用发出的 schema 是否超过 4500 字符（与 ``tests/test_answer_cache.py`` 同一上限）；
- 模型调用了本轮未开放的工具时，是否照常执行且在 trace 里被标记。

**不测**模型拿到工具后选得对不对——那要真实模型（规格 P7-S4-A13，无 Key 即 blocked）。
用例是合成的，标签与暴露规则出自同一作者：召回率 1.0 只说明这些问法被覆盖，不说明任意
用户表达都能命中。

脚本必须恰好用完（多调抛 ``ScriptExhausted``、少调报剩余步），与 ``agentic_p0`` 同一纪律。
用例的最终回答一律是非规范性文本，避免引用校验回流改变调用次数。

退出码：0 全部硬指标达标；1 有硬指标不达标或用例执行出错；2 加载问题、0 条用例或参数错误。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import sys
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from water_agent.harness.doubles import ScriptedChatModel, ScriptExhausted

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO_ROOT / "data" / "eval" / "tool_exposure_p0.jsonl"
FIXTURE_CLAUSES = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
FAKE_MCP_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_mcp_server.py"

SUITE = "tool_exposure_p0"
GENERATED_BY = "water_agent.harness.tool_exposure_eval"
REPORT_FORMAT = 1

#: 用例里 MCP 工具名的 server 段固定为这个 id（``mcp_txfake_echo_query``）。
MCP_SERVER_ID = "txfake"
MCP_ALLOW = ("echo_query", "lookup_project")
DRAWING_ID = "P-01"

#: 一次调用发出的工具 schema 上限，与 ``tests/test_answer_cache.py`` 的 4500 同一口径
#: （``json.dumps(..., ensure_ascii=False)`` 的字符数）。这里只读、不调。
SCHEMA_BUDGET = 4500
GUIDE_HEADER = "【可用工具】"
TRUNCATION_MARK = "受预算限制省略"
#: S4 的 core 组（执行 Prompt §0）。只用于观测指标 ``overexposure_rate``：开放了 core 之外、
#: 本轮又不需要的工具才算多开。
CORE_TOOLS = frozenset({"retrieve_clauses", "cite_clauses", "calc_velocity"})

#: 硬指标与目标值。速率为 ``None``（分母为 0、未测量）一律判不达标：未测量不是通过。
HARD_TARGETS: dict[str, float | int] = {
    "needed_tool_exposed_rate": 1.0,
    "needed_tool_in_guide_rate": 1.0,
    "forbidden_tool_exposed_count": 0,
    "schema_budget_violation_count": 0,
    "guide_truncated_count": 0,
    "unexposed_call_executed_rate": 1.0,
    "harness_error_count": 0,
}


class HarnessUsageError(RuntimeError):
    """参数或环境不对，评测无法开始（退出码 2）。"""


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScriptCall(_Strict):
    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class ScriptStep(_Strict):
    content: str = ""
    tool_calls: list[ScriptCall] = Field(default_factory=list)


class Deployment(_Strict):
    drawing: bool = False
    mcp: bool = False


class ExposureTurn(_Strict):
    query: str = Field(min_length=1)
    script: list[ScriptStep] = Field(min_length=1)
    #: 必填（可以是空列表）：缺了就是加载问题，不能悄悄当成"什么都不需要"而通过。
    must_expose: list[str]
    must_not_expose: list[str] = Field(default_factory=list)
    expect_unexposed_call: str | None = None


class ExposureCase(_Strict):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    description: str = ""
    deployment: Deployment = Field(default_factory=Deployment)
    turns: list[ExposureTurn] = Field(min_length=1)


def load_cases(path: Path) -> tuple[list[ExposureCase], list[str]]:
    """读用例；任何一行坏掉都记为加载问题（整套评测随之 exit 2），不跳过。"""

    cases: list[ExposureCase] = []
    problems: list[str] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            case = ExposureCase.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            problems.append(f"第 {line_no} 行：{exc}")
            continue
        if case.id in seen:
            problems.append(f"第 {line_no} 行：用例 id 重复 {case.id!r}")
            continue
        seen.add(case.id)
        cases.append(case)
    return cases, problems


# ---------------------------------------------------------------------------
# 观测
# ---------------------------------------------------------------------------


def tool_names(tools: Sequence[Any]) -> list[str]:
    names: list[str] = []
    for item in tools:
        function = item.get("function", {}) if isinstance(item, dict) else {}
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return names


def schema_chars(tools: Sequence[Any]) -> int:
    return len(json.dumps(list(tools), ensure_ascii=False))


def guide_segment(messages: Sequence[Any]) -> str:
    """系统提示里的工具指引段（到下一个空行为止）；没有就返回空串。"""

    for message in messages:
        if getattr(message, "type", "") != "system":
            continue
        content = message.content if isinstance(message.content, str) else str(message.content)
        start = content.find(GUIDE_HEADER)
        if start < 0:
            return ""
        end = content.find("\n\n", start)
        return content[start:] if end < 0 else content[start:end]
    return ""


def guide_tools(guide: str) -> list[str]:
    """指引里逐行列出的工具名（``- name：…``）。截断标记与说明行不算。"""

    names: list[str] = []
    for line in guide.splitlines():
        if line.startswith("- ") and "：" in line:
            names.append(line[2:].split("：", 1)[0].strip())
    return names


def _events(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((trace or {}).get("events", []) or [])


def _executed(events: Iterable[dict[str, Any]], tool: str) -> bool:
    return any(
        event.get("event_type") == "tool"
        and (event.get("data") or {}).get("tool_name") == tool
        and (event.get("data") or {}).get("executed") is True
        for event in events
    )


def _marked_unexposed(events: Iterable[dict[str, Any]], tool: str) -> bool:
    return any(
        (event.get("data") or {}).get("unexposed_call") is True
        and (event.get("data") or {}).get("tool_name") == tool
        for event in events
    )


@dataclass
class TurnResult:
    query_index: int
    exposed: list[str] = field(default_factory=list)
    guide_tools: list[str] = field(default_factory=list)
    schema_chars_first: int = 0
    schema_chars_max: int = 0
    guide_chars: int = 0
    guide_truncated: bool = False
    missing: list[str] = field(default_factory=list)
    not_in_guide: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    needed: int = 0
    overexposed: bool = False
    unexposed_expected: str | None = None
    unexposed_ok: bool | None = None
    status: str = ""
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn": self.query_index,
            "exposed": self.exposed,
            "guide_tools": self.guide_tools,
            "schema_chars_first": self.schema_chars_first,
            "schema_chars_max": self.schema_chars_max,
            "guide_chars": self.guide_chars,
            "guide_truncated": self.guide_truncated,
            "missing": self.missing,
            "not_in_guide": self.not_in_guide,
            "forbidden": self.forbidden,
            "needed": self.needed,
            "overexposed": self.overexposed,
            "unexposed_expected": self.unexposed_expected,
            "unexposed_ok": self.unexposed_ok,
            "status": self.status,
            "failures": self.failures,
        }


@dataclass
class CaseResult:
    case_id: str
    category: str
    turns: list[TurnResult] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and not any(turn.failures for turn in self.turns)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "category": self.category,
            "passed": self.passed,
            "error": self.error,
            "turns": [turn.as_dict() for turn in self.turns],
        }


def score_turn(
    turn: ExposureTurn,
    *,
    index: int,
    bound_tools: Sequence[Sequence[Any]],
    received: Sequence[Sequence[Any]],
    trace: dict[str, Any] | None,
    calls: int,
    status: str,
) -> TurnResult:
    """按一轮的观测打分。只看本轮第一次模型调用的指引与工具（本轮"看到了什么"）；
    schema 上限对本轮每一次调用都检查。"""

    result = TurnResult(query_index=index, status=status, needed=len(set(turn.must_expose)))
    if calls != len(turn.script):
        result.failures.append(f"脚本应恰好用完 {len(turn.script)} 步，实际模型被调用 {calls} 次")
    if not bound_tools or not received:
        result.failures.append("本轮模型一次都没有被调用（被护栏拦下或装配失败），无法观测暴露")
        result.missing = sorted(set(turn.must_expose))
        result.not_in_guide = sorted(set(turn.must_expose))
        return result
    exposed = tool_names(bound_tools[0])
    guide = guide_segment(received[0])
    listed = guide_tools(guide)
    result.exposed = exposed
    result.guide_tools = listed
    result.schema_chars_first = schema_chars(bound_tools[0])
    result.schema_chars_max = max(schema_chars(tools) for tools in bound_tools)
    result.guide_chars = len(guide)
    result.guide_truncated = TRUNCATION_MARK in guide
    result.missing = sorted(set(turn.must_expose) - set(exposed))
    result.not_in_guide = sorted(set(turn.must_expose) - set(listed))
    result.forbidden = sorted(set(turn.must_not_expose) & set(exposed))
    result.overexposed = bool(set(exposed) - set(turn.must_expose) - CORE_TOOLS)
    if result.missing:
        result.failures.append(f"需要的工具未开放：{result.missing}")
    if result.not_in_guide:
        result.failures.append(f"需要的工具不在工具指引里：{result.not_in_guide}")
    if result.forbidden:
        result.failures.append(f"无关工具被开放：{result.forbidden}")
    if result.schema_chars_max > SCHEMA_BUDGET:
        result.failures.append(f"发出的 schema {result.schema_chars_max} > {SCHEMA_BUDGET}")
    if result.guide_truncated:
        result.failures.append("工具指引被截断")
    if turn.expect_unexposed_call:
        tool = turn.expect_unexposed_call
        events = _events(trace)
        result.unexposed_expected = tool
        result.unexposed_ok = (
            tool not in exposed and _executed(events, tool) and _marked_unexposed(events, tool)
        )
        if not result.unexposed_ok:
            result.failures.append(
                f"未开放工具 {tool} 的调用应照常执行且在 trace 标记 unexposed_call"
                f"（开放={tool in exposed}，执行={_executed(events, tool)}，"
                f"标记={_marked_unexposed(events, tool)}）"
            )
    return result


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def current_strategy() -> str:
    """当前代码实际使用的暴露策略。S4 Step 1 时还没有策略模块，如实记为 ``baseline-s3``。"""

    try:
        module = importlib.import_module("water_agent.plugins.exposure")
    except ModuleNotFoundError:
        return "baseline-s3"
    return str(module.resolve_exposure_strategy(os.environ))


def _write_mcp_config(path: Path) -> Path:
    from water_agent.mcp.config import MCP_CONFIG_VERSION

    if not FAKE_MCP_SERVER.is_file():
        raise HarnessUsageError(f"缺少 fake MCP server：{FAKE_MCP_SERVER}")
    payload = {
        "version": MCP_CONFIG_VERSION,
        "confirmation_handler_ready": False,
        "servers": [
            {
                "server_id": MCP_SERVER_ID,
                "command": sys.executable,
                "args": [str(FAKE_MCP_SERVER), "--mode", "ok"],
                "allow": list(MCP_ALLOW),
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _session_args(workdir: Path, drawing_root: str) -> argparse.Namespace:
    # 与 HarnessRunner._agentic_args 同形：mock 生成器、fixture 语料、不走真实索引
    return argparse.Namespace(
        db_path=None,
        dimensions=None,
        rerank="local",
        rerank_model=None,
        rerank_base_url=None,
        rerank_api_key=None,
        top_k=5,
        recall_top_k=20,
        hybrid_alpha=0.7,
        model="deepseek-chat",
        base_url=None,
        api_key_env="DEEPSEEK_API_KEY",
        api_key=None,
        mock_generator=True,
        session_dir=str(workdir / "sessions"),
        trace_dir=str(workdir / "traces"),
        stream=False,
        drawing_root=drawing_root,
    )


def run_case(case: ExposureCase, workdir: Path, *, strategy: str | None) -> CaseResult:
    from langgraph.checkpoint.memory import InMemorySaver

    from water_agent.harness.tool_assembly_snapshot import hermetic_environment
    from water_agent.retrieval.loader import load_clauses_jsonl
    from water_agent.retrieval.trivium import TriviumRetriever
    from water_agent.runtime.agentic import AgenticSession
    from water_agent.session import default_checkpoint_serde

    result = CaseResult(case_id=case.id, category=case.category)
    case_dir = workdir / case.id
    clauses = load_clauses_jsonl(FIXTURE_CLAUSES)
    with hermetic_environment(case_dir):
        os.environ.pop("WATER_AGENT_TOOL_EXPOSURE", None)
        if strategy is not None:
            os.environ["WATER_AGENT_TOOL_EXPOSURE"] = strategy
        drawing_root = ""
        if case.deployment.drawing:
            from water_agent.drawing.synth import build_synthetic_drawing

            drawings = case_dir / "drawings"
            drawings.mkdir(parents=True, exist_ok=True)
            build_synthetic_drawing(drawings, drawing_id=DRAWING_ID)
            drawing_root = str(drawings)
        if case.deployment.mcp:
            os.environ["WATER_AGENT_MCP_CONFIG"] = str(_write_mcp_config(case_dir / "mcp.json"))
        saver = InMemorySaver(serde=default_checkpoint_serde())
        thread_id = f"{case.id}-{uuid4().hex[:8]}"
        for number, turn in enumerate(case.turns, start=1):
            model = ScriptedChatModel(
                steps=[step.model_dump(mode="json") for step in turn.script],
                id_prefix=f"{case.id}-t{number}",
            )
            payload: dict[str, Any] = {}
            trace: dict[str, Any] | None = None
            try:
                with ExitStack() as stack:
                    session = AgenticSession(
                        _session_args(case_dir, drawing_root),
                        stack=stack,
                        thread_id=thread_id,
                        checkpointer=saver,
                        model=model,
                        retriever=TriviumRetriever(db_path=None, fallback_clauses=clauses),
                        mcp_enabled=case.deployment.mcp,
                    )
                    tokens: list[str] = []
                    payload = asyncio.run(
                        session.aask(turn.query, run_id=uuid4().hex, on_token=tokens.append)
                    )
                    trace = session.recorder.snapshot()
            except ScriptExhausted as exc:
                result.turns.append(
                    TurnResult(query_index=number, failures=[f"ScriptExhausted：{exc}"])
                )
                continue
            except Exception as exc:  # noqa: BLE001 — 记为用例错误（硬指标 harness_error_count）
                result.error = f"turn {number}: {type(exc).__name__}: {exc}"
                return result
            result.turns.append(
                score_turn(
                    turn,
                    index=number,
                    bound_tools=model.state.get("tools", []),
                    received=model.state["received"],
                    trace=trace,
                    calls=int(model.state["calls"]),
                    status=str(payload.get("status") or ""),
                )
            )
    return result


# ---------------------------------------------------------------------------
# 指标与报告
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def _mean(values: Sequence[int]) -> float | None:
    return None if not values else round(sum(values) / len(values), 3)


def metrics_from_case_dicts(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """从报告里的用例明细算出全部指标。harness 写报告与比较器核对"报告没被手改"共用这一个函数。"""

    needed = exposed_needed = in_guide = 0
    forbidden = budget = truncated = 0
    unexposed_total = unexposed_ok = 0
    over = observed_turns = 0
    counts: list[int] = []
    chars: list[int] = []
    for case in cases:
        for turn in case.get("turns", []) or []:
            want = int(turn.get("needed", 0))
            needed += want
            exposed_needed += want - len(turn.get("missing", []))
            in_guide += want - len(turn.get("not_in_guide", []))
            forbidden += len(turn.get("forbidden", []))
            budget += int(int(turn.get("schema_chars_max", 0)) > SCHEMA_BUDGET)
            truncated += int(bool(turn.get("guide_truncated")))
            if turn.get("unexposed_expected"):
                unexposed_total += 1
                unexposed_ok += int(bool(turn.get("unexposed_ok")))
            if turn.get("exposed"):
                observed_turns += 1
                counts.append(len(turn["exposed"]))
                chars.append(int(turn.get("schema_chars_first", 0)))
                over += int(bool(turn.get("overexposed")))
    return {
        "needed_tool_exposed_rate": _rate(exposed_needed, needed),
        "needed_tool_in_guide_rate": _rate(in_guide, needed),
        "forbidden_tool_exposed_count": forbidden,
        "schema_budget_violation_count": budget,
        "guide_truncated_count": truncated,
        "unexposed_call_executed_rate": _rate(unexposed_ok, unexposed_total),
        "harness_error_count": sum(1 for case in cases if case.get("error") is not None),
        # 观测指标（不设硬门槛，进基线对比）
        "exposed_tool_count_mean": _mean(counts),
        "exposed_tool_count_max": max(counts) if counts else None,
        "exposed_schema_chars_mean": _mean(chars),
        "exposed_schema_chars_max": max(chars) if chars else None,
        "overexposure_rate": _rate(over, observed_turns),
    }


def aggregate(results: Sequence[CaseResult]) -> dict[str, Any]:
    return metrics_from_case_dicts([result.as_dict() for result in results])


def hard_violations(metrics: dict[str, Any]) -> list[str]:
    return [
        f"{name}={metrics.get(name)!r}（目标 {target!r}）"
        for name, target in HARD_TARGETS.items()
        if metrics.get(name) != target
    ]


def build_report(
    results: Sequence[CaseResult],
    *,
    dataset: Path,
    strategy: str,
) -> dict[str, Any]:
    metrics = aggregate(results)
    violations = hard_violations(metrics)
    try:
        dataset_label = dataset.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        dataset_label = dataset.name
    return {
        "suite": SUITE,
        "generated_by": GENERATED_BY,
        "report_format": REPORT_FORMAT,
        "strategy": strategy,
        "dataset": dataset_label,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "total": len(results),
        "passed": sum(1 for result in results if result.passed),
        "metrics": metrics,
        "hard_targets": dict(HARD_TARGETS),
        "hard_violations": violations,
        "ok": not violations,
        "cases": [result.as_dict() for result in results],
    }


def run(dataset: Path, *, strategy: str | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    cases, problems = load_cases(dataset)
    if problems:
        return None, problems
    if not cases:
        return None, ["0 条可执行用例"]
    if strategy is not None and current_strategy() == "baseline-s3":
        raise HarnessUsageError("当前代码没有暴露策略模块，--strategy 无从生效（S4 Step 1 只能测现行行为）")
    with tempfile.TemporaryDirectory(prefix="wa-tx-") as tmp:
        workdir = Path(tmp)
        results = [run_case(case, workdir, strategy=strategy) for case in cases]
        saved = os.environ.get("WATER_AGENT_TOOL_EXPOSURE")
        try:
            if strategy is not None:
                os.environ["WATER_AGENT_TOOL_EXPOSURE"] = strategy
            effective = current_strategy()
        finally:
            if saved is None:
                os.environ.pop("WATER_AGENT_TOOL_EXPOSURE", None)
            else:
                os.environ["WATER_AGENT_TOOL_EXPOSURE"] = saved
    return build_report(results, dataset=dataset, strategy=effective), []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument("--strategy", choices=("intent", "full"), default=None)
    args = parser.parse_args(argv)
    try:
        report, problems = run(args.dataset, strategy=args.strategy)
    except HarnessUsageError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2
    if report is None:
        for problem in problems:
            print(f"!! 加载问题：{problem}", file=sys.stderr)
        return 2
    if args.report_path is not None:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"{SUITE} strategy={report['strategy']} passed={report['passed']}/{report['total']}")
    for name, value in report["metrics"].items():
        print(f"  {name}: {value}")
    for case in report["cases"]:
        if not case["passed"]:
            detail = case["error"] or "; ".join(
                f"t{turn['turn']}: {'; '.join(turn['failures'])}"
                for turn in case["turns"]
                if turn["failures"]
            )
            print(f"  FAIL {case['id']}: {detail}")
    if report["hard_violations"]:
        print("!! 硬指标不达标：" + "；".join(report["hard_violations"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HARD_TARGETS",
    "ExposureCase",
    "ExposureTurn",
    "aggregate",
    "build_report",
    "guide_segment",
    "guide_tools",
    "hard_violations",
    "load_cases",
    "main",
    "metrics_from_case_dicts",
    "run",
    "run_case",
    "score_turn",
]

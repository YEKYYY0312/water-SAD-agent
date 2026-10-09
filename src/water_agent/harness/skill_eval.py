"""skill_p0：Skill 库的机制层评测（P7-S9，规格 §7 P7-S9-A10）。

每条用例走生产 ``AgenticSession``（真实 ``assemble_tools``、提示装配、护栏、引用校验、checkpoint），只把
模型换成脚本模型。harness 据此判定四类事实：

- **catalog**：本轮是否开放了 ``load_skill``、工具描述里的目录列了哪些 skill、最紧组合下 skills 组是否让位；
  运维目录里不安全 / 违反铁律 / 缺工具 / 同名的 skill 是否被拒并带对的错误码；
- **injection**：``load_skill`` 之后，下一次模型调用的系统提示里是否出现那份正文、来源标记对不对；工具结果
  只留回执；跨轮仍在、重复加载去重；
- **revocation**：运维删掉 / 改掉 skill、管理员停用之后，下一轮不再注入并如实记 ``skill_unavailable``；
- **iron_rules**：加载了规程之后，数值仍只来自 ``calc_velocity``、引用仍经 ``cite_clauses`` + 校验器。

**不测**真实模型会不会在该加载时加载、加载后会不会照做（规格 P7-S9-A13，无 Key 即 blocked）。用例是
合成的，triggers 与用例同一作者：命中率 1.0 只说明这些问法被覆盖。

脚本必须恰好用完（多调抛 ``ScriptExhausted``、少调报剩余步），与 ``agentic_p0`` 同一纪律。数据集在 S9
Step 1 冻结；之后改它必须同时重新固化入仓基线并写明原因。

退出码：0 全部硬指标达标；1 有硬指标不达标或用例执行出错；2 加载问题、0 条用例或参数错误。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import re
import shutil
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
DEFAULT_DATASET = REPO_ROOT / "data" / "eval" / "skill_p0.jsonl"
FIXTURE_CLAUSES = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
OPERATOR_SKILLS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "skills" / "operator"

SUITE = "skill_p0"
GENERATED_BY = "water_agent.harness.skill_eval"
REPORT_FORMAT = 1
DRAWING_ID = "P-01"

#: 运行时契约（S9 执行 Prompt §0 / §1）。harness 只按这些名字观测，不 import 运行时代码里的常量——
#: 机制还不存在时（S9 Step 1）同样能如实跑出"失败"。
SKILL_TOOL = "load_skill"
SKILL_GROUP = "skills"
SKILLS_SWITCH_ENV = "WATER_AGENT_SKILLS"
SKILLS_DIR_ENV = "WATER_AGENT_SKILLS_DIR"
WEB_LAYER_ENV = "WATER_AGENT_MCP_WEB_LAYER"
SKILL_CONTEXT_HEADER = "【已加载规程】"
#: 系统提示里每份已加载规程的包裹：``<skill_content name="…" source="builtin|operator">``
_SKILL_BLOCK_RE = re.compile(r'<skill_content name="([a-z0-9-]+)" source="(builtin|operator)"')
#: 一次调用发出的工具 schema 上限，与 ``tests/test_answer_cache.py`` 同一口径。只读、不调。
SCHEMA_BUDGET = 4500

#: 硬指标与目标值。速率为 ``None``（分母为 0、未测量）一律判不达标：未测量不是通过。
HARD_TARGETS: dict[str, float | int] = {
    "expected_exposure_rate": 1.0,
    "forbidden_exposure_count": 0,
    "catalog_match_rate": 1.0,
    "skill_error_match_rate": 1.0,
    "injection_match_rate": 1.0,
    "revocation_match_rate": 1.0,
    "iron_rule_hold_rate": 1.0,
    "forbidden_text_leak_count": 0,
    "schema_budget_violation_count": 0,
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
    #: 打开 Skill 库（``WATER_AGENT_SKILLS=on``）。关闭时与 S9 之前逐字相同。
    skills: bool = False
    #: 挂上运维目录（``tests/fixtures/skills/operator`` 的副本）。
    operator: bool = False
    drawing: bool = False
    #: 用例开始前就在 Web 管理层里停用的 skill。
    disabled: list[str] = Field(default_factory=list)


class BeforeTurn(_Strict):
    """在这一轮开始前对部署做的改动（模拟运维 / 管理员在两轮之间的操作）。"""

    remove_operator_skill: str | None = None
    edit_operator_skill: str | None = None
    disable_skill: str | None = None


class SkillTurn(_Strict):
    query: str = Field(min_length=1)
    script: list[ScriptStep] = Field(min_length=1)
    before_turn: BeforeTurn | None = None
    must_expose: list[str] = Field(default_factory=list)
    must_not_expose: list[str] = Field(default_factory=list)
    #: 目录（``load_skill`` 的 ``name`` 枚举）应当恰好是这些名字（字母序）；``[]`` 表示不该有目录。
    expect_catalog: list[str] | None = None
    expect_dropped: list[str] = Field(default_factory=list)
    #: 名字 → 错误码（来自装配期 trace 的 ``skill_library`` 事件）。
    expect_skill_errors: dict[str, str] = Field(default_factory=dict)
    #: 本轮结束时 ``loaded_skills`` 里的名字（按加载顺序、去重）。
    expect_loaded: list[str] | None = None
    #: 本轮最后一次模型调用的系统提示里，``skill_context`` 段应当恰好列出这些 skill 及其来源。
    expect_skill_context: dict[str, str] | None = None
    forbid_in_system_prompt: list[str] = Field(default_factory=list)
    expect_warning: str | None = None
    expect_tool_error: str | None = None
    expect_status: str | None = None
    expect_executed: list[str] = Field(default_factory=list)
    expect_citations: list[str] | None = None


class SkillCase(_Strict):
    id: str = Field(min_length=1)
    category: str = Field(pattern=r"^(catalog|injection|revocation|iron_rules)$")
    description: str = ""
    deployment: Deployment = Field(default_factory=Deployment)
    turns: list[SkillTurn] = Field(min_length=1)


def load_cases(path: Path) -> tuple[list[SkillCase], list[str]]:
    """读用例；任何一行坏掉都记为加载问题（整套评测随之 exit 2），不跳过。"""

    cases: list[SkillCase] = []
    problems: list[str] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            case = SkillCase.model_validate(json.loads(raw))
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


def catalog_of(tools: Sequence[Any]) -> list[str] | None:
    """``load_skill`` 的 ``skill`` 参数枚举（字母序）；本次没有发出 ``load_skill`` 时为 ``None``。"""

    for item in tools:
        function = item.get("function", {}) if isinstance(item, dict) else {}
        if function.get("name") != SKILL_TOOL:
            continue
        parameters = function.get("parameters") or {}
        prop = (parameters.get("properties") or {}).get("skill") or {}
        values = prop.get("enum")
        if values is None:
            for ref in (parameters.get("$defs") or {}).values():
                if isinstance(ref, dict) and "enum" in ref:
                    values = ref["enum"]
                    break
        return sorted(str(value) for value in (values or []))
    return None


def schema_chars(tools: Sequence[Any]) -> int:
    return len(json.dumps(list(tools), ensure_ascii=False))


def system_prompt(messages: Sequence[Any]) -> str:
    for message in messages:
        if getattr(message, "type", "") == "system":
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def skill_context_of(prompt: str) -> dict[str, str]:
    """系统提示里 ``skill_context`` 段列出的 skill → 来源；没有该段即 ``{}``。"""

    start = prompt.find(SKILL_CONTEXT_HEADER)
    if start < 0:
        return {}
    return {name: source for name, source in _SKILL_BLOCK_RE.findall(prompt[start:])}


def _events(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((trace or {}).get("events", []) or [])


def _executed_tools(events: Iterable[dict[str, Any]]) -> set[str]:
    return {
        str((event.get("data") or {}).get("tool_name"))
        for event in events
        if event.get("event_type") == "tool" and (event.get("data") or {}).get("executed") is True
    }


def _tool_results(received: Sequence[Sequence[Any]], tool: str) -> list[dict[str, Any]]:
    """本轮模型收到的 ``tool`` 的结果（``ToolMessage`` 正文解析成 dict；解析不了的记为空 dict）。

    trace 的工具事件只有 ``tool_status`` / ``code``：缺参这类业务结果是"执行成功、返回里写着缺什么"，
    只能从模型实际收到的结果里看。
    """

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for messages in received:
        for message in messages:
            if getattr(message, "type", "") != "tool" or getattr(message, "name", "") != tool:
                continue
            key = str(getattr(message, "tool_call_id", "") or id(message))
            if key in seen:
                continue
            seen.add(key)
            content = message.content if isinstance(message.content, str) else str(message.content)
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError):
                parsed = {}
            results.append(parsed if isinstance(parsed, dict) else {})
    return results


def _tool_errored(received: Sequence[Sequence[Any]], tool: str) -> bool:
    """这一轮 ``tool`` 至少一次返回了结构化错误（``error`` 为真 / ``ok`` 为假）或缺参（``missing_inputs`` 非空）。"""

    return any(
        result.get("error") is True or result.get("ok") is False or bool(result.get("missing_inputs"))
        for result in _tool_results(received, tool)
    )


def _dropped_groups(events: Iterable[dict[str, Any]]) -> set[str]:
    groups: set[str] = set()
    for event in events:
        if event.get("event_type") != "tool_exposure":
            continue
        for item in (event.get("data") or {}).get("dropped", []) or []:
            if isinstance(item, (list, tuple)) and item:
                groups.add(str(item[0]))
    return groups


def _skill_errors(events: Iterable[dict[str, Any]]) -> dict[str, str]:
    """装配期 ``skill_library`` 事件里记下的 名字 → 错误码。"""

    errors: dict[str, str] = {}
    for event in events:
        if event.get("event_type") != "skill_library":
            continue
        for item in (event.get("data") or {}).get("skipped", []) or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                errors[str(item[0])] = str(item[1])
    return errors


@dataclass
class TurnResult:
    turn: int
    exposed: list[str] = field(default_factory=list)
    catalog: list[str] | None = None
    dropped: list[str] = field(default_factory=list)
    skill_errors: dict[str, str] = field(default_factory=dict)
    loaded: list[str] = field(default_factory=list)
    skill_context: dict[str, str] = field(default_factory=dict)
    status: str = ""
    warnings: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    schema_chars_max: int = 0
    #: 每个检查面：None = 本轮未声明该期望；True / False = 是否达成。
    checks: dict[str, bool | None] = field(default_factory=dict)
    forbidden_exposed: list[str] = field(default_factory=list)
    leaked: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "exposed": self.exposed,
            "catalog": self.catalog,
            "dropped": self.dropped,
            "skill_errors": self.skill_errors,
            "loaded": self.loaded,
            "skill_context": self.skill_context,
            "status": self.status,
            "warnings": self.warnings,
            "citations": self.citations,
            "executed": self.executed,
            "schema_chars_max": self.schema_chars_max,
            "checks": self.checks,
            "forbidden_exposed": self.forbidden_exposed,
            "leaked": self.leaked,
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


def _check(result: TurnResult, name: str, ok: bool, message: str) -> None:
    previous = result.checks.get(name)
    result.checks[name] = ok if previous is None else bool(previous and ok)
    if not ok:
        result.failures.append(message)


def score_turn(
    turn: SkillTurn,
    *,
    index: int,
    category: str,
    bound_tools: Sequence[Sequence[Any]],
    received: Sequence[Sequence[Any]],
    trace: dict[str, Any] | None,
    calls: int,
    payload: dict[str, Any],
    loaded: Sequence[str],
) -> TurnResult:
    """按一轮的观测打分。暴露与目录看本轮**第一次**模型调用；正文注入看本轮**最后一次**模型调用
    （``load_skill`` 之后的那次）；状态 / 引用 / 执行看本轮返回与 trace。"""

    result = TurnResult(turn=index, status=str(payload.get("status") or ""))
    result.warnings = [str(item) for item in payload.get("warnings", []) or []]
    result.citations = [str(item.get("source_id")) for item in payload.get("citations", []) or []]
    result.loaded = list(loaded)
    events = _events(trace)
    result.executed = sorted(_executed_tools(events))
    result.dropped = sorted(_dropped_groups(events))
    result.skill_errors = _skill_errors(events)
    if calls != len(turn.script):
        result.failures.append(f"脚本应恰好用完 {len(turn.script)} 步，实际模型被调用 {calls} 次")
    if not bound_tools or not received:
        result.failures.append("本轮模型一次都没有被调用（被护栏拦下或装配失败），无法观测")
        return result

    result.exposed = tool_names(bound_tools[0])
    result.catalog = catalog_of(bound_tools[0])
    result.schema_chars_max = max(schema_chars(tools) for tools in bound_tools)
    last_prompt = system_prompt(received[-1])
    all_prompts = "\n".join(system_prompt(messages) for messages in received)
    result.skill_context = skill_context_of(last_prompt)

    if turn.must_expose:
        missing = sorted(set(turn.must_expose) - set(result.exposed))
        _check(result, "exposure", not missing, f"应开放的工具没有开放：{missing}")
    result.forbidden_exposed = sorted(set(turn.must_not_expose) & set(result.exposed))
    if turn.must_not_expose:
        _check(result, "exposure", not result.forbidden_exposed,
               f"不该开放的工具被开放：{result.forbidden_exposed}")
    if turn.expect_catalog is not None:
        got = result.catalog or []
        _check(result, "catalog", got == sorted(turn.expect_catalog),
               f"目录应为 {sorted(turn.expect_catalog)}，实际 {got}")
    if turn.expect_dropped:
        missing_drop = sorted(set(turn.expect_dropped) - set(result.dropped))
        _check(result, "catalog", not missing_drop, f"应让位（dropped）的组没有让位：{missing_drop}")
    if turn.expect_skill_errors:
        wrong = {
            name: (code, result.skill_errors.get(name))
            for name, code in turn.expect_skill_errors.items()
            if result.skill_errors.get(name) != code
        }
        _check(result, "skill_errors", not wrong, f"skill 错误码不符（期望, 实际）：{wrong}")
    surface = "revocation" if category == "revocation" else "injection"
    if turn.expect_loaded is not None:
        _check(result, surface, list(loaded) == turn.expect_loaded,
               f"loaded_skills 应为 {turn.expect_loaded}，实际 {list(loaded)}")
    if turn.expect_skill_context is not None:
        _check(result, surface, result.skill_context == turn.expect_skill_context,
               f"skill_context 段应为 {turn.expect_skill_context}，实际 {result.skill_context}")
    if turn.expect_warning is not None:
        _check(result, surface, turn.expect_warning in result.warnings,
               f"应记 warning {turn.expect_warning!r}，实际 {result.warnings}")
    if turn.expect_tool_error is not None:
        name = surface if category != "iron_rules" else "iron_rules"
        _check(result, name, _tool_errored(received, turn.expect_tool_error),
               f"{turn.expect_tool_error} 应返回结构化错误 / 缺参，模型收到的结果里没有")
    result.leaked = [text for text in turn.forbid_in_system_prompt if text in all_prompts]
    if result.leaked:
        result.failures.append(f"不该出现在系统提示里的文本出现了：{result.leaked}")
    if turn.expect_status is not None:
        _check(result, "iron_rules", result.status == turn.expect_status,
               f"状态应为 {turn.expect_status!r}，实际 {result.status!r}")
    if turn.expect_executed:
        missing_exec = sorted(set(turn.expect_executed) - set(result.executed))
        _check(result, "iron_rules", not missing_exec, f"应执行的工具没有执行：{missing_exec}")
    if turn.expect_citations is not None:
        _check(result, "iron_rules", sorted(result.citations) == sorted(turn.expect_citations),
               f"引用应为 {turn.expect_citations}，实际 {result.citations}")
    if result.schema_chars_max > SCHEMA_BUDGET:
        result.failures.append(f"发出的 schema {result.schema_chars_max} > {SCHEMA_BUDGET}")
    return result


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def skills_implemented() -> bool:
    """当前代码有没有 Skill 库（S9 Step 1 时没有，报告如实记 ``mechanism=absent``）。"""

    try:
        importlib.import_module("water_agent.skills")
    except ModuleNotFoundError:
        return False
    return True


def _session_args(workdir: Path, drawing_root: str) -> argparse.Namespace:
    # 与 tool_exposure_eval 同形：mock 生成器、fixture 语料、不走真实索引
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


def _write_web_layer(path: Path, disabled: Iterable[str]) -> None:
    """在 Web 管理层里停用这些 skill（与面板写入的形状一致：``skills: [{name, disabled}]``）。"""

    current: dict[str, Any] = {}
    if path.is_file():
        current = json.loads(path.read_text(encoding="utf-8"))
    entries = {entry["name"]: entry for entry in current.get("skills", [])}
    for name in disabled:
        entries[name] = {"name": name, "disabled": True}
    payload = {
        "revision": int(current.get("revision", 0)) + 1,
        "servers": current.get("servers", []),
        "skills": list(entries.values()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_before_turn(change: BeforeTurn, *, operator_dir: Path, web_layer: Path) -> None:
    if change.remove_operator_skill:
        shutil.rmtree(operator_dir / change.remove_operator_skill)
    if change.edit_operator_skill:
        target = operator_dir / change.edit_operator_skill / "SKILL.md"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n5. （运维追加的一行，改变了正文摘要）\n",
            encoding="utf-8",
        )
    if change.disable_skill:
        _write_web_layer(web_layer, [change.disable_skill])


def _loaded_names(session: Any) -> list[str]:
    """本 thread 的 ``loaded_skills``（按加载顺序）；机制不存在时为空。"""

    try:
        values = getattr(session.app.get_state(session.config), "values", None) or {}
    except Exception:  # noqa: BLE001 — 读不到即视为没有
        return []
    return [str(item.get("name")) for item in values.get("loaded_skills", []) or [] if isinstance(item, dict)]


def run_case(case: SkillCase, workdir: Path) -> CaseResult:
    from langgraph.checkpoint.memory import InMemorySaver

    from water_agent.harness.tool_assembly_snapshot import hermetic_environment
    from water_agent.retrieval.loader import load_clauses_jsonl
    from water_agent.retrieval.trivium import TriviumRetriever
    from water_agent.runtime.agentic import AgenticSession
    from water_agent.session import default_checkpoint_serde

    result = CaseResult(case_id=case.id, category=case.category)
    case_dir = workdir / case.id
    clauses = load_clauses_jsonl(FIXTURE_CLAUSES)
    operator_dir = case_dir / "operator-skills"
    web_layer = case_dir / "mcp-admin" / "web_layer.json"
    with hermetic_environment(case_dir):
        for key in (SKILLS_SWITCH_ENV, SKILLS_DIR_ENV, WEB_LAYER_ENV, "WATER_AGENT_TOOL_EXPOSURE"):
            os.environ.pop(key, None)
        if case.deployment.skills:
            os.environ[SKILLS_SWITCH_ENV] = "on"
        if case.deployment.operator:
            shutil.copytree(OPERATOR_SKILLS_FIXTURE, operator_dir)
            os.environ[SKILLS_DIR_ENV] = str(operator_dir)
        os.environ[WEB_LAYER_ENV] = str(web_layer)
        if case.deployment.disabled:
            _write_web_layer(web_layer, case.deployment.disabled)
        drawing_root = ""
        if case.deployment.drawing:
            from water_agent.drawing.synth import build_synthetic_drawing

            drawings = case_dir / "drawings"
            drawings.mkdir(parents=True, exist_ok=True)
            build_synthetic_drawing(drawings, drawing_id=DRAWING_ID)
            drawing_root = str(drawings)
        saver = InMemorySaver(serde=default_checkpoint_serde())
        thread_id = f"{case.id}-{uuid4().hex[:8]}"
        for number, turn in enumerate(case.turns, start=1):
            if turn.before_turn is not None:
                _apply_before_turn(turn.before_turn, operator_dir=operator_dir, web_layer=web_layer)
            model = ScriptedChatModel(
                steps=[step.model_dump(mode="json") for step in turn.script],
                id_prefix=f"{case.id}-t{number}",
            )
            try:
                with ExitStack() as stack:
                    session = AgenticSession(
                        _session_args(case_dir, drawing_root),
                        stack=stack,
                        thread_id=thread_id,
                        checkpointer=saver,
                        model=model,
                        retriever=TriviumRetriever(db_path=None, fallback_clauses=clauses),
                        mcp_enabled=False,
                    )
                    payload = asyncio.run(session.aask(turn.query, run_id=uuid4().hex))
                    trace = session.recorder.snapshot()
                    loaded = _loaded_names(session)
            except ScriptExhausted as exc:
                result.turns.append(TurnResult(turn=number, failures=[f"ScriptExhausted：{exc}"]))
                continue
            except Exception as exc:  # noqa: BLE001 — 记为用例错误（硬指标 harness_error_count）
                result.error = f"turn {number}: {type(exc).__name__}: {exc}"
                return result
            result.turns.append(
                score_turn(
                    turn,
                    index=number,
                    category=case.category,
                    bound_tools=model.state.get("tools", []),
                    received=model.state["received"],
                    trace=trace,
                    calls=int(model.state["calls"]),
                    payload=payload,
                    loaded=loaded,
                )
            )
    return result


# ---------------------------------------------------------------------------
# 指标与报告
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def metrics_from_case_dicts(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """从报告里的用例明细算出全部指标。harness 写报告与比较器核对"报告没被手改"共用这一个函数。

    每个检查面按"声明了这项期望的轮次"计数：分母为 0 即 ``None``（未测量），判不达标。
    """

    totals: dict[str, list[int]] = {}
    forbidden = leaked = budget = 0
    for case in cases:
        for turn in case.get("turns", []) or []:
            for name, value in (turn.get("checks") or {}).items():
                if value is None:
                    continue
                hit, total = totals.setdefault(name, [0, 0])
                totals[name] = [hit + int(bool(value)), total + 1]
            forbidden += len(turn.get("forbidden_exposed", []) or [])
            leaked += len(turn.get("leaked", []) or [])
            budget += int(int(turn.get("schema_chars_max", 0)) > SCHEMA_BUDGET)

    def rate(name: str) -> float | None:
        hit, total = totals.get(name, [0, 0])
        return _rate(hit, total)

    return {
        "expected_exposure_rate": rate("exposure"),
        "forbidden_exposure_count": forbidden,
        "catalog_match_rate": rate("catalog"),
        "skill_error_match_rate": rate("skill_errors"),
        "injection_match_rate": rate("injection"),
        "revocation_match_rate": rate("revocation"),
        "iron_rule_hold_rate": rate("iron_rules"),
        "forbidden_text_leak_count": leaked,
        "schema_budget_violation_count": budget,
        "harness_error_count": sum(1 for case in cases if case.get("error") is not None),
    }


def aggregate(results: Sequence[CaseResult]) -> dict[str, Any]:
    return metrics_from_case_dicts([result.as_dict() for result in results])


def hard_violations(metrics: dict[str, Any]) -> list[str]:
    return [
        f"{name}={metrics.get(name)!r}（目标 {target!r}）"
        for name, target in HARD_TARGETS.items()
        if metrics.get(name) != target
    ]


def build_report(results: Sequence[CaseResult], *, dataset: Path) -> dict[str, Any]:
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
        "mechanism": "present" if skills_implemented() else "absent",
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


def run(dataset: Path) -> tuple[dict[str, Any] | None, list[str]]:
    cases, problems = load_cases(dataset)
    if problems:
        return None, problems
    if not cases:
        return None, ["0 条可执行用例"]
    if not OPERATOR_SKILLS_FIXTURE.is_dir():
        raise HarnessUsageError(f"缺少运维 skill 夹具：{OPERATOR_SKILLS_FIXTURE}")
    with tempfile.TemporaryDirectory(prefix="wa-skill-") as tmp:
        workdir = Path(tmp)
        results = [run_case(case, workdir) for case in cases]
    return build_report(results, dataset=dataset), []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or SUITE).splitlines()[0])
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--report-path", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        report, problems = run(args.dataset)
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
    print(f"{SUITE} mechanism={report['mechanism']} passed={report['passed']}/{report['total']}")
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
    "GENERATED_BY",
    "HARD_TARGETS",
    "SUITE",
    "SkillCase",
    "SkillTurn",
    "aggregate",
    "build_report",
    "catalog_of",
    "hard_violations",
    "load_cases",
    "main",
    "metrics_from_case_dicts",
    "run",
    "run_case",
    "score_turn",
    "skill_context_of",
]

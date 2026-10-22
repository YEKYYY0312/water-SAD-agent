"""P7-S0 H1：工具装配快照（``docs/design/16-p7-plugin-mcp.md`` §6.2）。

快照由 ``water_agent.harness.tool_assembly_snapshot`` 采集。这里锁住三件事：

1. 与入仓基线 ``data/eval/tool_assembly_main.json`` 逐字段一致；
2. 采集是确定的，且不受本机环境变量影响；
3. 对真实改动有鉴别力：四个反证（改工具描述、调换注册顺序、丢掉授权、改指引预算）
   都必须让快照出现差异，否则"快照一致"什么也证明不了。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from water_agent.harness import tool_assembly_snapshot as snap
from water_agent.runtime.governance.contracts import Capability
from water_agent.tools.registry import ToolRegistry

_PROTOCOL = (
    "有意的变更：按规格 §6.2 递进版本号，用 "
    "`python -m water_agent.harness.tool_assembly_snapshot --write --reason ...` 重写基线，"
    "并把 `--diff` 的输出贴进该步决策记录；无意的变更：修代码，不要改基线。"
)


@pytest.fixture(scope="module")
def head() -> dict[str, Any]:
    return snap.collect_snapshot()


@pytest.fixture(scope="module")
def baseline() -> dict[str, Any]:
    return snap.load_snapshot(snap.DEFAULT_BASELINE)


def _subset(*names: str) -> list[snap.Profile]:
    selected = [profile for profile in snap.PROFILES if profile.name in names]
    assert len(selected) == len(names), f"未知 profile：{names}"
    return selected


def _profile_diff(baseline: dict[str, Any], partial: dict[str, Any]) -> list[str]:
    """只拿 ``partial`` 里跑过的 profile 与基线对比。"""

    names = list(partial["profiles"])
    return snap.diff_snapshots(
        {"profiles": {name: baseline["profiles"][name] for name in names}},
        {"profiles": partial["profiles"]},
    )


def test_snapshot_matches_baseline(head: dict[str, Any], baseline: dict[str, Any]) -> None:
    """P7-S0-A03 / P7-S3-A12 / P7-S4-A10 / P7-S7-A08 / P7-S6-A10 / P7-S9-A11 / P7-S8-A13 / P7-S10-A10 当前代码的装配快照与入仓基线
    逐字段一致（S8 新增 ``session_http_mcp_fake``，其余 16 个 profile 不变；S10 新增 ``session_endpoint_custom``，其余 17 个不变）；S9 新增的 ``session_skills_builtin`` 在基线里，且它是唯一注册了 ``load_skill`` 的 profile
    （其余 profile 开关 off，只有主体能力集多了 ``load_skill``）。"""

    lines = snap.diff_snapshots(baseline, head)
    assert not lines, (
        f"装配快照与入仓基线有 {len(lines)} 处差异。{_PROTOCOL}\n" + "\n".join(lines[:40])
    )
    with_skill_tool = sorted(
        name
        for name, profile in baseline["profiles"].items()
        if profile["surface"] is not None and "load_skill" in profile["surface"]["registration_order"]
    )
    assert with_skill_tool == ["session_skills_builtin"]


def test_snapshot_is_hermetic_and_deterministic(
    head: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S0-A04 / P7-S6-A10 / P7-S9-A11 / P7-S8-A13 / P7-S10-A10 环境变量被污染时再采一次，结果与第一次完全相同，且环境原样恢复。

    ``WATER_AGENT_MCP_WEB_LAYER`` 指向一份损坏的层文件：没被快照钉住的话，带 MCP 的 profile 全会
    变成 ``MCP_CONFIG_INVALID``。``WATER_AGENT_SKILLS=on`` 与一个放了运维 skill 的目录：没被钉住的话，
    开关 off 的 15 个 profile 都会多出 ``load_skill``。
    """

    from water_agent.drawing.synth import build_synthetic_drawing

    drawings = tmp_path / "drawings"
    drawings.mkdir()
    build_synthetic_drawing(drawings, drawing_id="X-01")
    corrupt_web_layer = tmp_path / "web_layer.json"
    corrupt_web_layer.write_text("{ 这不是 JSON", encoding="utf-8")
    import shutil

    operator_skills = tmp_path / "operator-skills"
    shutil.copytree(Path(__file__).resolve().parent / "fixtures" / "skills" / "operator", operator_skills)
    polluted = {
        "DRAWING_ROOT": str(drawings),
        "WATER_AGENT_MCP_CONFIG": str(tmp_path / "missing" / "mcp_servers.json"),
        "WATER_AGENT_MCP_WEB_LAYER": str(corrupt_web_layer),
        "LOCAL_MODEL_ENABLED": "true",
        "WEB_MOCK_GENERATOR": "true",
        "DEEPSEEK_API_KEY": "sk-polluted-environment-0000",
        "WATER_AGENT_SKILLS": "on",
        "WATER_AGENT_SKILLS_DIR": str(operator_skills),
        # P7-S8-A13：授权回调地址的来源也被钉住——污染值不能让任何 profile 的装配发生变化
        "WEB_PUBLIC_BASE_URL": "https://polluted.example.invalid",
        # P7-S10-A10：对话 / embedding 端点、兼容报告与检索模式同样被钉住
        "WATER_AGENT_LLM_BASE_URL": "https://polluted-llm.example.invalid/v1",
        "WATER_AGENT_LLM_MODEL": "polluted-model",
        "WATER_AGENT_LLM_API_KEY": "sk-polluted-llm-0000",
        "WATER_AGENT_LLM_COMPATIBILITY_REPORT": str(tmp_path / "missing-report.json"),
        "WATER_AGENT_LLM_PRICE": "not,a,price",
        "WATER_AGENT_EMBEDDING_MODEL": "polluted-embedding",
        "WATER_AGENT_RETRIEVAL_MODE": "text",
    }
    for key, value in polluted.items():
        monkeypatch.setenv(key, value)

    again = snap.collect_snapshot()

    assert snap.diff_snapshots(head, again) == []
    import os

    assert {key: os.environ.get(key) for key in polluted} == polluted


def test_capture_fires_once_per_profile(head: dict[str, Any]) -> None:
    """P7-S0-A05 装配没抛异常的 profile 恰好捕获一次；抛异常的不产生 surface。"""

    for name, profile in head["profiles"].items():
        lifecycle = profile["lifecycle"]
        if lifecycle["exception"] is None:
            assert lifecycle["captures"] == 1, name
            assert profile["surface"] is not None, name
        else:
            assert lifecycle["captures"] == 0, name
            assert profile["surface"] is None, name


def test_profile_set_covers_both_assembly_paths(head: dict[str, Any]) -> None:
    """P7-S0-A05 会话路径与 runner real-agentic 路径都在快照里，profile 名可作 JSON pointer。"""

    sides = {profile["side"] for profile in head["profiles"].values()}
    assert sides == {"session", "runner"}
    for name in head["profiles"]:
        assert name.replace("_", "").isalnum() and name.islower(), name


def test_baseline_versions_match_code(baseline: dict[str, Any]) -> None:
    """P7-S0-A06 基线记录的版本号与代码里的常量一致（版本号递进必须伴随基线重写）。"""

    assert baseline["versions"] == snap.current_versions()
    assert baseline["format_version"] == snap.SNAPSHOT_FORMAT_VERSION


# ---------------------------------------------------------------------------
# 反证：每一种改动都必须被快照识别出来
# ---------------------------------------------------------------------------


def test_negative_control_description_change_detected(
    baseline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S0-A07 反证：改一条工具描述，schema 摘要与工具指引必须出现差异。"""

    original = ToolRegistry.register

    def register(self: ToolRegistry, spec: Any) -> None:
        if spec.name == "calc_velocity":
            spec = spec.model_copy(update={"description": spec.description + "（反证）"})
        original(self, spec)

    monkeypatch.setattr(ToolRegistry, "register", register)
    lines = _profile_diff(baseline, snap.collect_snapshot(_subset("session_drawing_arg")))

    assert any("/surface/tools/calc_velocity/schema_sha256" in line for line in lines), lines


def test_negative_control_registration_order_detected(
    baseline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S0-A08 反证：调换注册顺序，注册顺序与工具指引变化，发给模型的字母序不变。"""

    original = ToolRegistry.register

    def register_at_front(self: ToolRegistry, spec: Any) -> None:
        original(self, spec)
        self._tools = {spec.name: spec, **{k: v for k, v in self._tools.items() if k != spec.name}}

    monkeypatch.setattr(ToolRegistry, "register", register_at_front)
    lines = _profile_diff(baseline, snap.collect_snapshot(_subset("session_drawing_arg")))

    assert any("/surface/registration_order" in line for line in lines), lines
    assert any("/surface/tool_guide" in line for line in lines), lines
    assert not any("/surface/model_tool_order" in line for line in lines), lines


def test_negative_control_dropped_grant_detected(
    baseline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S0-A09 反证：注册表主体丢掉 MCP_INVOKE，主体能力与 MCP 探针都必须变化。"""

    original_init = ToolRegistry.__init__

    def init(self: ToolRegistry, *, pipeline: Any = None, principal: Any = None,
             trace: Any = None) -> None:
        if principal is not None and Capability.MCP_INVOKE in principal.capabilities:
            principal = principal.model_copy(
                update={"capabilities": principal.capabilities - {Capability.MCP_INVOKE}}
            )
        original_init(self, pipeline=pipeline, principal=principal, trace=trace)

    monkeypatch.setattr(ToolRegistry, "__init__", init)
    lines = _profile_diff(baseline, snap.collect_snapshot(_subset("session_mcp_fake")))

    assert any("/surface/registry_principal/capabilities" in line for line in lines), lines
    assert any("/lifecycle/app/probes/mcp_echo" in line for line in lines), lines


def test_negative_control_guide_budget_detected(
    baseline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S0-A10 反证：把工具指引预算从 800 调到 900，截断消失，快照必须变化。"""

    from water_agent.prompting import assembler

    monkeypatch.setitem(assembler.SEGMENT_BUDGETS, "tool_guide", 900)
    lines = _profile_diff(baseline, snap.collect_snapshot(_subset("session_drawing_arg")))

    assert any("/surface/tool_guide" in line for line in lines), lines


# ---------------------------------------------------------------------------
# 写入守卫与 CLI
# ---------------------------------------------------------------------------


def _minimal(surface: dict[str, Any] | None, lifecycle: dict[str, Any],
             tool_policy: str = "v1") -> dict[str, Any]:
    return {
        "format_version": snap.SNAPSHOT_FORMAT_VERSION,
        "generated_by": snap.GENERATED_BY,
        "versions": {"TOOL_POLICY_VERSION": tool_policy, "PROMPT_TEMPLATE_VERSION": "p1"},
        "profiles": {"session_x": {"side": "session", "surface": surface,
                                   "lifecycle": lifecycle}},
        "info": {},
    }


def test_write_guard_refuses_surface_change_without_version_bump() -> None:
    """P7-S0-A11 写入守卫：模型可见面变了而版本号一个都没递进 → 拒绝写入。"""

    base = _minimal({"registration_order": ["a", "b"]}, {"captures": 1})
    head = _minimal({"registration_order": ["b", "a"]}, {"captures": 1})

    violations = snap.write_guard_violations(base, head, reason="调换顺序")
    assert any("TOOL_POLICY_VERSION" in line for line in violations)

    bumped = _minimal({"registration_order": ["b", "a"]}, {"captures": 1}, tool_policy="v2")
    assert snap.write_guard_violations(base, bumped, reason="调换顺序") == []


def test_write_guard_requires_reason_for_any_change() -> None:
    """P7-S0-A11 写入守卫：任何变化都必须带 --reason；装配成败改变按生命周期变化处理。"""

    base = _minimal({"registration_order": ["a"]}, {"captures": 1})
    lifecycle_change = _minimal({"registration_order": ["a"]}, {"captures": 1, "x": 1})
    assert snap.write_guard_violations(base, lifecycle_change, reason=None)
    assert snap.write_guard_violations(base, lifecycle_change, reason="  ") != []
    assert snap.write_guard_violations(base, lifecycle_change, reason="新增字段") == []

    # 某个 profile 从"采到 surface"变成"装配抛错"：不要求递进版本号，但必须说明原因
    now_raises = _minimal(None, {"captures": 0, "exception": {"type": "ValueError"}})
    assert snap.write_guard_violations(base, now_raises, reason=None)
    assert snap.write_guard_violations(base, now_raises, reason="注册故障改为响亮失败") == []

    assert snap.write_guard_violations(base, copy.deepcopy(base), reason=None) == []


def test_diff_reports_json_pointers_and_ignores_info() -> None:
    """P7-S0-A11 差异以 JSON pointer 报告；``info`` 块不参与比较。"""

    base = _minimal({"tool_guide": "x"}, {"captures": 1})
    head = copy.deepcopy(base)
    head["info"] = {"git_head": "deadbeef"}
    assert snap.diff_snapshots(base, head) == []

    head["profiles"]["session_x"]["surface"]["tool_guide"] = "y"
    assert snap.diff_snapshots(base, head) == ['/profiles/session_x/surface/tool_guide: "x" -> "y"']


def test_cli_check_exit_codes(tmp_path: Path, baseline: dict[str, Any]) -> None:
    """P7-S0-A11 ``--check``：基线缺失 exit 2，有差异 exit 1，一致 exit 0。"""

    assert snap.main(["--check", str(tmp_path / "missing.json")]) == 2

    doctored = copy.deepcopy(baseline)
    doctored["profiles"]["runner_no_mcp"]["lifecycle"]["captures"] = 99
    doctored_path = tmp_path / "doctored.json"
    doctored_path.write_text(json.dumps(doctored, ensure_ascii=False), encoding="utf-8")
    assert snap.main(["--check", str(doctored_path)]) == 1

    assert snap.main(["--check", str(snap.DEFAULT_BASELINE)]) == 0


def test_http_mcp_profile_is_added_and_the_existing_ones_are_untouched(
    head: dict[str, Any], baseline: dict[str, Any]
) -> None:
    """P7-S8-A13：新增 ``session_http_mcp_fake``（经 fake Streamable HTTP server 暴露两个工具、一个被 schema 子集
    拒绝）；入仓基线里其余 16 个 profile 逐字不变；``WEB_PUBLIC_BASE_URL`` 在钉住的环境变量里。"""

    profile = head["profiles"]["session_http_mcp_fake"]
    assert profile["side"] == "session"
    view = profile["lifecycle"]["mcp"][0]
    assert view["exposed"] == ["mcp_snaphttp_echo_query", "mcp_snaphttp_lookup_project"]
    assert ["snaphttp", "nested_schema_tool", "MCP_UNSUPPORTED_SCHEMA"] in view["skipped_tools"]
    assert profile["lifecycle"]["exception"] is None
    assert profile["lifecycle"]["mcp_clients_alive_after_scope"] == 0, "会话收尾后 HTTP 会话已关闭"
    assert profile["surface"]["tools"]["mcp_snaphttp_echo_query"]["schema_chars"] > 0

    # S10 / S15 / S12 之后新增的 profile 不在"S8 时就存在的 16 个"里
    existing = {
        name
        for name in baseline["profiles"]
        if name not in {"session_http_mcp_fake", "session_endpoint_custom", "session_pick_model_off", "session_web_search_fake"}
    }
    assert len(existing) == 16
    for name in existing:
        assert head["profiles"][name] == baseline["profiles"][name], name
    assert "WEB_PUBLIC_BASE_URL" in snap.PINNED_ENV_KEYS


def test_endpoint_profile_is_added_and_the_existing_ones_are_untouched(
    head: dict[str, Any], baseline: dict[str, Any]
) -> None:
    """P7-S10-A10 新增 ``session_endpoint_custom``：非缺省 OpenAI 兼容对话端点 + 一份精确匹配的测试兼容报告，
    真生成器装配不抛错，chat model（``GovernedChatModel``）指向配置的端点与模型、兼容状态 verified；模型可见面
    与 mock 缺省 profile 相同（换端点不改工具与提示）；其余 17 个 profile 与基线逐字相同；新变量都被钉住。"""

    profile = head["profiles"]["session_endpoint_custom"]
    assert profile["side"] == "session"
    assert profile["lifecycle"]["exception"] is None
    assert profile["lifecycle"]["chat_model_endpoint"] == {
        "wrapper": "GovernedChatModel",
        "base_url": snap.CUSTOM_ENDPOINT_BASE_URL,
        "model": snap.CUSTOM_ENDPOINT_MODEL,
        "compatibility": "verified",
    }
    assert profile["surface"] == head["profiles"]["session_mock_default"]["surface"]
    others = {
        name
        for name in baseline["profiles"]
        if name not in {"session_endpoint_custom", "session_pick_model_off", "session_web_search_fake"}
    }
    assert len(others) == 17
    for name in others:
        assert head["profiles"][name] == baseline["profiles"][name], name
        assert "chat_model_endpoint" not in head["profiles"][name]["lifecycle"], name
    for key in (
        "WATER_AGENT_LLM_BASE_URL",
        "WATER_AGENT_LLM_MODEL",
        "WATER_AGENT_LLM_API_KEY",
        "WATER_AGENT_LLM_COMPATIBILITY_REPORT",
        "WATER_AGENT_LLM_TIMEOUT_SECONDS",
        "WATER_AGENT_LLM_PRICE",
        "WATER_AGENT_EMBEDDING_BASE_URL",
        "WATER_AGENT_EMBEDDING_MODEL",
        "WATER_AGENT_EMBEDDING_API_KEY",
        "WATER_AGENT_RETRIEVAL_MODE",
    ):
        assert key in snap.PINNED_ENV_KEYS, key


def test_pick_model_profile_is_added_and_the_existing_ones_are_untouched(
    head: dict[str, Any], baseline: dict[str, Any]
) -> None:
    """P7-S15-A09 新增 ``session_pick_model_off``：缺省端点上从模型目录选一个 ``thinking: off`` 的模型 + 它的逐对
    兼容性报告，真生成器装配不抛错，chat model 指向所选模型、带关闭思考的 ``extra_body``、兼容状态按逐对报告
    判为 verified（不是缺省免检）；模型可见面与 mock 缺省 profile 相同（选模型不改工具与提示）；其余 18 个
    profile 与基线逐字相同，也都没有 ``chat_model_endpoint``（除端点 profile 外）；没有新增环境变量。"""

    profile = head["profiles"]["session_pick_model_off"]
    assert profile["side"] == "session"
    assert profile["lifecycle"]["exception"] is None
    assert profile["lifecycle"]["chat_model_endpoint"] == {
        "wrapper": "GovernedChatModel",
        "base_url": "https://api.deepseek.com",
        "model": snap.PICKED_MODEL,
        "compatibility": "verified",
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert profile["surface"] == head["profiles"]["session_mock_default"]["surface"]
    others = {name for name in baseline["profiles"] if name not in {"session_pick_model_off", "session_web_search_fake"}}
    assert len(others) == 18
    for name in others:
        assert head["profiles"][name] == baseline["profiles"][name], name
        if name != "session_endpoint_custom":
            assert "chat_model_endpoint" not in head["profiles"][name]["lifecycle"], name

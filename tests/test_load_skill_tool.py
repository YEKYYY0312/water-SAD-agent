"""P7-S9：``load_skill`` 工具、能力、插件与暴露（``docs/design/16-p7-plugin-mcp.md`` §7 S9 A03 / A04）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from water_agent.plugins import (
    McpGatewayPlugin,
    SkillsContribution,
    assemble_tools,
    builtin_plugins,
)
from water_agent.plugins.base import AssemblyContext
from water_agent.plugins.exposure import EXPOSED_SCHEMA_BUDGET, bound_tool_definition, schema_chars
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.runtime.governance.contracts import (
    DEFAULT_LOCAL_CAPABILITIES,
    Capability,
    Principal,
)
from water_agent.runtime.governance.inventory import capability_for_tool, undeclared_tool_names
from water_agent.skills import SkillLibraryError, build_skill_library
from water_agent.skills.errors import SKILL_CATALOG_FULL, SKILL_TOOL_UNAVAILABLE
from water_agent.tools.load_skill import CATALOG_SCHEMA_BUDGET, build_load_skill_tool, fit_catalog
from water_agent.webapp.auth.dependencies import _MEMBER_CAPABILITIES

OPERATOR = Path(__file__).resolve().parent / "fixtures" / "skills" / "operator"
CORE = {"retrieve_clauses", "cite_clauses", "calc_velocity"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for key in ("WATER_AGENT_SKILLS", "WATER_AGENT_SKILLS_DIR", "WATER_AGENT_TOOL_EXPOSURE", "DRAWING_ROOT"):
        monkeypatch.delenv(key, raising=False)


def _skill(root: Path, name: str, *, description: str = "测试规程。", triggers: str = "[测试规程]") -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntriggers: {triggers}\n"
        "requires_tools: [retrieve_clauses]\n---\n1. 先检索相关条文。\n2. 结论经 cite_clauses 声明引用。\n",
        encoding="utf-8",
    )


def _assemble(drawing_root: str = "", **plugin_kwargs: object):
    return assemble_tools(
        AssemblyContext(retriever=TriviumRetriever(db_path=None, fallback_clauses=[]), drawing_root=drawing_root),
        base_principal=Principal.local("s9-tool"),
        trace=None,
        plugins=builtin_plugins(mcp=McpGatewayPlugin.borrowing(None), **plugin_kwargs),
    )


def test_load_skill_is_governed_and_locally_granted() -> None:
    """P7-S9-A03 ``load_skill`` 的 ToolSpec 在 ``tools/``、登记了能力 ``LOAD_SKILL``；能力在默认能力集与
    Web 成员能力集里（只读、不是危险能力）；参数是目录名的枚举，目录之外的名字过不了 schema。"""

    assert capability_for_tool("load_skill") is Capability.LOAD_SKILL
    assert undeclared_tool_names() == frozenset()
    assert Capability.LOAD_SKILL in DEFAULT_LOCAL_CAPABILITIES
    assert Capability.LOAD_SKILL in _MEMBER_CAPABILITIES

    library = build_skill_library(
        available_tools=CORE,
        environ={"WATER_AGENT_SKILLS": "on", "WATER_AGENT_SKILLS_DIR": str(OPERATOR)},
        builtin_dir=Path("/nonexistent-builtin"),
    )
    spec = build_load_skill_tool(library)
    assert spec is not None and spec.name == "load_skill"
    schema = spec.args_model.model_json_schema()
    assert sorted(schema["properties"]["skill"]["enum"]) == sorted(library.names)
    assert "$defs" not in schema, "Literal 内联枚举，不多一层 $defs"
    for name in library.names:
        assert f"- {name}：" in spec.description, "目录在工具描述里，每个 skill 一行"

    from water_agent.tools.registry import ToolRegistry

    registry = ToolRegistry(principal=Principal.local("s9-invoke"))
    registry.register(spec)
    ok = registry.invoke("load_skill", skill="pump-room-checklist")
    assert ok["ok"] is True and ok["source"] == "operator" and len(ok["digest"]) == 16
    assert "正文" in ok["message"] and "泵房布置" not in json.dumps(ok, ensure_ascii=False), "回执不含正文"
    bad = registry.invoke("load_skill", skill="unsafe-override")
    assert bad.get("error") is True, "目录之外（未过安检）的名字过不了 schema"


def test_switch_off_or_no_skills_means_no_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S9-A03 开关缺省 off：skills 插件 skipped、不注册 ``load_skill``、工具与 S7 相同；开关 on 但没有
    已启用 skill 时同样 skipped；开关取值非法 → 装配期失败；内置插件顺序末尾是 skills。"""

    off = _assemble()
    contribution = off.contribution(SkillsContribution)
    assert contribution.status == "skipped" and contribution.detail == "disabled"
    assert "load_skill" not in off.registry.list_all_names()
    assert [item.plugin_id for item in off.contributions][-1] == "skills"

    empty_builtin = tmp_path / "empty"
    empty_builtin.mkdir()
    monkeypatch.setenv("WATER_AGENT_SKILLS", "on")
    none_enabled = _assemble(skills_builtin_dir=empty_builtin)
    skipped = none_enabled.contribution(SkillsContribution)
    assert skipped.status == "skipped" and skipped.detail == "no_enabled_skills"
    assert "load_skill" not in none_enabled.registry.list_all_names()

    monkeypatch.setenv("WATER_AGENT_SKILLS", "maybe")
    with pytest.raises(SkillLibraryError):
        _assemble()


def test_requires_tools_follow_the_actual_assembly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S9-A03 ``requires_tools`` 按本次装配里先于 skills 的插件实际贡献的工具核对：没配图纸目录时
    要求 ``read_drawing`` 的 skill 不加载（``SKILL_TOOL_UNAVAILABLE``），配了就加载。"""

    from water_agent.drawing.synth import build_synthetic_drawing

    operator = tmp_path / "operator"
    directory = operator / "drawing-steps"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: drawing-steps\ndescription: 读图步骤。\nrequires_tools: [read_drawing]\n---\n先读图再总结。\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WATER_AGENT_SKILLS", "on")
    monkeypatch.setenv("WATER_AGENT_SKILLS_DIR", str(operator))
    without = _assemble().contribution(SkillsContribution)
    assert ("drawing-steps", SKILL_TOOL_UNAVAILABLE) in [(n, c) for n, c, _ in without.library.skipped]

    drawings = tmp_path / "drawings"
    drawings.mkdir()
    build_synthetic_drawing(drawings, drawing_id="P-01")
    with_drawing = _assemble(drawing_root=str(drawings)).contribution(SkillsContribution)
    assert "drawing-steps" in with_drawing.library.names


def test_catalog_fits_and_skills_group_yields_under_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S9-A04 目录 schema ≤ 900（超出按字母序从末尾截掉并记 SKILL_CATALOG_FULL）；skills 组是 intent：
    命中 triggers 或固定词才开放；"读图 + 材料"最紧组合下整组让位并记 dropped，发出的 schema ≤ 4500。"""

    from water_agent.drawing.synth import build_synthetic_drawing

    crowded = tmp_path / "crowded"
    for index in range(40):
        _skill(crowded, f"long-skill-{index:02d}", description="这是一段故意写得很长的规程描述，用来把目录撑满。" * 3)
    library = build_skill_library(
        available_tools=CORE,
        environ={"WATER_AGENT_SKILLS": "on", "WATER_AGENT_SKILLS_DIR": str(crowded)},
        builtin_dir=tmp_path / "none",
    )
    fitted = fit_catalog(library)
    assert 0 < len(fitted.names) < len(library.names)
    spec = build_load_skill_tool(fitted)
    assert spec is not None
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.registry import ToolRegistry

    registry = ToolRegistry(principal=Principal.local("s9-budget"))
    registry.register(spec)
    assert schema_chars([bound_tool_definition(tool_spec_to_base_tool(registry, "load_skill"))]) <= CATALOG_SCHEMA_BUDGET
    dropped = {name for name, code, _ in fitted.skipped if code == SKILL_CATALOG_FULL}
    assert dropped and all(name > max(fitted.names) for name in dropped), "按字母序从末尾截"

    operator = tmp_path / "operator"
    _skill(operator, "calc-steps", triggers="[计算书]")
    monkeypatch.setenv("WATER_AGENT_SKILLS", "on")
    monkeypatch.setenv("WATER_AGENT_SKILLS_DIR", str(operator))
    drawings = tmp_path / "drawings"
    drawings.mkdir()
    build_synthetic_drawing(drawings, drawing_id="P-01")
    assembly = _assemble(drawing_root=str(drawings))
    policy = assembly.exposure

    hit = policy.decide("帮我写一份给水流速的计算书")
    assert "load_skill" in hit.exposed and ("skills", "intent") in hit.reasons
    fixed = policy.decide("请按规程回答")
    assert "load_skill" in fixed.exposed
    miss = policy.decide("你好，你能做什么")
    assert "load_skill" not in miss.exposed and not any(group == "skills" for group, _ in miss.dropped)
    tight = policy.decide("按计算书流程统计 P-01 这张图的材料清单")
    assert {"read_drawing", "material_stats"} <= set(tight.exposed)
    assert "load_skill" not in tight.exposed and ("skills", "schema_budget") in tight.dropped
    assert tight.schema_chars <= EXPOSED_SCHEMA_BUDGET
    sticky = policy.decide("管径换成 65 再说一遍", previous_turn_tools=["load_skill"])
    assert "load_skill" in sticky.exposed and ("skills", "sticky") in sticky.reasons


# ---------------------------------------------------------------------------
# P7-S13：已安装层与目录预算（第三方来源不得挤掉仓库评审过的规程，也不得改变现有部署的目录）
# ---------------------------------------------------------------------------


def _installed_skill(name: str, *, description: str = "这是正好十五个字的首句描述。"):  # type: ignore[no-untyped-def]
    from water_agent.skills.models import Skill, SkillManifest

    return Skill(manifest=SkillManifest(name=name, description=description), body="x", source="installed", digest="0" * 16)


def _with_installed(library, names: list[str]):  # type: ignore[no-untyped-def]
    """现行顺序（内置 + 运维合并后的字母序）之后**追加**已安装层——与 ``build_skill_library`` 同一规则。"""

    from water_agent.skills.library import SkillLibrary

    extra = sorted((_installed_skill(name) for name in names), key=lambda skill: skill.name)
    return SkillLibrary(enabled=(*library.enabled, *extra), active=True)


def test_installed_skills_never_push_a_builtin_out_of_the_catalog() -> None:
    """P7-S13-A05 装满已安装 skill 时 4 个内置 skill 仍全部在目录里：``fit_catalog`` 先从末尾截已安装层（组内字母序从末尾起），
    已安装层截空之前不碰内置 + 运维。

    **构造要点**：``enabled`` 的**顺序**不能决定被截的是谁——如果 ``fit_catalog`` 只认"字母序最后一个"，那么只要把已安装 skill 按
    字母序混进内置里，名字排在前面的已安装 skill 就会把内置的 ``spec-conflict-handling`` 挤出目录（Step 0 实测）。所以这里**直接
    按字母序全排**构造库（不依赖"追加"约定），``fit_catalog`` 自己必须认 ``source``。"""

    from water_agent.skills.library import SkillLibrary

    base = build_skill_library(available_tools=CORE | {"material_stats"}, environ={"WATER_AGENT_SKILLS": "on"})
    assert len(base.names) == 4
    for prefix in ("aa", "zz"):
        for count in (1, 3, 4, 7, 20):
            mixed = sorted([*base.enabled, *(_installed_skill(f"{prefix}-skill-n{index:02d}") for index in range(count))],
                           key=lambda skill: skill.name)
            fitted = fit_catalog(SkillLibrary(enabled=tuple(mixed), active=True))
            assert set(base.names) <= set(fitted.names), (prefix, count, "内置被挤出目录")
            dropped = [name for name, code, _ in fitted.skipped if code == SKILL_CATALOG_FULL]
            assert all(name.startswith(prefix) for name in dropped), "只会丢已安装层"
            assert dropped == sorted(dropped, reverse=True), "组内按字母序从末尾起丢"
            assert build_load_skill_tool(fitted) is not None
    # 已安装层被截空之后才轮到内置 + 运维（沿用现行"字母序从末尾截"）
    from water_agent.skills.library import SkillLibrary

    crowded = SkillLibrary(
        enabled=tuple(_installed_skill(f"b-long-skill-{index:02d}", description="很长的规程描述。" * 12) for index in range(30)),
        active=True,
    )
    fitted = fit_catalog(crowded)
    dropped = {name for name, code, _ in fitted.skipped if code == SKILL_CATALOG_FULL}
    assert dropped and all(name > max(fitted.names) for name in dropped)


def test_without_installed_skills_the_catalog_and_truncation_are_unchanged(tmp_path: Path) -> None:
    """P7-S13-A05 无已安装 skill 时 ``fit_catalog`` 与现行**逐字相同**：内置 + 运维合并后的目录文本、截断结果（按字母序从末尾、记 ``SKILL_CATALOG_FULL``）
    都与 S9 一致——现行部署的目录与 ``skill_library_digest`` 不变。"""

    from water_agent.tools.load_skill import describe

    operator = tmp_path / "operator"
    for name in ("leak-test-notes", "pump-room-checklist"):
        _skill(operator, name)
    library = build_skill_library(
        available_tools=CORE | {"material_stats"},
        environ={"WATER_AGENT_SKILLS": "on", "WATER_AGENT_SKILLS_DIR": str(operator)},
    )
    assert library.names == (
        "calc-book-workflow", "drawing-review-report", "leak-test-notes", "missing-input-clarification",
        "pump-room-checklist", "spec-conflict-handling",
    ), "现行顺序：内置 + 运维合并后字母序（不是三段分组）"
    assert fit_catalog(library).names == library.names
    assert describe(fit_catalog(library).enabled) == describe(library.enabled)

    crowded = tmp_path / "crowded"
    for index in range(40):
        _skill(crowded, f"long-skill-{index:02d}", description="这是一段故意写得很长的规程描述，用来把目录撑满。" * 3)
    full = build_skill_library(
        available_tools=CORE,
        environ={"WATER_AGENT_SKILLS": "on", "WATER_AGENT_SKILLS_DIR": str(crowded)},
        builtin_dir=tmp_path / "none",
    )
    fitted = fit_catalog(full)
    dropped = sorted(name for name, code, _ in fitted.skipped if code == SKILL_CATALOG_FULL)
    assert dropped == sorted(set(full.names) - set(fitted.names)) and all(name > max(fitted.names) for name in dropped)


def test_builtin_and_operator_relative_order_is_independent_of_the_installed_layer(tmp_path: Path) -> None:
    """P7-S13-A05 已安装层存在与否，内置 + 运维的相对顺序不变（已安装层只追加在末尾）。"""

    operator = tmp_path / "operator"
    _skill(operator, "leak-test-notes")
    environ = {"WATER_AGENT_SKILLS": "on", "WATER_AGENT_SKILLS_DIR": str(operator)}
    without = build_skill_library(available_tools=CORE | {"material_stats"}, environ=environ)
    installed = tmp_path / "installed"
    (installed / "aaa-first").mkdir(parents=True)
    from water_agent.skills.installed import InstalledMeta, sha256_of, write_lock_atomic

    raw = "---\nname: aaa-first\ndescription: 排在最前的已安装规程。\n---\n先检索。\n".encode()
    (installed / "aaa-first" / "SKILL.md").write_bytes(raw)
    write_lock_atomic(installed, [InstalledMeta("aaa-first", "1.0.0", sha256_of(raw), len(raw), "r.example", "ops", "t", None)])
    with_layer = build_skill_library(
        available_tools=CORE | {"material_stats"}, environ={**environ, "WATER_AGENT_SKILLS_INSTALLED_DIR": str(installed)}
    )
    assert with_layer.names[:-1] == without.names and with_layer.names[-1] == "aaa-first"
    assert [skill.source for skill in with_layer.enabled][-1] == "installed"

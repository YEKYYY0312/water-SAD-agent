"""P7-S9：skill 的格式、加载与两级来源合并（``docs/design/16-p7-plugin-mcp.md`` §7 S9 A01）。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from water_agent.skills import SkillLibraryError, build_skill_library, resolve_skills_switch
from water_agent.skills.errors import (
    SKILL_DISABLED,
    SKILL_INVALID,
    SKILL_LIBRARY_INVALID,
    SKILL_SHADOWED,
)
from water_agent.skills.loader import parse_skill, scan_root
from water_agent.skills.models import MAX_BODY_CHARACTERS, MAX_FILE_BYTES, Skill

TOOLS = ("retrieve_clauses", "cite_clauses", "calc_velocity", "material_stats")
BODY = "1. 先用 retrieve_clauses 检索相关条文。\n2. 结论经 cite_clauses 声明引用。"


def _skill(root: Path, name: str, *, header: str | None = None, body: str = BODY) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    head = header if header is not None else (
        f"name: {name}\ndescription: 测试用规程。\nversion: 1.0.0\n"
        "triggers: [测试规程]\nrequires_tools: [retrieve_clauses]\n"
    )
    (directory / "SKILL.md").write_text(f"---\n{head}---\n{body}\n", encoding="utf-8")
    return directory


def _env(**values: str) -> dict[str, str]:
    return {"WATER_AGENT_SKILLS": "on", **values}


def test_skill_manifest_is_strict(tmp_path: Path) -> None:
    """P7-S9-A01 frontmatter 只认 name / description / version / triggers / requires_tools；未知键、坏 YAML、
    名字与目录不一致、非 kebab-case、正文为空或超长、文件超限、不是单层目录 → SKILL_INVALID。"""

    root = tmp_path / "skills"
    good = parse_skill(_skill(root, "good-one"), source="operator", root=root)
    assert isinstance(good, Skill)
    assert good.manifest.triggers == ("测试规程",)
    assert good.manifest.requires_tools == ("retrieve_clauses",)
    assert good.body == BODY and len(good.digest) == 16

    bad_headers = {
        "unknown-key": "name: unknown-key\ndescription: x\nallowed_tools: [shell]\n",
        "bad-yaml": "name: bad-yaml\ndescription: [unterminated\n",
        "not-mapping": "- just\n- a list\n",
        "name-mismatch": "name: other-name\ndescription: x\n",
        "bad-tool": "name: bad-tool\ndescription: x\nrequires_tools: ['rm -rf']\n",
        "long-description": "name: long-description\ndescription: " + "长" * 301 + "\n",
    }
    for name, header in bad_headers.items():
        result = parse_skill(_skill(root, name, header=header), source="operator", root=root)
        assert not isinstance(result, Skill), name
        assert result.code == SKILL_INVALID, name
        assert "shell" not in result.detail and "rm -rf" not in result.detail, "说明只列字段名，不回显值"

    assert parse_skill(_skill(root, "empty-body", body="   "), source="operator", root=root).code == SKILL_INVALID
    too_long = parse_skill(_skill(root, "too-long", body="字" * (MAX_BODY_CHARACTERS + 1)), source="operator", root=root)
    assert too_long.code == SKILL_INVALID

    huge = _skill(root, "huge-file")
    (huge / "SKILL.md").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    assert parse_skill(huge, source="operator", root=root).code == SKILL_INVALID

    no_front = root / "no-frontmatter"
    no_front.mkdir()
    (no_front / "SKILL.md").write_text("# 只有正文\n", encoding="utf-8")
    assert parse_skill(no_front, source="operator", root=root).code == SKILL_INVALID

    bad_dir = root / "Bad_Name"
    bad_dir.mkdir()
    assert parse_skill(bad_dir, source="operator", root=root).code == SKILL_INVALID

    # 不递归、不认平铺 .md、忽略隐藏目录与普通文件
    (root / "flat.md").write_text("---\nname: flat\ndescription: x\n---\n正文\n", encoding="utf-8")
    nested = root / "outer" / "inner"
    _skill(nested.parent, "inner")
    (root / ".hidden").mkdir()
    names = [getattr(entry, "name", None) for entry in scan_root(root, source="operator")]
    assert "flat" not in names and "inner" not in names and ".hidden" not in names
    assert "outer" in names, "outer 没有 SKILL.md：记为 SKILL_INVALID，不往下找"


def test_symlinks_cannot_escape_the_root(tmp_path: Path) -> None:
    """P7-S9-A01 指向根目录之外的符号链接不加载（运维目录里的链接不能把别处的文件变成系统策略）。"""

    outside = tmp_path / "outside"
    _skill(outside, "secret-plan")
    root = tmp_path / "skills"
    root.mkdir()
    (root / "secret-plan").symlink_to(outside / "secret-plan", target_is_directory=True)
    result = parse_skill(root / "secret-plan", source="operator", root=root)
    assert not isinstance(result, Skill) and result.code == SKILL_INVALID
    assert "符号链接" in result.detail


def test_two_sources_builtin_wins(tmp_path: Path) -> None:
    """P7-S9-A01 内置 + 运维两级合并：同名时内置优先（运维那份 SKILL_SHADOWED）；停用的记 SKILL_DISABLED；
    结果按名字排序；摘要只随已启用 skill 变化。"""

    builtin = tmp_path / "builtin"
    operator = tmp_path / "operator"
    _skill(builtin, "shared-name", body="内置正文：先检索再引用。")
    _skill(builtin, "builtin-only")
    _skill(operator, "shared-name", body="运维正文：不应生效。")
    _skill(operator, "operator-only")

    library = build_skill_library(
        available_tools=TOOLS,
        environ=_env(WATER_AGENT_SKILLS_DIR=str(operator)),
        builtin_dir=builtin,
    )
    assert library.active is True
    assert library.names == ("builtin-only", "operator-only", "shared-name")
    shared = library.get("shared-name")
    assert shared is not None and shared.source == "builtin" and "内置正文" in shared.body
    assert ("shared-name", SKILL_SHADOWED) in [(name, code) for name, code, _ in library.skipped]
    digest = library.digest()
    assert len(digest) == 16

    disabled = build_skill_library(
        available_tools=TOOLS,
        disabled=["operator-only"],
        environ=_env(WATER_AGENT_SKILLS_DIR=str(operator)),
        builtin_dir=builtin,
    )
    assert "operator-only" not in disabled.names
    assert ("operator-only", SKILL_DISABLED) in [(name, code) for name, code, _ in disabled.skipped]
    assert disabled.digest() != digest

    # 只有内置（没设运维目录）
    only_builtin = build_skill_library(available_tools=TOOLS, environ=_env(), builtin_dir=builtin)
    assert only_builtin.names == ("builtin-only", "shared-name")

    # 正文变了 → 摘要变了
    _skill(builtin, "builtin-only", body=BODY + "\n3. 多一行。")
    changed = build_skill_library(available_tools=TOOLS, environ=_env(), builtin_dir=builtin)
    assert changed.digest() != only_builtin.digest()

    summary = library.trace_summary()
    assert summary["enabled"] == [["builtin-only", "builtin"], ["operator-only", "operator"], ["shared-name", "builtin"]]
    assert str(operator) not in str(summary), "trace 摘要不含运维目录路径"
    assert "运维正文" not in str(summary) and "内置正文" not in str(summary), "trace 摘要不含正文"


def test_bad_skill_is_skipped_not_fatal(tmp_path: Path) -> None:
    """P7-S9-A01 单个坏 skill 只记错误码、其余照常；运维目录设了却不存在 / 不是目录、开关取值不对 → 装配期
    响亮失败；开关关闭时一个目录都不读（空库）。"""

    builtin = tmp_path / "builtin"
    operator = tmp_path / "operator"
    _skill(builtin, "fine-skill")
    _skill(operator, "broken-skill", header="name: broken-skill\n")  # 缺 description
    library = build_skill_library(
        available_tools=TOOLS,
        environ=_env(WATER_AGENT_SKILLS_DIR=str(operator)),
        builtin_dir=builtin,
    )
    assert library.names == ("fine-skill",)
    assert [(name, code) for name, code, _ in library.skipped] == [("broken-skill", SKILL_INVALID)]

    for missing in (tmp_path / "nope", tmp_path / "a-file"):
        if missing.name == "a-file":
            missing.write_text("x", encoding="utf-8")
        with pytest.raises(SkillLibraryError) as exc:
            build_skill_library(
                available_tools=TOOLS,
                environ=_env(WATER_AGENT_SKILLS_DIR=str(missing)),
                builtin_dir=builtin,
            )
        assert exc.value.code == SKILL_LIBRARY_INVALID
        assert str(missing) not in str(exc.value), "报错不回显运维目录路径"

    assert resolve_skills_switch({}) == "off"
    assert resolve_skills_switch({"WATER_AGENT_SKILLS": " ON "}) == "on"
    for bad in ("true", "1", "yes", "enabled"):
        with pytest.raises(SkillLibraryError):
            resolve_skills_switch({"WATER_AGENT_SKILLS": bad})

    # 开关关闭：连运维目录是否存在都不看
    off = build_skill_library(
        available_tools=TOOLS,
        environ={"WATER_AGENT_SKILLS_DIR": str(tmp_path / "nope")},
        builtin_dir=builtin,
    )
    assert off.active is False and off.names == () and off.digest() == ""

    # 缺省读 os.environ
    os.environ.pop("WATER_AGENT_SKILLS", None)
    assert build_skill_library(available_tools=TOOLS, builtin_dir=builtin).active is False

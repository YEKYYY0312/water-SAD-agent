"""P7-S9：首批内置 skill（``docs/design/16-p7-plugin-mcp.md`` §7 S9 A07）。

内置 skill 随包分发、以系统策略身份生效，所以入库就要满足和运维 skill 一样的安检，并额外满足铁律①②的
表述纪律：**规程只规定怎么做，不规定是多少**——正文里不得出现规范号、条款号或带工程单位的数值（这些只能
来自检索结果与计算工具）。
"""
from __future__ import annotations

import re
from pathlib import Path

from water_agent.skills import BUILTIN_SKILLS_DIR, build_skill_library
from water_agent.skills.loader import scan_root
from water_agent.skills.models import Skill
from water_agent.skills.safety import check_skill
from water_agent.tools.load_skill import CATALOG_SCHEMA_BUDGET, build_load_skill_tool, fit_catalog

EXPECTED = ("calc-book-workflow", "drawing-review-report", "missing-input-clarification", "spec-conflict-handling")
#: 默认装配（不配图纸目录、不接 MCP）里的工具。内置 skill 不能依赖可选工具，否则默认部署里就被跳过。
DEFAULT_TOOLS = ("retrieve_clauses", "cite_clauses", "calc_velocity", "material_stats")

#: 与 ``graphs/agent_rag._NORMATIVE_MARKERS`` 的规范号、条款号与带单位数值三条同口径（正文是规程，不是结论）。
_SPEC_NUMBER = re.compile(r"(?:GB|CJJ|CECS|JGJ|DBJ?|T/)\s*/?\s*T?\s*\d{3,5}", re.IGNORECASE)
_ARTICLE = re.compile(r"(?<![\d.])\d{1,2}\.\d{1,2}\.\d{1,3}(?![\d.])|第\s*\d+(?:\.\d+)*\s*条")
_UNIT_NUMBER = re.compile(
    r"\d(?:[\d.,]*)\s*(?:m/s|m³/h|m3/h|L/s|l/s|L/min|MPa|kPa|Pa\b|mm\b|cm\b|m\b|米|毫米|厘米|%|‰|℃|°C|kW|m³|m3|升|吨)",
    re.IGNORECASE,
)


def test_builtin_skills_pass_safety_and_carry_no_numbers() -> None:
    """P7-S9-A07 4 份内置 skill 全部解析成功、过安检、``requires_tools`` 都在默认装配里；正文与描述不含规范号、
    条款号与带单位数值；目录装得进 900 字符、一个都不被截掉；包里确实带了这 4 份（随包分发）。"""

    entries = scan_root(BUILTIN_SKILLS_DIR, source="builtin")
    assert [getattr(entry, "name", None) for entry in entries] == list(EXPECTED)
    for entry in entries:
        assert isinstance(entry, Skill), entry
        assert check_skill(entry, available_tools=DEFAULT_TOOLS) is None, entry.name
        assert set(entry.manifest.requires_tools) <= set(DEFAULT_TOOLS), entry.name
        assert entry.manifest.triggers, f"{entry.name} 没有 triggers：永远不会被 intent 暴露"
        text = f"{entry.manifest.description}\n{entry.body}"
        for pattern in (_SPEC_NUMBER, _ARTICLE, _UNIT_NUMBER):
            assert not pattern.search(text), (entry.name, pattern.pattern, pattern.findall(text)[:3])

    library = build_skill_library(available_tools=DEFAULT_TOOLS, environ={"WATER_AGENT_SKILLS": "on"})
    assert library.names == EXPECTED and library.skipped == ()
    fitted = fit_catalog(library)
    assert fitted.names == EXPECTED, f"目录超过 {CATALOG_SCHEMA_BUDGET} 字符：先精简描述，不得上调上限"
    assert build_load_skill_tool(fitted) is not None

    packaged = sorted(path.parent.name for path in Path(BUILTIN_SKILLS_DIR).glob("*/SKILL.md"))
    assert packaged == list(EXPECTED)
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"water_agent.skills" = ["builtin/*/SKILL.md"]' in pyproject, "内置 skill 必须随包分发"


def test_number_patterns_would_catch_a_regression() -> None:
    """P7-S9-A07 反证：上面三条规则确实能抓到规范号、条款号与带单位数值（否则"不含"是空断言）。"""

    for sample in ("依据 GB50015-2019", "按第 3.7.3 条", "流速不宜超过 2.0 m/s", "管径 DN50 即 50 mm"):
        assert any(pattern.search(sample) for pattern in (_SPEC_NUMBER, _ARTICLE, _UNIT_NUMBER)), sample

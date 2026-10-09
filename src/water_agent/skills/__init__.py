"""Skill 库（P7-S9）：两级来源（内置 + 运维只读目录）的 ``SKILL.md`` 操作规程。

模型经 ``load_skill`` 工具按需加载；加载后的正文以 ``SYSTEM_POLICY`` 分片进系统提示的 ``skill_context`` 段。
整个库由特性开关 ``WATER_AGENT_SKILLS``（缺省 ``off``）护住。规格见 ``docs/design/16-p7-plugin-mcp.md`` §3.9。
"""
from water_agent.skills.errors import SKILL_ERROR_CODES, SkillLibraryError
from water_agent.skills.library import (
    BUILTIN_SKILLS_DIR,
    SKILLS_DIR_ENV,
    SKILLS_SWITCH_ENV,
    SkillLibrary,
    build_skill_library,
    resolve_skills_switch,
)
from water_agent.skills.models import Skill, SkillManifest

__all__ = [
    "BUILTIN_SKILLS_DIR",
    "SKILLS_DIR_ENV",
    "SKILLS_SWITCH_ENV",
    "SKILL_ERROR_CODES",
    "Skill",
    "SkillLibrary",
    "SkillLibraryError",
    "SkillManifest",
    "build_skill_library",
    "resolve_skills_switch",
]

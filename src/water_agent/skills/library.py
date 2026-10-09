"""Skill 库：两级来源合并（P7-S9，``docs/design/16-p7-plugin-mcp.md`` §3.9）。

来源只有两级：

1. **内置**：随包分发的 ``water_agent/skills/builtin/<name>/SKILL.md``，经代码评审合入；
2. **运维**：``WATER_AGENT_SKILLS_DIR`` 指向的只读目录，运维对它负责。

**不扫描**项目目录、用户主目录、上传文件或 MCP 资源。同名时内置优先（运维那份记 ``SKILL_SHADOWED``）。
每份 skill 过 ``safety.check_skill``；管理员在 Web 管理层停用的记 ``SKILL_DISABLED``。整个库由特性开关
``WATER_AGENT_SKILLS``（缺省 ``off``）护住：关闭时不读任何目录。

库本身是纯数据：装配时建一次（会话 / runner），``load_skill`` 工具与提示装配都从同一个实例取正文。
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from water_agent.skills.errors import (
    SKILL_DISABLED,
    SKILL_LIBRARY_INVALID,
    SKILL_SHADOWED,
    SkillLibraryError,
)
from water_agent.skills.loader import SkillProblem, scan_root
from water_agent.skills.models import Skill
from water_agent.skills.safety import check_skill

#: 特性开关。``off``（缺省）：不读任何 skill 目录；``on``：读内置 + 运维目录。
SKILLS_SWITCH_ENV = "WATER_AGENT_SKILLS"
#: 运维 skill 目录（只读挂载）。未设 = 只有内置；设了却不存在 / 不是目录 = 部署错误。
SKILLS_DIR_ENV = "WATER_AGENT_SKILLS_DIR"
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "builtin"

SkillsSwitch = Literal["off", "on"]


def resolve_skills_switch(environ: Mapping[str, str]) -> SkillsSwitch:
    """读开关。空值即 ``off``；写错不静默当成关闭或打开。"""

    value = (environ.get(SKILLS_SWITCH_ENV) or "").strip().lower()
    if value in ("", "off"):
        return "off"
    if value == "on":
        return "on"
    raise SkillLibraryError(SKILL_LIBRARY_INVALID, f"{SKILLS_SWITCH_ENV} 只接受 off / on")


def operator_skills_dir(environ: Mapping[str, str]) -> Path | None:
    raw = (environ.get(SKILLS_DIR_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


@dataclass(frozen=True)
class SkillLibrary:
    """一次装配看到的 skill 库。``enabled`` 按名字排序；``skipped`` 是 (名字, 错误码, 说明)。"""

    enabled: tuple[Skill, ...] = ()
    skipped: tuple[tuple[str, str, str], ...] = ()
    active: bool = False
    _by_name: dict[str, Skill] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_name", {skill.name: skill for skill in self.enabled})

    def get(self, name: str) -> Skill | None:
        return self._by_name.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.enabled)

    def digest(self) -> str:
        """全部已启用 skill 的 name + digest 摘要（缓存键因子）；没有 skill 时为空串。"""

        if not self.enabled:
            return ""
        payload = [[skill.name, skill.source, skill.digest] for skill in self.enabled]
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def without(self, names: Collection[str], *, code: str, detail: str) -> SkillLibrary:
        """去掉这些 skill（记 ``code``）。用于目录装不下时按字母序截断。"""

        drop = set(names)
        return SkillLibrary(
            enabled=tuple(skill for skill in self.enabled if skill.name not in drop),
            skipped=self.skipped + tuple((name, code, detail) for name in sorted(drop)),
            active=self.active,
        )

    def trace_summary(self) -> dict[str, object]:
        """写进 trace 的摘要：只有名字、来源与错误码（不含正文、不含运维目录路径）。"""

        return {
            "enabled": [[skill.name, skill.source] for skill in self.enabled],
            "skipped": [[name, code] for name, code, _detail in self.skipped],
            "digest_nonempty": bool(self.enabled),
        }


def build_skill_library(
    *,
    available_tools: Collection[str],
    disabled: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
    builtin_dir: Path = BUILTIN_SKILLS_DIR,
) -> SkillLibrary:
    """按开关与两级来源建库。开关 off → 空库（``active=False``）。"""

    env = os.environ if environ is None else environ
    if resolve_skills_switch(env) == "off":
        return SkillLibrary()
    operator_dir = operator_skills_dir(env)
    if operator_dir is not None and not operator_dir.is_dir():
        raise SkillLibraryError(
            SKILL_LIBRARY_INVALID, f"{SKILLS_DIR_ENV} 指向的运维 skill 目录不存在或不是目录"
        )

    sources: list[tuple[str, Path]] = []
    if builtin_dir.is_dir():
        sources.append(("builtin", builtin_dir))
    if operator_dir is not None:
        sources.append(("operator", operator_dir))

    chosen: dict[str, Skill] = {}
    skipped: list[tuple[str, str, str]] = []
    disabled_names = set(disabled)
    for source, root in sources:
        try:
            entries = scan_root(root, source="builtin" if source == "builtin" else "operator")
        except OSError as exc:
            raise SkillLibraryError(
                SKILL_LIBRARY_INVALID, f"读不了 {source} skill 目录：{type(exc).__name__}"
            ) from exc
        for entry in entries:
            if isinstance(entry, SkillProblem):
                skipped.append((entry.name, entry.code, entry.detail))
                continue
            if entry.name in chosen:
                skipped.append((entry.name, SKILL_SHADOWED, "与内置 skill 同名，内置优先"))
                continue
            problem = check_skill(entry, available_tools=available_tools)
            if problem is not None:
                skipped.append((entry.name, *problem))
                continue
            if entry.name in disabled_names:
                skipped.append((entry.name, SKILL_DISABLED, "管理员在 Web 管理层停用"))
                continue
            chosen[entry.name] = entry
    return SkillLibrary(
        enabled=tuple(chosen[name] for name in sorted(chosen)),
        skipped=tuple(skipped),
        active=True,
    )


__all__ = [
    "BUILTIN_SKILLS_DIR",
    "SKILLS_DIR_ENV",
    "SKILLS_SWITCH_ENV",
    "SkillLibrary",
    "build_skill_library",
    "operator_skills_dir",
    "resolve_skills_switch",
]

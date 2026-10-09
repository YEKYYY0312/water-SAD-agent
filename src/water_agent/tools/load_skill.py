"""工具：load_skill（按名加载操作规程，P7-S9）。

模型看到的目录就在本工具的描述里（每个已启用 skill 一行 ``name：描述首句``），``skill`` 参数是这些名字的
枚举——模型表达不出目录之外的名字。加载成功只返回**回执**（名字、来源、版本、摘要），正文由提示装配器
按 thread 的 ``loaded_skills`` 放进系统提示的 ``skill_context`` 段（以 ``SYSTEM_POLICY`` 分片身份生效）；
正文不进工具结果，否则它会作为 ``TOOL_OUTPUT`` 数据留在历史里、每轮重复，且不再受停用 / 删除的约束。

目录装不下时（工具定义超过 :data:`CATALOG_SCHEMA_BUDGET`）按名字字母序截掉末尾的 skill，并记
``SKILL_CATALOG_FULL``——装配期决定，不在运行期静默截断。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from water_agent.skills.errors import SKILL_CATALOG_FULL, SKILL_NOT_FOUND
from water_agent.skills.library import SkillLibrary
from water_agent.skills.models import Skill
from water_agent.tools.base import ToolSpec

TOOL_NAME = "load_skill"
#: ``load_skill`` 的完整工具定义（``bound_tool_definition`` 同一序列化口径）上限：core 2125 + 900 ≤ 3025，
#: "core + 读图" + 900 ≤ 3981，都在 4500 内；"读图 + 材料"同时命中时 skills 组整组让位。
CATALOG_SCHEMA_BUDGET = 900

_HEAD = "按名加载一份操作规程（skill），加载后规程进入系统提示并在本会话后续轮次持续生效。"
_TAIL = (
    "Use when 用户点名某个规程，或问题明显符合下列某个规程的用途；先加载再动手。"
    "Do not use for 检索条文或计算数值（规程只规定怎么做，数值与条文仍走各自的工具）。"
)


def _first_sentence(text: str) -> str:
    line = text.splitlines()[0] if text else ""
    return line[: line.index("。") + 1] if "。" in line else line


def describe(skills: Sequence[Skill]) -> str:
    lines = "".join(f"\n- {skill.name}：{_first_sentence(skill.manifest.description)}" for skill in skills)
    return f"{_HEAD}{_TAIL}\n可用规程：{lines}"


def _args_model(names: Sequence[str]) -> type[BaseModel]:
    """``name`` 是目录里名字的 ``Literal`` 枚举：内联 ``enum``，比 ``Enum`` 少一层 ``$defs``，省目录预算。"""

    choice = Literal.__getitem__(tuple(names))  # 动态 Literal[...]

    class LoadSkillArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")

        #: 不叫 ``name``：``ToolRegistry.invoke(name, **args)`` 与 ToolNode 适配层都把 ``name`` 当工具名
        #: 位置参数，参数也叫 ``name`` 会让每次调用 ``TypeError``。
        skill: choice = Field(description="要加载的规程名（只能是目录里列出的名字）")  # type: ignore[valid-type]

    return LoadSkillArgs


def _definition_chars(description: str, args_model: type[BaseModel]) -> int:
    definition = {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": description,
            "parameters": args_model.model_json_schema(),
        },
    }
    return len(json.dumps([definition], ensure_ascii=False))


def fit_catalog(library: SkillLibrary) -> SkillLibrary:
    """目录装不下就按字母序从末尾截掉 skill（记 ``SKILL_CATALOG_FULL``），直到工具定义 ≤ 上限。"""

    current = library
    while current.enabled:
        names = current.names
        if _definition_chars(describe(current.enabled), _args_model(names)) <= CATALOG_SCHEMA_BUDGET:
            return current
        current = current.without(
            [names[-1]], code=SKILL_CATALOG_FULL, detail=f"目录超过 {CATALOG_SCHEMA_BUDGET} 字符上限"
        )
    return current


def build_load_skill_tool(
    library: SkillLibrary, *, on_loaded: Callable[[Skill], None] | None = None
) -> ToolSpec | None:
    """为已启用的 skill 建 ``load_skill``；没有 skill 时返回 ``None``（不暴露一个必然失败的工具）。

    ``library`` 须已经过 :func:`fit_catalog`。``on_loaded`` 在加载成功时回调（会话据此更新本轮的
    ``loaded_skills``）；正文不经回调传出。
    """

    if not library.enabled:
        return None
    args_model = _args_model(library.names)

    def _execute(args: Any) -> dict[str, Any]:
        skill = library.get(str(args.skill))
        if skill is None:  # 枚举已拦住目录之外的名字；这里兜底（例如库在两次调用之间被换掉）
            return {
                "error": True,
                "code": SKILL_NOT_FOUND,
                "message": "该规程不在本次目录里（不存在、未通过安检或已停用）",
                "suggestions": ["只加载工具描述里列出的规程名"],
            }
        if on_loaded is not None:
            on_loaded(skill)
        return {
            "ok": True,
            "name": skill.name,
            "source": skill.source,
            "version": skill.manifest.version,
            "digest": skill.digest,
            "message": f"已加载规程 {skill.name}，规程正文已进入系统提示，请按其步骤作答。",
        }

    return ToolSpec(
        name="load_skill",
        description=describe(library.enabled),
        args_model=args_model,
        execute=_execute,
        timeout_seconds=5.0,
    )


__all__ = ["CATALOG_SCHEMA_BUDGET", "TOOL_NAME", "build_load_skill_tool", "describe", "fit_catalog"]

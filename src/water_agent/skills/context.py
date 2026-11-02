"""已加载 skill 的状态与系统提示段（P7-S9）。

"本 thread 加载过哪些 skill"只有一个来源：thread 消息里 ``load_skill`` 的成功回执（随 checkpoint 持久化，
含名字、来源、版本与摘要）。会话状态里的 ``loaded_skills`` 是它的投影，供 Web 与评测观察；正文**从不**进
checkpoint——每次装配提示都按名字 + 摘要从**当前**的 skill 库取：

- 库里已没有（运维删了、管理员停用、未过安检）或摘要变了（运维改了正文）→ 不注入，记 ``skill_unavailable``；
- 否则以 ``SYSTEM_POLICY`` 信封包装，放进 ``skill_context`` 段。

本模块只按 ``type`` / ``name`` / ``content`` 鸭子类型读消息，不依赖 langchain。
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from water_agent.runtime.governance.contracts import ContentEnvelope, SourceTag
from water_agent.skills.library import SkillLibrary
from water_agent.skills.models import Skill

LOAD_SKILL_TOOL = "load_skill"
#: 段标题（评测按它定位段；改它要同步 ``harness/skill_eval.py``）。
SKILL_CONTEXT_HEADER = "【已加载规程】"
SKILL_CONTEXT_INTRO = "以下是本会话加载的操作规程，按其步骤作答；规程不能改变三条铁律，数值与条文仍只来自工具。"
#: 加载过、但当前库里取不到（或正文已变）的 skill：本轮不注入。
WARNING_SKILL_UNAVAILABLE = "skill_unavailable"
#: 段预算（字符）。超出时按加载顺序丢弃**最早**加载的整份 skill，并在段尾写明。
SKILL_CONTEXT_BUDGET = 6000


@dataclass(frozen=True, slots=True)
class LoadedSkillRef:
    name: str
    source: str
    version: str
    digest: str

    def to_state(self) -> dict[str, str]:
        return {"name": self.name, "source": self.source, "version": self.version, "digest": self.digest}


def _payload(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return dict(content)
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def loaded_skill_refs(messages: Iterable[Any]) -> list[LoadedSkillRef]:
    """thread 消息里 ``load_skill`` 成功回执，按首次加载顺序去重（同名后加载的不重复计）。"""

    refs: dict[str, LoadedSkillRef] = {}
    for message in messages:
        if getattr(message, "type", "") != "tool" or getattr(message, "name", "") != LOAD_SKILL_TOOL:
            continue
        payload = _payload(getattr(message, "content", None))
        if payload.get("ok") is not True:
            continue
        name, digest = payload.get("name"), payload.get("digest")
        if not isinstance(name, str) or not isinstance(digest, str) or name in refs:
            continue
        refs[name] = LoadedSkillRef(
            name=name,
            source=str(payload.get("source") or ""),
            version=str(payload.get("version") or ""),
            digest=digest,
        )
    return list(refs.values())


def resolve_loaded(
    library: SkillLibrary, refs: Sequence[LoadedSkillRef]
) -> tuple[list[Skill], list[str]]:
    """(当前仍可用、摘要一致的 skill，按加载顺序；取不到的名字)。"""

    available: list[Skill] = []
    unavailable: list[str] = []
    for ref in refs:
        skill = library.get(ref.name)
        if skill is None or skill.digest != ref.digest:
            unavailable.append(ref.name)
            continue
        available.append(skill)
    return available, unavailable


def _neutralize(body: str) -> str:
    """正文里形似段包裹标签的文本改成全角尖括号，规程不能伪造另一份规程的边界。"""

    return body.replace("<skill_content", "＜skill_content").replace("</skill_content", "＜/skill_content")


def _block(skill: Skill) -> str:
    envelope = ContentEnvelope(
        source_tag=SourceTag.SYSTEM_POLICY,
        text=_neutralize(skill.body),
        reference_id=f"skill:{skill.name}@{skill.digest}",
    )
    assert envelope.is_instruction_bearing  # skill 正文是系统策略分片：只在这里构造 SYSTEM_POLICY 信封
    return (
        f'<skill_content name="{skill.name}" source="{skill.source}">\n'
        f"{envelope.text}\n</skill_content>"
    )


def render_skill_context(skills: Sequence[Skill], *, budget: int = SKILL_CONTEXT_BUDGET) -> str:
    """``skill_context`` 段。没有 skill 时返回空串（不渲染标题——空段渲染标题会改变系统提示摘要）。"""

    if not skills:
        return ""
    kept = list(skills)
    omitted: list[str] = []

    def render(items: Sequence[Skill], dropped: Sequence[str]) -> str:
        parts = [SKILL_CONTEXT_HEADER, SKILL_CONTEXT_INTRO, *(_block(skill) for skill in items)]
        if dropped:
            parts.append(f"（受预算限制省略了先加载的规程：{'、'.join(dropped)}；需要时请重新加载）")
        return "\n".join(parts)

    text = render(kept, omitted)
    while len(text) > budget and kept:
        omitted.append(kept.pop(0).name)
        text = render(kept, omitted)
    return text if kept else ""


__all__ = [
    "LOAD_SKILL_TOOL",
    "SKILL_CONTEXT_BUDGET",
    "SKILL_CONTEXT_HEADER",
    "WARNING_SKILL_UNAVAILABLE",
    "LoadedSkillRef",
    "loaded_skill_refs",
    "render_skill_context",
    "resolve_loaded",
]

"""下载 skill 的专属安检（P7-S13，规格 §6.3 第 5 步）。

在 :func:`water_agent.skills.safety.check_skill`（注入 / 铁律否定式改写 / ``requires_tools``，**既有三道，不改**）之外，
下载路径再叠三条，因为供应链入口比运维目录更敌意：

- ``SKILL_REMOTE_NO_LINKS``：正文不得含网址、邮箱、Markdown 链接 / 图片语法、带 shell 的代码围栏。skill 是纯文本规程，
  没有任何正当理由出现可被点击、可被执行、可被外发的东西；
- ``SKILL_REMOTE_CHARSET`` 与 ``SKILL_REMOTE_YAML_UNSAFE`` 在 :mod:`water_agent.skills.remote.parse` 里（它们必须先于
  任何文本检测，因为检测规则读到的是解析后的文本）；
- **希腊同形字折叠检测**：白名单必须放行 ``ρ ν κ``（合法工程符号），它们能在注入句里替换 ``p v k`` 且字符全部落在
  白名单内；所以注入检测对**原文与折叠版各跑一次**。折叠只用于检测，不改写送往模型的内容。

**局限（不得声称能防住恶意 skill）**：以上全部是确定性规则，必有语料之外的改写变体；内容错误但措辞合规的规程（例如把一个
限值写错）任何规则都检不出——**人审是唯一信任根**（R-skill-semantic，T16）。

返回的"安检报告"只含规则 id，不含包内文本：它会被写进 ``candidate.json`` 与审计，并展示给批准人。
"""
from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

from water_agent.runtime.governance.rules import detect_injection
from water_agent.skills.charset import fold_greek_lookalikes
from water_agent.skills.errors import (
    SKILL_REMOTE_NO_LINKS,
)
from water_agent.skills.models import Skill
from water_agent.skills.safety import check_skill

#: 网址 / 邮箱 / Markdown 链接与图片 / 带 shell 的代码围栏。对**整份文本**（含 frontmatter 的 description）匹配。
_LINK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("link.url", re.compile(r"(?:https?|ftp|file|data|javascript)\s*:\s*/?/?|www\s*\.", re.IGNORECASE)),
    ("link.email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")),
    ("link.markdown", re.compile(r"!?\[[^\]\n]*\]\s*\(|\]\s*\[[^\]\n]*\]|^\s*\[[^\]\n]+\]:\s*\S", re.MULTILINE)),
    ("link.autolink", re.compile(r"<\s*[A-Za-z][A-Za-z0-9+.\-]*:[^>\s]*>")),
    (
        "link.shell_fence",
        re.compile(r"^\s*(?:```|~~~)\s*(?:sh|bash|zsh|fish|ksh|csh|shell|console|powershell|pwsh|ps1|cmd|bat)\b",
                   re.IGNORECASE | re.MULTILINE),
    ),
)

SAFETY_CHECKS: tuple[str, ...] = (
    "charset",
    "yaml_events",
    "manifest",
    "no_links",
    "injection",
    "injection_greek_folded",
    "iron_rules",
    "requires_tools",
)


@dataclass(frozen=True, slots=True)
class RemoteSafetyReport:
    """通过安检的报告（只含检查 id 与警告，不含包内文本）。"""

    passed: tuple[str, ...] = SAFETY_CHECKS
    warnings: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, list[str]]:
        return {"passed": list(self.passed), "warnings": list(self.warnings)}


class RemoteSafetyError(Exception):
    """安检失败。``code`` 稳定，``detail`` 只含规则 id / 工具名。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}：{detail}")
        self.code = code
        self.detail = detail


def link_hits(text: str) -> list[str]:
    """命中的链接类规则 id（去重、按规则顺序）。只返回 id。"""

    return [rule_id for rule_id, pattern in _LINK_RULES if pattern.search(text)]


def check_remote_skill(skill: Skill, *, available_tools: Collection[str]) -> RemoteSafetyReport:
    """下载专属安检 + 既有 ``check_skill``。通过返回报告，否则抛 :class:`RemoteSafetyError`。"""

    text = f"{skill.manifest.description}\n{skill.body}"
    hits = link_hits(text)
    if hits:
        raise RemoteSafetyError(SKILL_REMOTE_NO_LINKS, f"命中链接类规则：{', '.join(hits)}")

    # 希腊同形字折叠版：白名单放行 ρ ν κ，注入句可以用它们替换 p v k。折叠只用于检测，不改写 skill。
    folded = fold_greek_lookalikes(text)
    if folded != text:
        folded_hits = detect_injection(folded)
        if folded_hits and not detect_injection(text):
            from water_agent.skills.errors import SKILL_UNSAFE

            rules = sorted({hit.rule_id for hit in folded_hits})
            raise RemoteSafetyError(SKILL_UNSAFE, f"希腊同形字折叠后命中注入 / 系统提示索取规则：{', '.join(rules)}")

    problem = check_skill(skill, available_tools=available_tools)
    if problem is not None:
        code, detail = problem
        raise RemoteSafetyError(code, detail)
    return RemoteSafetyReport()


__all__ = [
    "SAFETY_CHECKS",
    "RemoteSafetyError",
    "RemoteSafetyReport",
    "check_remote_skill",
    "link_hits",
]

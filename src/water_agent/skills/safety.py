"""Skill 的装配期安检（P7-S9）。

skill 正文会以 ``SYSTEM_POLICY`` 分片进系统提示——它是**指令**，不是数据。所以运维目录里的 skill 在加载前
必须过三道检查（内置 skill 同样过，由测试保证入库即过）：

1. **注入 / 系统提示索取**：复用治理层的 ``detect_injection``（与 MCP 工具描述安检同一套规则），命中即
   ``SKILL_UNSAFE``，**不脱敏放行**；
2. **铁律否定式改写**：``SKILL_FORBIDDEN_DIRECTIVES``——要求不调计算工具、不声明引用、凭经验给数值、不检索
   就下结论这一类句式，命中即 ``SKILL_CONFLICTS_IRON_RULE``；
3. **依赖的工具都在**：``requires_tools`` 里有本次装配没有的工具即 ``SKILL_TOOL_UNAVAILABLE``——要求调用
   不存在工具的规程会诱导模型编造调用。

规则是**确定性正则**，必然存在未覆盖的改写变体：这里拦下的是明显越权的写法，不是"任意越权规程"。最终
防线仍是运行时的铁律机制（计算只经工具、引用经校验器）——规程管不到它们，``skill_p0`` 的 iron_rules 类
用例锁住这一点。
"""
from __future__ import annotations

import re
from collections.abc import Collection

from water_agent.runtime.governance.rules import detect_injection, normalize_for_detection
from water_agent.skills.errors import (
    SKILL_CONFLICTS_IRON_RULE,
    SKILL_TOOL_UNAVAILABLE,
    SKILL_UNSAFE,
)
from water_agent.skills.models import Skill

#: 否定铁律的句式（对规范化后的文本匹配：NFKC + 去不可见字符 + 折叠空白 + 小写）。
#: 每条都有正例与"正常规程不被误拦"的反例测试（``tests/test_skill_safety.py``）。否定词与被否定的动作
#: 之间只容 0–4 个字符：窗口一宽，"……不自行裁决""……不凭记忆给出数值"这类正向约束就会被读成越权。
_NEGATION = r"(?:不必|不用|无需|无须|不需要|可以不|可不|跳过|省略|免去)"
_NUMERIC = r"(?:流速|流量|管径|水头损失|扬程|压力|坡度|数值|限值)"
_GUESS = r"(?:凭经验|按经验|凭记忆|按记忆|心算|直接估算|自行估算|自行估计|估一个|拍脑袋)"
SKILL_FORBIDDEN_DIRECTIVES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "iron_rule.skip_calculation_tool",
        re.compile(
            _NEGATION + r".{0,4}(?:调用|使用|用|经过|走)?\s*(?:calc_velocity|计算工具|水力计算工具|程序化计算)"
        ),
    ),
    (
        # "流速直接凭经验估算" / "管径可以自行估计后填写" / "凭经验给出流速"——数值量与估算动作相邻，
        # 前面没有"不"（"不凭经验给出数值"是正向约束）
        "iron_rule.estimate_numbers",
        re.compile(
            _NUMERIC + r"[^。；\n]{0,6}?(?<!不)(?<!不要)(?:直接|可以|可)?" + _GUESS
            + r"|(?<!不)(?<!不要)(?<!禁止)" + _GUESS + r"[^。；\n]{0,6}?(?:给出|填写|算出|确定)?" + _NUMERIC
        ),
    ),
    (
        "iron_rule.skip_citation",
        re.compile(_NEGATION + r".{0,4}(?:引用|声明引用|cite_clauses|标注出处|注明条款|给出处|条文依据)"),
    ),
    (
        "iron_rule.skip_retrieval",
        re.compile(
            _NEGATION + r".{0,2}(?:检索|retrieve_clauses|查规范|查条文)"
            r"|(?<!不)(?:凭记忆|按记忆|根据记忆)[^。；\n]{0,6}(?:给出|回答|引用)[^。；\n]{0,4}(?:规范|条款|条文)"
        ),
    ),
    (
        "iron_rule.invent_spec_number",
        re.compile(r"(?<!不)(?<!不要)(?<!禁止)(?<!不得)(?:编造|虚构|自拟|随意填写).{0,6}(?:规范号|条款号|条文|标准号)"),
    ),
)


def iron_rule_conflicts(text: str) -> list[str]:
    """命中的铁律否定规则 id（去重、按规则顺序）。只返回 id，不返回命中文本。"""

    normalized = normalize_for_detection(text)
    return [rule_id for rule_id, pattern in SKILL_FORBIDDEN_DIRECTIVES if pattern.search(normalized)]


def check_skill(skill: Skill, *, available_tools: Collection[str]) -> tuple[str, str] | None:
    """安检一份 skill。通过返回 ``None``，否则返回 ``(错误码, 只含规则 id / 工具名的说明)``。"""

    text = f"{skill.manifest.description}\n{skill.body}"
    injection = detect_injection(text)
    if injection:
        rules = sorted({hit.rule_id for hit in injection})
        return SKILL_UNSAFE, f"命中注入 / 系统提示索取规则：{', '.join(rules)}"
    conflicts = iron_rule_conflicts(text)
    if conflicts:
        return SKILL_CONFLICTS_IRON_RULE, f"要求绕过铁律：{', '.join(conflicts)}"
    missing = [tool for tool in skill.manifest.requires_tools if tool not in set(available_tools)]
    if missing:
        return SKILL_TOOL_UNAVAILABLE, f"本次装配没有这些工具：{', '.join(missing)}"
    return None


__all__ = ["SKILL_FORBIDDEN_DIRECTIVES", "check_skill", "iron_rule_conflicts"]

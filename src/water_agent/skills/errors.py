"""Skill 库的稳定错误码（P7-S9）。

错误码是稳定契约：评测、面板与 trace 都按它判定，改名等同破坏兼容。与 MCP / 治理错误码分开命名空间。
单个 skill 的错误只让**这一个** skill 不加载，记录在 ``SkillLibrary.skipped`` 里；只有"运维目录配了却
读不了"这种部署错误才响亮失败（:class:`SkillLibraryError`）。
"""
from __future__ import annotations

#: frontmatter 坏 / 未知键 / 名字与目录不一致 / 超长 / 不是单层目录里的 SKILL.md。
SKILL_INVALID = "SKILL_INVALID"
#: 描述或正文命中注入 / 系统提示索取规则（``detect_injection``）。不脱敏放行。
SKILL_UNSAFE = "SKILL_UNSAFE"
#: 正文要求绕过三条铁律（不调计算工具、不声明引用、凭经验给数值……）。
SKILL_CONFLICTS_IRON_RULE = "SKILL_CONFLICTS_IRON_RULE"
#: ``requires_tools`` 里有当前装配不存在的工具。
SKILL_TOOL_UNAVAILABLE = "SKILL_TOOL_UNAVAILABLE"
#: 运维 skill 与内置 skill 同名，被内置那份遮蔽。
SKILL_SHADOWED = "SKILL_SHADOWED"
#: 目录装不下（工具描述 + 枚举超过上限），按名字字母序截掉的 skill。
SKILL_CATALOG_FULL = "SKILL_CATALOG_FULL"
#: 管理员在 Web 管理层停用了它。
SKILL_DISABLED = "SKILL_DISABLED"
#: 模型点名的 skill 不在本次目录里（不存在 / 被跳过 / 停用）。只出现在 ``load_skill`` 的结果里。
SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
#: 运维目录配了却不存在 / 不是目录 / 不可读（部署错误，装配期响亮失败）。
SKILL_LIBRARY_INVALID = "SKILL_LIBRARY_INVALID"

SKILL_ERROR_CODES: frozenset[str] = frozenset(
    {
        SKILL_INVALID,
        SKILL_UNSAFE,
        SKILL_CONFLICTS_IRON_RULE,
        SKILL_TOOL_UNAVAILABLE,
        SKILL_SHADOWED,
        SKILL_CATALOG_FULL,
        SKILL_DISABLED,
        SKILL_NOT_FOUND,
        SKILL_LIBRARY_INVALID,
    }
)


class SkillLibraryError(RuntimeError):
    """部署层面的 skill 配置错误（运维目录不可读、开关取值不对）。装配期上抛，不静默跳过。"""

    def __init__(self, code: str, message: str) -> None:
        if code not in SKILL_ERROR_CODES:
            raise ValueError(f"未知 skill 错误码 {code!r}")
        super().__init__(f"{code}：{message}")
        self.code = code
        self.message = message


__all__ = [
    "SKILL_CATALOG_FULL",
    "SKILL_CONFLICTS_IRON_RULE",
    "SKILL_DISABLED",
    "SKILL_ERROR_CODES",
    "SKILL_INVALID",
    "SKILL_LIBRARY_INVALID",
    "SKILL_NOT_FOUND",
    "SKILL_SHADOWED",
    "SKILL_TOOL_UNAVAILABLE",
    "SKILL_UNSAFE",
    "SkillLibraryError",
]

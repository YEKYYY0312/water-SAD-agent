"""Skill 库的稳定错误码（P7-S9；S13 新增下载 / 批准 / 第三来源层相关）。

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

# ---------------------------------------------------------------------------
# P7-S13：下载（运维 CLI）/ 批准 / 第三来源层。取回与批准只发生在主机 shell 上，这些码出现在 CLI 的输出、审计与
# 加载期的 ``skipped`` 里；模型工具与 Web 面板没有任何取 / 批准入口。
# ---------------------------------------------------------------------------

#: 注册表不可达 / 未配置 / 目的地被禁 / 超时 / 5xx / 鉴权 / 限流 / 连接失败。
SKILL_REGISTRY_UNAVAILABLE = "SKILL_REGISTRY_UNAVAILABLE"
#: 注册表给出了无法接受的响应：坏状态码（404 等）、媒体类型不对、JSON / 形状不合法、超限、重定向。
SKILL_REGISTRY_BAD_RESPONSE = "SKILL_REGISTRY_BAD_RESPONSE"
#: 注册表自相矛盾：index 回显的 name / version 与请求不一致，或文件的字节数 / sha256 与 index 声明不符。
SKILL_REGISTRY_MISMATCH = "SKILL_REGISTRY_MISMATCH"
#: index 里没有请求的 ``name@version``。
SKILL_NOT_IN_REGISTRY = "SKILL_NOT_IN_REGISTRY"
#: 已批准安装的版本，在 index 里被换了 sha256（同版本不同内容：重放 / 被篡改的注册表）。
SKILL_REGISTRY_REPLAY = "SKILL_REGISTRY_REPLAY"
#: 下载路径专属：正文或 frontmatter 里有字符白名单之外的字符（Tag 区、变体选择符、零宽、bidi、同形字……）。
SKILL_REMOTE_CHARSET = "SKILL_REMOTE_CHARSET"
#: 下载路径专属：frontmatter 含 anchor / alias / 显式 tag / 重复键 / 合并键 / 过深嵌套。
SKILL_REMOTE_YAML_UNSAFE = "SKILL_REMOTE_YAML_UNSAFE"
#: 下载路径专属：含网址 / 邮箱 / Markdown 链接或图片语法 / 带 shell 的代码围栏。
SKILL_REMOTE_NO_LINKS = "SKILL_REMOTE_NO_LINKS"
#: 与内置 / 运维目录的 skill 同名：``fetch`` 预检即拒（不发任何请求），``approve`` 再核一次。
SKILL_NAME_TAKEN = "SKILL_NAME_TAKEN"
#: ``approve`` / ``rollback`` / ``remove`` 要求交互终端；没有任何旁路。
SKILL_APPROVE_NO_TTY = "SKILL_APPROVE_NO_TTY"
#: 键入的 skill 名与待批准的不一致（含 EOF / 中断：没有明确确认就是没确认）。
SKILL_APPROVE_NAME_MISMATCH = "SKILL_APPROVE_NAME_MISMATCH"
#: ``--sha256`` 与隔离区文件 / ``candidate.json`` 的摘要不一致。
SKILL_APPROVE_DIGEST_MISMATCH = "SKILL_APPROVE_DIGEST_MISMATCH"
#: 隔离区里没有这个候选。
SKILL_CANDIDATE_NOT_FOUND = "SKILL_CANDIDATE_NOT_FOUND"
#: 批准 / 取回的版本必须严格大于已安装版本（降级与相等都被拒；回到旧版只有 ``rollback``）。
SKILL_VERSION_NOT_NEWER = "SKILL_VERSION_NOT_NEWER"
#: ``rollback`` 找不到可回到的上一版（没有 ``.previous/`` 或其摘要对不上）。
SKILL_ROLLBACK_UNAVAILABLE = "SKILL_ROLLBACK_UNAVAILABLE"
#: ``rollback`` / ``remove`` 的目标没有安装。
SKILL_NOT_INSTALLED = "SKILL_NOT_INSTALLED"
#: 加载期：已安装 skill 文件的字节摘要或大小与 ``skills.lock.json`` 不符（不加载、**不删除**）。
SKILL_TAMPERED = "SKILL_TAMPERED"
#: 加载期：安装区里有、lock 里没有（或 lock 里有、文件没了）——不加载。
SKILL_UNLOCKED = "SKILL_UNLOCKED"
#: CLI 的部署配置缺失 / 不合法（缺必需的环境变量、几个根目录互相包含、审计目录不可写、用法错误）。
SKILL_CONFIG_INVALID = "SKILL_CONFIG_INVALID"
#: CLI 内部未预期的异常（bug）：只给异常类型，不回显消息（消息里可能带路径或上游内容）。
SKILL_INTERNAL_ERROR = "SKILL_INTERNAL_ERROR"

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
        SKILL_REGISTRY_UNAVAILABLE,
        SKILL_REGISTRY_BAD_RESPONSE,
        SKILL_REGISTRY_MISMATCH,
        SKILL_NOT_IN_REGISTRY,
        SKILL_REGISTRY_REPLAY,
        SKILL_REMOTE_CHARSET,
        SKILL_REMOTE_YAML_UNSAFE,
        SKILL_REMOTE_NO_LINKS,
        SKILL_NAME_TAKEN,
        SKILL_APPROVE_NO_TTY,
        SKILL_APPROVE_NAME_MISMATCH,
        SKILL_APPROVE_DIGEST_MISMATCH,
        SKILL_CANDIDATE_NOT_FOUND,
        SKILL_VERSION_NOT_NEWER,
        SKILL_ROLLBACK_UNAVAILABLE,
        SKILL_NOT_INSTALLED,
        SKILL_TAMPERED,
        SKILL_UNLOCKED,
        SKILL_CONFIG_INVALID,
        SKILL_INTERNAL_ERROR,
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
    "SKILL_APPROVE_DIGEST_MISMATCH",
    "SKILL_APPROVE_NAME_MISMATCH",
    "SKILL_APPROVE_NO_TTY",
    "SKILL_CANDIDATE_NOT_FOUND",
    "SKILL_CATALOG_FULL",
    "SKILL_CONFIG_INVALID",
    "SKILL_CONFLICTS_IRON_RULE",
    "SKILL_DISABLED",
    "SKILL_ERROR_CODES",
    "SKILL_INTERNAL_ERROR",
    "SKILL_INVALID",
    "SKILL_LIBRARY_INVALID",
    "SKILL_NAME_TAKEN",
    "SKILL_NOT_FOUND",
    "SKILL_NOT_INSTALLED",
    "SKILL_NOT_IN_REGISTRY",
    "SKILL_REGISTRY_BAD_RESPONSE",
    "SKILL_REGISTRY_MISMATCH",
    "SKILL_REGISTRY_REPLAY",
    "SKILL_REGISTRY_UNAVAILABLE",
    "SKILL_REMOTE_CHARSET",
    "SKILL_REMOTE_NO_LINKS",
    "SKILL_REMOTE_YAML_UNSAFE",
    "SKILL_ROLLBACK_UNAVAILABLE",
    "SKILL_SHADOWED",
    "SKILL_TAMPERED",
    "SKILL_TOOL_UNAVAILABLE",
    "SKILL_UNLOCKED",
    "SKILL_UNSAFE",
    "SKILL_VERSION_NOT_NEWER",
    "SkillLibraryError",
]

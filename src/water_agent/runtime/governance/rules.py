"""P2.5 护栏规则集（Step 14）：可解释的确定性检查。

设计原则（规格 §6）：

1. **确定性、可解释**：每条规则有稳定 ``rule_id``、版本、正例与反例测试。
   默认实现**不含 ML 分类器**；外部 moderation 只能是可选 adapter（本步不实现）。
2. **不误伤工程问答**：规格 §2.2 明确「输出中的工程技术信息不因"敏感词"而被粗暴
   拦截。内容策略的重点是数据泄漏、提示注入、凭据、越权工具操作和明确危险执行指令」。
   因此**没有**"暴力/违法"等通用话题分类器 —— 给排水规范里"污水""排放""管道爆管"
   都是正常术语。每条规则都有"正常问答不被拦"的反例测试。
3. **规范化后再检测**：Base64、零宽字符、全角变体先规范化，否则检测形同虚设
   （`skill-guardrail-patterns` 的 "Missed encoded attacks" 反模式）。
4. **PII 类型限定为本项目确认的三类**：手机号、邮箱、身份证号。
   刻意**不做**姓名检测 —— 中文姓名与普通词汇无法用规则区分，误伤率不可接受。

本模块不导入 langchain/langgraph（治理层纯度约束）。
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

#: 规则集版本。**改动任何规则都必须改它**（进 trace 与 PolicyDecision，便于回溯裁定依据）。
#: P7-S1：工具面新增 ``tool.confirmation_required``（需人工确认的 MCP 工具执行期一律拒绝）。
#: P7-S7：工具面新增执行凭证的两条规则 ``tool.confirmation_mismatch`` / ``tool.confirmation_replayed``
#: （凭证是确认门在人工批准后签发的唯一放行路径）。
RULESET_VERSION = "p7-s7-2026-10-07"

# ---------------------------------------------------------------------------
# 输入上限（规格 §6：输入规范化与字节/token 上限）
# ---------------------------------------------------------------------------

#: 用户单条输入字节上限。给排水问题最长几百字；16KB（≈5000 汉字）足够宽松。
#: **刻意低于 50KB**：`skill-guardrail-patterns` 红队测试 #8 用 50000 字符探长度限制，
#: 上限设成 50KB 会让这条红队用例恰好穿过（有测试断言它被拦）。
#: 大批量结构化数据（管段表）走工具参数通道，受 `ToolPolicy.max_arg_bytes` 约束，不走此处。
MAX_USER_INPUT_BYTES = 16 * 1024
#: 检索/工具内容单条字节上限（条文最长约数千字）。
MAX_CONTENT_BYTES = 200 * 1024
#: 零宽与不可见字符（隐藏指令的常见载体）。
#: **必须写 \\uXXXX 转义而非字面字符** —— 字面不可见字符在编辑/复制中会静默丢失，
#: 让这条规则悄悄失效（ruff PLE2515/PLE2502 会因此报错）。
_INVISIBLE_CHARS = (
    "\u200b"  # ZERO WIDTH SPACE
    "\u200c"  # ZERO WIDTH NON-JOINER
    "\u200d"  # ZERO WIDTH JOINER
    "\u200e"  # LEFT-TO-RIGHT MARK
    "\u200f"  # RIGHT-TO-LEFT MARK
    "\u2060"  # WORD JOINER
    "\ufeff"  # ZERO WIDTH NO-BREAK SPACE (BOM)
    "\u00ad"  # SOFT HYPHEN
)
_INVISIBLE_RE = re.compile(f"[{_INVISIBLE_CHARS}]")


class RuleCategory(str, Enum):
    """规则类别。决定命中后的默认动作与 trace 归类。"""

    INPUT_LIMIT = "input_limit"
    INJECTION = "injection"
    PROMPT_LEAK = "prompt_leak"
    SECRET = "secret"
    PII = "pii"
    TOOL_POLICY = "tool_policy"
    DANGEROUS_EXECUTION = "dangerous_execution"


@dataclass(frozen=True, slots=True)
class RuleHit:
    """一条规则的命中结果。

    ``detail`` 只放**脱敏摘要**（命中类型 + 计数 + 位置），
    **绝不含原始命中文本**（规格 §6：trace 仅记录规则 ID、动作、脱敏摘要）。
    """

    rule_id: str
    category: RuleCategory
    detail: str
    #: 命中次数（用于摘要，不暴露内容）。
    count: int = 1


# ---------------------------------------------------------------------------
# 规范化（检测前必做）
# ---------------------------------------------------------------------------


def strip_invisible(text: str) -> tuple[str, int]:
    """移除零宽/不可见字符。返回 (清理后文本, 移除个数)。

    这些字符本身就是攻击信号：正常中文工程问答不会包含零宽连接符。
    """
    cleaned = _INVISIBLE_RE.sub("", text)
    return cleaned, len(text) - len(cleaned)


def normalize_for_detection(text: str) -> str:
    """检测用规范化：NFKC + 移除不可见字符 + 折叠空白 + 小写。

    **只用于检测**，不改变送往模型的实际内容 —— 规范化会丢失信息，
    但检测需要归一化形态（全角 `ｉｇｎｏｒｅ` 与 `ignore` 必须都被抓到）。
    """
    cleaned, _ = strip_invisible(unicodedata.normalize("NFKC", text))
    return " ".join(cleaned.split()).lower()


_B64_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def decode_base64_segments(text: str, *, max_segments: int = 8) -> list[str]:
    """尝试解码文本中的 Base64 片段，供二次检测。

    只解码长度 ≥16 且能解出可打印文本的片段，最多 ``max_segments`` 个
    （防止在超长输入上做无界解码）。解不出或不可打印的一律忽略。
    """
    decoded: list[str] = []
    for match in _B64_CANDIDATE_RE.finditer(text):
        if len(decoded) >= max_segments:
            break
        chunk = match.group(0)
        padded = chunk + "=" * (-len(chunk) % 4)
        try:
            raw = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            candidate = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if candidate.isprintable() or "\n" in candidate:
            decoded.append(candidate)
    return decoded


# ---------------------------------------------------------------------------
# 注入检测（规格 §6：直接注入 + 间接注入）
# ---------------------------------------------------------------------------

#: 指令覆盖型注入标记（规范化后匹配）。中英双语——本项目用户用中文提问。
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("injection.ignore_previous", re.compile(
        r"ignore\s+(?:all\s+)?(?:previous|prior|above|preceding)\s+"
        r"(?:instructions?|prompts?|rules?|directions?)"
    )),
    ("injection.ignore_previous_zh", re.compile(
        r"(?:忽略|无视|忘记|抛弃|不要理)(?:掉)?(?:以上|上述|之前|前面|先前|所有)"
        r".{0,6}(?:指令|指示|规则|要求|提示|设定)"
    )),
    ("injection.disregard", re.compile(r"disregard\s+(?:the\s+)?(?:above|previous|all)")),
    ("injection.new_instructions", re.compile(
        r"(?:new|updated|revised)\s+(?:system\s+)?(?:instructions?|prompt)\s*[:：]"
    )),
    ("injection.new_instructions_zh", re.compile(
        r"(?:新的|以下是新的|现在的)(?:系统)?(?:指令|提示|规则)\s*[:：]"
    )),
    ("injection.role_override", re.compile(
        r"you\s+are\s+now\s+(?:a|an|dan|no longer)|act\s+as\s+(?:if\s+)?(?:you|dan)\b"
    )),
    ("injection.role_override_zh", re.compile(
        r"(?:你现在是|从现在起你是|扮演)(?!给排水|一名给排水)"
        r".{0,12}(?:不受限制|没有限制|无限制|开发者模式)"
    )),
    ("injection.developer_mode", re.compile(r"developer\s+mode|dan\s+mode|jailbreak")),
    ("injection.fake_system_tag", re.compile(r"<\s*/?\s*(?:system|系统)\s*>|\[\s*system\s*\]")),
    ("injection.tool_directive", re.compile(
        r"(?:立即|马上|请)?(?:调用|执行|运行)\s*(?:工具|命令|shell|bash)"
        r"|execute\s+(?:the\s+)?(?:following|this)\s+(?:command|code|shell)"
    )),
)

#: 系统提示索取。
_PROMPT_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("prompt_leak.repeat_above", re.compile(
        r"repeat\s+(?:everything|all|the\s+text)\s+(?:above|before)"
        r"|print\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions?)"
        r"|(?:show|reveal|output)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+)?"
        r"(?:prompt|instructions?|rules?)"
    )),
    ("prompt_leak.repeat_above_zh", re.compile(
        r"(?:重复|复述|输出|显示|告诉我|打印)(?:一下|出)?"
        r"(?:你的|上面|以上|前面)?.{0,8}(?:系统提示|系统指令|提示词|prompt|原始指令)"
    )),
    # 中文动词可后置（"把上面的原始指令完整输出给我"），故再加一条**语序无关**的规则：
    # 指代词 + 泄漏目标 + 动词。少了它，只把动词前置的说法拦住等于形同虚设。
    ("prompt_leak.repeat_above_zh_postverb", re.compile(
        r"(?:你的|上面|以上|前面|之前)(?:的)?.{0,8}"
        r"(?:系统提示|系统指令|提示词|prompt|原始指令).{0,12}"
        r"(?:输出|复述|重复|打印|显示|告诉|发给|给我)"
    )),
    ("prompt_leak.verbatim", re.compile(r"verbatim.{0,20}(?:instructions?|prompt)")),
)


def detect_injection(text: str) -> list[RuleHit]:
    """检测直接/间接提示注入。

    对**原文规范化形态**与**Base64 解码结果**分别检测 ——
    只查明文会漏掉编码攻击（`skill-guardrail-patterns` 反模式之一）。
    """
    hits: list[RuleHit] = []
    normalized = normalize_for_detection(text)
    _, invisible_count = strip_invisible(text)

    if invisible_count > 0:
        hits.append(RuleHit(
            rule_id="injection.invisible_chars",
            category=RuleCategory.INJECTION,
            detail=f"含 {invisible_count} 个零宽/不可见字符",
            count=invisible_count,
        ))

    surfaces = [("plain", normalized)]
    for decoded in decode_base64_segments(text):
        surfaces.append(("base64", normalize_for_detection(decoded)))

    for surface_name, surface in surfaces:
        for rule_id, pattern in _INJECTION_PATTERNS:
            found = pattern.findall(surface)
            if found:
                hits.append(RuleHit(
                    rule_id=rule_id,
                    category=RuleCategory.INJECTION,
                    detail=f"在 {surface_name} 层命中注入标记 ×{len(found)}",
                    count=len(found),
                ))
        for rule_id, pattern in _PROMPT_LEAK_PATTERNS:
            found = pattern.findall(surface)
            if found:
                hits.append(RuleHit(
                    rule_id=rule_id,
                    category=RuleCategory.PROMPT_LEAK,
                    detail=f"在 {surface_name} 层命中系统提示索取 ×{len(found)}",
                    count=len(found),
                ))
    return hits


# ---------------------------------------------------------------------------
# 凭据检测（规格 §6：密钥常见格式）
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # OpenAI / DeepSeek 风格：sk- 后跟 ≥20 位
    ("secret.sk_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    # 阿里云百炼
    ("secret.dashscope", re.compile(r"\bsk-[a-f0-9]{32}\b")),
    ("secret.aliyun_ak", re.compile(r"\bLTAI[A-Za-z0-9]{12,}")),
    # AWS
    ("secret.aws_ak", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    # GitHub
    ("secret.github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    # 通用 Bearer / Authorization
    ("secret.bearer", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9_\-\.=]{20,}")),
    # 私钥块
    ("secret.private_key", re.compile(r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----")),
    # 形如 api_key=xxx 的赋值（值 ≥16 位，避免误伤 api_key=<your key>）
    ("secret.assignment", re.compile(
        r"(?:api[_\-]?key|secret[_\-]?key|access[_\-]?token|password)\s*[=:]\s*"
        r"['\"]?[A-Za-z0-9_\-]{16,}"
    , re.IGNORECASE)),
)


def detect_secrets(text: str) -> list[RuleHit]:
    """检测常见凭据格式。**不做规范化**（凭据是精确串，规范化会破坏形态）。"""
    hits: list[RuleHit] = []
    for rule_id, pattern in _SECRET_PATTERNS:
        found = pattern.findall(text)
        if found:
            hits.append(RuleHit(
                rule_id=rule_id,
                category=RuleCategory.SECRET,
                detail=f"命中凭据格式 ×{len(found)}",
                count=len(found),
            ))
    return hits


# ---------------------------------------------------------------------------
# PII 检测（本项目确认的三类）
# ---------------------------------------------------------------------------
#
# ⚠️ 给排水场景对 PII 规则有特殊要求：正文里**大量长数字**（管径、流量、坐标、
# 图号、编号）。开发时实测到 4 类误伤：
#   坐标 X=123456789012345678 / 流量 15000000000 L/d / 编号 202609061530001 /
#   管段 S13800138000
# 因此**纯长度正则不可用**，必须叠加结构校验：
#   - 身份证：GB 11643 校验位 + 出生日期合法性（这两项让随机长数字几乎不可能通过）；
#   - 手机号：边界排除字母前后缀 + 排除紧跟工程单位的数字。
# 每条都有正反例测试（见 tests/test_guardrails.py::TestPIINoFalsePositiveOnEngineering）。

#: 中国大陆手机号：1[3-9] + 9 位。边界同时排除数字**与字母**
#: （`S13800138000` 是管段编号，不是手机号）。
_PHONE_RE = re.compile(r"(?<![\dA-Za-z])1[3-9]\d{9}(?![\dA-Za-z])")

#: 紧跟数字的工程单位 —— 命中则该数字是量值，不是手机号/证件号。
_ENGINEERING_UNIT_RE = re.compile(
    r"\s*(?:l/s|l/d|l/min|m3/h|m³/h|m3/d|m³/d|mm|cm|km|kpa|mpa|pa|"
    r"m/s|m²|m2|m³|m3|dn|de|kw|kg|t/d|%|℃|°c|m\b|s\b|h\b|个|根|米|条|台|座)",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

#: 身份证候选：18 位（末位可为 X）。**15 位旧证不做检测** ——
#: 与工程编号无法区分（实测 `202609061530001` 误伤），宁可漏检旧证。
_ID_CARD_CANDIDATE_RE = re.compile(r"(?<![\dA-Za-z])(\d{17}[\dXx])(?![\dA-Za-z])")

#: GB 11643 校验位权重与映射表。
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"


def _valid_id_card_checksum(value: str) -> bool:
    """GB 11643 校验位验证。随机 18 位数字通过概率约 1/11。"""
    if len(value) != 18:
        return False
    try:
        total = sum(int(ch) * w for ch, w in zip(value[:17], _ID_WEIGHTS, strict=True))
    except ValueError:
        return False
    return value[17].upper() == _ID_CHECK_CHARS[total % 11]


def _valid_id_card_birthdate(value: str) -> bool:
    """出生日期（第 7–14 位 YYYYMMDD）合法性。年份限 1900–2100。"""
    from datetime import date

    try:
        year, month, day = int(value[6:10]), int(value[10:12]), int(value[12:14])
        if not (1900 <= year <= 2100):
            return False
        date(year, month, day)
    except ValueError:
        return False
    return True


def _find_id_cards(text: str) -> list[str]:
    """找出**通过校验位与日期校验**的身份证号。

    双重校验把随机长数字的误报率压到可忽略：校验位约 1/11，
    再叠加日期合法性（月 ≤12、日合法、年份区间）。
    """
    found: list[str] = []
    for match in _ID_CARD_CANDIDATE_RE.finditer(text):
        candidate = match.group(1)
        if _valid_id_card_checksum(candidate) and _valid_id_card_birthdate(candidate):
            found.append(candidate)
    return found


def _find_phones(text: str) -> list[str]:
    """找出手机号，排除紧跟工程单位的量值（如 ``15000000000 L/d``）。"""
    found: list[str] = []
    for match in _PHONE_RE.finditer(text):
        tail = text[match.end() : match.end() + 8]
        if _ENGINEERING_UNIT_RE.match(tail):
            continue  # 是流量/长度等量值，不是手机号
        found.append(match.group(0))
    return found


def detect_pii(text: str) -> list[RuleHit]:
    """检测 PII。

    **刻意不做中文姓名检测** —— 规则无法与普通词汇区分，误伤率不可接受。
    **刻意不做 15 位旧身份证检测** —— 与工程编号无法区分（实测误伤）。
    """
    hits: list[RuleHit] = []
    phones = _find_phones(text)
    if phones:
        hits.append(RuleHit(
            rule_id="pii.phone", category=RuleCategory.PII,
            detail=f"命中 PII ×{len(phones)}", count=len(phones),
        ))
    emails = _EMAIL_RE.findall(text)
    if emails:
        hits.append(RuleHit(
            rule_id="pii.email", category=RuleCategory.PII,
            detail=f"命中 PII ×{len(emails)}", count=len(emails),
        ))
    id_cards = _find_id_cards(text)
    if id_cards:
        hits.append(RuleHit(
            rule_id="pii.id_card", category=RuleCategory.PII,
            detail=f"命中 PII ×{len(id_cards)}", count=len(id_cards),
        ))
    return hits


def redact_text(text: str) -> tuple[str, list[RuleHit]]:
    """脱敏：把 PII 与凭据替换为占位符。返回 (脱敏后文本, 命中列表)。

    脱敏是**内容变换**，必须在送往云模型、落 trace、写缓存**之前**执行
    （规格 §2.2：默认 ``pii_mode=redact``）。只替换**通过校验的**命中项，
    因此工程数值不会被误改。
    """
    hits = detect_pii(text) + detect_secrets(text)
    redacted = text
    for phone in _find_phones(text):
        redacted = redacted.replace(phone, "[REDACTED:PHONE]")
    redacted = _EMAIL_RE.sub("[REDACTED:EMAIL]", redacted)
    for id_card in _find_id_cards(text):
        redacted = redacted.replace(id_card, "[REDACTED:ID_CARD]")
    for rule_id, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{rule_id.split('.')[-1].upper()}]", redacted)
    return redacted, hits


# ---------------------------------------------------------------------------
# 危险执行指令（输出侧；规格 §6：明确危险执行指令 fail closed）
# ---------------------------------------------------------------------------

#: 只匹配**可直接执行的破坏性指令**，不匹配技术讨论。
#: 反例保护：给排水问答里"删除管段""清空水池"必须放行。
_DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("danger.rm_rf", re.compile(r"rm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/")),
    ("danger.drop_table", re.compile(r"\bdrop\s+(?:table|database)\s+\w", re.IGNORECASE)),
    ("danger.truncate_table", re.compile(r"\btruncate\s+table\s+\w", re.IGNORECASE)),
    ("danger.delete_without_where", re.compile(
        r"\bdelete\s+from\s+\w+\s*(?:;|$)", re.IGNORECASE
    )),
    ("danger.shell_exec", re.compile(
        r"\b(?:os\.system|subprocess\.(?:run|call|Popen)|eval|exec)\s*\("
    )),
    ("danger.curl_pipe_shell", re.compile(r"curl\s+[^\n|]*\|\s*(?:ba)?sh")),
    ("danger.chmod_777", re.compile(r"chmod\s+(?:-R\s+)?777\s+/")),
)


def detect_dangerous_execution(text: str) -> list[RuleHit]:
    """检测可直接执行的破坏性指令（模型输出被当作命令的风险，LLM05）。"""
    hits: list[RuleHit] = []
    for rule_id, pattern in _DANGEROUS_PATTERNS:
        found = pattern.findall(text)
        if found:
            hits.append(RuleHit(
                rule_id=rule_id,
                category=RuleCategory.DANGEROUS_EXECUTION,
                detail=f"命中危险执行指令 ×{len(found)}",
                count=len(found),
            ))
    return hits


# ---------------------------------------------------------------------------
# 输入上限
# ---------------------------------------------------------------------------


def check_size(text: str, *, limit_bytes: int, label: str) -> list[RuleHit]:
    """字节上限检查（防 prompt stuffing / 资源耗尽）。"""
    size = len(text.encode("utf-8"))
    if size > limit_bytes:
        return [RuleHit(
            rule_id="input_limit.too_large",
            category=RuleCategory.INPUT_LIMIT,
            detail=f"{label} 大小 {size} 字节超过上限 {limit_bytes}",
        )]
    return []


__all__ = [
    "MAX_CONTENT_BYTES",
    "MAX_USER_INPUT_BYTES",
    "RULESET_VERSION",
    "RuleCategory",
    "RuleHit",
    "check_size",
    "decode_base64_segments",
    "detect_dangerous_execution",
    "detect_injection",
    "detect_pii",
    "detect_secrets",
    "normalize_for_detection",
    "redact_text",
    "strip_invisible",
]

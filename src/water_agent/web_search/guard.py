"""查询出口守卫 ``QueryEgressGuard``（P7-S12，规格 §5.4，威胁模型 T15）。

查询是**离开系统的用户数据**，也是间接注入的外泄通道（被投毒的文档诱导模型把私有内容塞进搜索词）。工具执行
前依次检查（任一不过 → ``WEB_SEARCH_QUERY_REJECTED``，**搜索服务零请求**）：

1. 形状：长度 ≤ 128、单行、无控制 / 零宽字符、不含 ``http(s)://`` 形态；
2. 内容：PII（手机号 / 邮箱 / 通过校验位的身份证号）、凭据形态、注入标记——与工具参数护栏**同一套规则**
   （``runtime/governance/rules.py``），这里再查一遍是因为护栏对 PII / 凭据在**工具参数面**并不拦（实测：手机号 /
   身份证 / ``sk-…`` 作为工具参数是 ``tool.allowed``），而发往第三方的查询必须拦；
3. 私有上下文回显：查询与**本轮**上传资料 / 长期记忆 / 检索到的条文 / 图纸审查结果存在 ≥ 16 字符的公共连续子串 →
   拒绝（被投毒文档诱导模型把私有内容塞进搜索词）。与**用户自己的消息**重合**不**拒绝——用户本人的问题就是该发的查询；
4. 每回合 ≤ 3 次（超出 → ``WEB_SEARCH_QUOTA``）。每主体速率与日额度另走 ``ProviderGateway`` 的画像。

**局限（必须如实）**：规则是确定性正则 / 子串，**必有未覆盖的变体**。改写、拆分、同义复述式的泄露拦不住；
"能拦住标注的子串 / 正则可识别的泄露"才是准确说法，**不得**说"能防住数据外泄"。

私有上下文由装配期注入的 ``private_context_source`` 提供（``AgenticSession`` 持有、``aask`` 入口登记），
**不经模型参数传入**——模型无法影响守卫看到什么。
"""
from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from water_agent.runtime.governance.rules import detect_injection, detect_pii, detect_secrets

#: 查询最大长度（与工具参数模型 ``query`` 的 ``max_length`` 一致）。
MAX_QUERY_CHARS = 128
#: 私有上下文回显的最小公共连续子串长度。
OVERLAP_MIN_CHARS = 16
#: 每回合最多发出的搜索次数。
MAX_SEARCHES_PER_TURN = 3

#: 稳定的拒绝原因码（审计 / 评测 / 前端文案用；**不含**查询原文）。
REASON_TOO_LONG = "too_long"
REASON_MULTILINE = "multiline"
REASON_CONTROL_CHARS = "control_chars"
REASON_URL_SHAPED = "url_shaped"
REASON_PII = "pii"
REASON_CREDENTIAL = "credential"
REASON_INJECTION = "injection"
REASON_PRIVATE_OVERLAP = "private_context_overlap"
REASON_TURN_LIMIT = "turn_limit"

PrivateContextSource = Callable[[], Sequence[str]]


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """守卫裁定。``reason`` 是稳定码，**不含**查询原文；``quota`` 为真表示"次数用尽"（映射 ``WEB_SEARCH_QUOTA``）。"""

    allowed: bool
    reason: str = ""
    quota: bool = False


ALLOW = GuardVerdict(allowed=True)


def _reject(reason: str, *, quota: bool = False) -> GuardVerdict:
    return GuardVerdict(allowed=False, reason=reason, quota=quota)


def _is_invisible_or_control(char: str) -> bool:
    category = unicodedata.category(char)
    # Cf（含零宽 / bidi 控制 / Tag 字符）与 Cc（控制）；换行与制表另行判断，不在这里报
    return category in ("Cf", "Cc") and char not in "\n\r\t"


def _normalize_for_overlap(text: str) -> str:
    """回显比对用的规范化：NFKC + 去不可见字符 + 折叠空白。只用于比对，不改变发出的查询。"""

    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(ch for ch in folded if not _is_invisible_or_control(ch))
    return "".join(folded.split())


def longest_common_run(a: str, b: str, *, at_least: int) -> int:
    """``a`` 与 ``b`` 的最长公共连续子串长度；只要找到 ≥ ``at_least`` 的就提前返回该长度。

    ``a`` 是查询（≤ 128 字符），``b`` 是私有上下文（可达数万字符）：对 ``a`` 的每个长为 ``at_least`` 的窗口用 ``in``
    判断，复杂度 ``O(len(a) · len(b))`` 的上界极小（``a`` 很短），且完全确定。
    """

    if at_least <= 0 or len(a) < at_least or len(b) < at_least:
        return 0
    for start in range(len(a) - at_least + 1):
        if a[start : start + at_least] in b:
            return at_least
    return 0


def _leaks(query: str, chunks: Sequence[str], user: str) -> bool:
    """查询里是否有一段长为 ``OVERLAP_MIN_CHARS`` 的窗口：出现在某个私有块里、且不在用户本人的消息里。"""

    width = OVERLAP_MIN_CHARS
    for start in range(len(query) - width + 1):
        window = query[start : start + width]
        if user and window in user:
            continue
        if any(window in chunk for chunk in chunks):
            return True
    return False


class QueryEgressGuard:
    """无状态的守卫。每回合计数由调用方传入（读 checkpoint 里本轮的 ``web_search`` 工具结果，与污点同源），
    不在进程内累积——否则重启 / 多 run 会让计数失真。"""

    def __init__(
        self,
        private_context_source: PrivateContextSource | None = None,
        *,
        user_text_source: Callable[[], str] | None = None,
    ) -> None:
        self._private_context_source = private_context_source
        self._user_text_source = user_text_source

    def check(self, query: str, *, searches_this_turn: int = 0) -> GuardVerdict:
        # 1. 形状
        if len(query) > MAX_QUERY_CHARS:
            return _reject(REASON_TOO_LONG)
        if "\n" in query or "\r" in query:
            return _reject(REASON_MULTILINE)
        if any(_is_invisible_or_control(ch) for ch in query):
            return _reject(REASON_CONTROL_CHARS)
        lowered = query.lower()
        if "http://" in lowered or "https://" in lowered or "ftp://" in lowered or "file://" in lowered:
            return _reject(REASON_URL_SHAPED)
        # 2. 内容（与工具参数护栏同一套规则，但这里 PII / 凭据一律拒绝：发往第三方的查询没有"授权 PII"一说）
        if detect_pii(query):
            return _reject(REASON_PII)
        if detect_secrets(query):
            return _reject(REASON_CREDENTIAL)
        if detect_injection(query):
            return _reject(REASON_INJECTION)
        # 3. 私有上下文回显：查询里任何一段长 ≥ 16 的连续文本出现在私有内容里、且**不**出现在用户本人的消息里 → 拒绝
        if self._private_context_source is not None:
            folded_query = _normalize_for_overlap(query)
            user = _normalize_for_overlap(self._user_text_source()) if self._user_text_source is not None else ""
            chunks = [_normalize_for_overlap(chunk) for chunk in self._private_context_source() if chunk]
            if chunks and _leaks(folded_query, chunks, user):
                return _reject(REASON_PRIVATE_OVERLAP)
        # 4. 每回合次数（放最后：被拒的查询不消耗次数的判断由调用方只对"放行"的计数）
        if searches_this_turn >= MAX_SEARCHES_PER_TURN:
            return _reject(REASON_TURN_LIMIT, quota=True)
        return ALLOW


__all__ = [
    "ALLOW",
    "MAX_QUERY_CHARS",
    "MAX_SEARCHES_PER_TURN",
    "OVERLAP_MIN_CHARS",
    "REASON_CONTROL_CHARS",
    "REASON_CREDENTIAL",
    "REASON_INJECTION",
    "REASON_MULTILINE",
    "REASON_PII",
    "REASON_PRIVATE_OVERLAP",
    "REASON_TOO_LONG",
    "REASON_TURN_LIMIT",
    "REASON_URL_SHAPED",
    "GuardVerdict",
    "PrivateContextSource",
    "QueryEgressGuard",
    "longest_common_run",
]

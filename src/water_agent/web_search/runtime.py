"""联网搜索的会话级运行态（P7-S12）：本轮私有上下文持有者 + 本回合搜索计数。

**为什么需要它**：``QueryEgressGuard`` 要在工具执行时知道"本轮有哪些私有内容"（上传资料 / 长期记忆 / 附件 / 本轮检索到的条文 /
本轮读到的图纸），以及"本回合已经搜索了几次"。这两件事都发生在**内层工具循环进行中**：此时 checkpoint 里还没有本回合的消息
（实测：工具执行期间读 checkpoint 只能看到上一次落盘的状态），所以不能只靠读 checkpoint——

- 召回结果（``memory_context`` / ``upload_context`` / ``attachment_context``）在 ``AgenticSession.aask`` 入口就已知，由
  :meth:`PrivateContext.begin_turn` 登记；
- 本轮检索 / 读图的工具输出由 LangChain 回调 :meth:`PrivateContext.on_tool_end` 在**输出刚产生时**捕获（回调随 ``config``
  继承进内层 react 子图，``AgenticSession`` 只在联网搜索被勾选时才把它加进 ``config``）；
- 回流 / 确认后恢复时，之前已落盘的本回合消息从 checkpoint 补读（:class:`WebSearchRuntime` 的 ``checkpoint_messages``）。

私有上下文**只由装配期注入**，不经模型参数传入：模型无法影响守卫看到什么。本模块不 import 网络原语。
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

#: 工具输出里哪些工具的结果算"私有内容"：检索到的条文（语料可能是机构内部标准）与读到的图纸审查结果。
PRIVATE_OUTPUT_TOOLS: frozenset[str] = frozenset({"retrieve_clauses", "read_drawing"})
#: 单个私有块的字符上限：只用于子串比对，更长的部分没有额外信息量，也避免无界占用。
MAX_CHUNK_CHARS = 60_000
#: 一轮最多保留多少个私有块（防止回调无界累积）。
MAX_CHUNKS = 64
WEB_SEARCH_TOOL_NAME = "web_search"


def _string_leaves(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _string_leaves(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _string_leaves(item, out)


def _text_of(value: Any) -> str:
    """召回结果 / 工具输出 → 可比对的文本。

    字符串原样；有 ``context_text`` 的召回结果取它；工具输出（``ToolMessage`` 正文是 JSON）取其中**全部字符串叶子**——
    JSON 序列化可能把中文写成 ``\\uXXXX`` 转义，直接拿 JSON 文本比对会让中文私有内容永远比不中。
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    text = getattr(value, "context_text", None)
    if isinstance(text, str):
        return text
    content = getattr(value, "content", None)  # ToolMessage
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return content
        leaves: list[str] = []
        _string_leaves(parsed, leaves)
        return "\n".join(leaves)
    return ""


class PrivateContext(BaseCallbackHandler):
    """本轮私有上下文。线程安全（回调可能来自工具线程）。``begin_turn`` 每个回合调用一次，清空上一回合的内容。"""

    raise_error = False

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._chunks: list[str] = []
        self._user_text = ""
        self._searches = 0

    # -- 回合边界 -------------------------------------------------------------------------------------

    def begin_turn(self, user_text: str, *contexts: Any) -> None:
        """新回合开始：清空，并登记这一回合用户本人的消息与召回结果（记忆 / 上传资料 / 附件）。``None`` 与空文本被忽略。

        用户本人的消息不算私有上下文：查询与它重合是正常的（用户的问题本来就该被搜索），守卫据此豁免。
        """

        with self._lock:
            self._chunks = []
            self._user_text = user_text or ""
            self._searches = 0
            for context in contexts:
                self._add_locked(_text_of(context))

    @property
    def user_text(self) -> str:
        with self._lock:
            return self._user_text

    def _add_locked(self, text: str) -> None:
        if text and len(self._chunks) < MAX_CHUNKS:
            self._chunks.append(text[:MAX_CHUNK_CHARS])

    def add(self, text: str) -> None:
        with self._lock:
            self._add_locked(text)

    def chunks(self) -> list[str]:
        with self._lock:
            return list(self._chunks)

    # -- 回调：捕获本轮检索 / 读图的输出 ------------------------------------------------------------------

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        name = kwargs.get("name") or getattr(output, "name", None)
        if name in PRIVATE_OUTPUT_TOOLS:
            self.add(_text_of(output))

    # -- 搜索计数 -------------------------------------------------------------------------------------

    @property
    def searches(self) -> int:
        with self._lock:
            return self._searches

    def count_attempt(self) -> None:
        with self._lock:
            self._searches += 1


@dataclass
class WebSearchRuntime:
    """一次会话里联网搜索工具要用到的"外部可见"依赖，全部惰性读取（会话装配完才有的东西）。

    ``request_context``：调用者主体的请求上下文（含装配后追加了 ``WEB_SEARCH`` 的主体）；``gateway``：本会话的
    ``ProviderGateway``（带 trace sink）；``recorder``：本次 run 的 trace 记录器；``checkpoint_messages``：本 thread 已落盘的
    消息（回流 / 恢复时补读本回合之前的内容）。缺省值让纯单元测试可以不带会话构造它。
    """

    private: PrivateContext = field(default_factory=PrivateContext)
    request_context: Callable[[], Any] | None = None
    gateway: Callable[[], Any] | None = None
    recorder: Callable[[], Any] | None = None
    checkpoint_messages: Callable[[], Sequence[Any]] | None = None

    # -- 守卫的两个输入 -------------------------------------------------------------------------------

    def user_text(self) -> str:
        """本回合用户本人的消息（守卫的豁免对象）。"""

        return self.private.user_text

    def private_chunks(self) -> list[str]:
        """回调捕获的 + checkpoint 里本回合已落盘的检索 / 读图输出。"""

        chunks = self.private.chunks()
        for message in self._turn_messages():
            if getattr(message, "type", "") == "tool" and getattr(message, "name", "") in PRIVATE_OUTPUT_TOOLS:
                text = _text_of(message)
                if text:
                    chunks.append(text[:MAX_CHUNK_CHARS])
        return chunks

    def searches_so_far(self) -> int:
        """本回合已经发起的搜索次数 = max(进程内计数, checkpoint 里本回合的 ``web_search`` 工具结果数)。

        两者互补：进程内计数覆盖"同一次内层循环里连续调用"（checkpoint 还没落盘），checkpoint 覆盖"回流 / 确认后恢复
        （新建了会话对象，进程内计数归零）"。取 max 而不是相加——同一次调用在两处都会被数到。
        """

        persisted = sum(
            1
            for message in self._turn_messages()
            if getattr(message, "type", "") == "tool" and getattr(message, "name", "") == WEB_SEARCH_TOOL_NAME
        )
        return max(self.private.searches, persisted)

    def _turn_messages(self) -> list[Any]:
        if self.checkpoint_messages is None:
            return []
        try:
            messages = list(self.checkpoint_messages())
        except Exception:  # noqa: BLE001 — 读不到 checkpoint 只影响补读，不影响搜索（进程内部分仍然生效）
            return []
        for index in range(len(messages) - 1, -1, -1):
            if getattr(messages[index], "type", None) == "human":
                return messages[index + 1 :]
        return messages


__all__ = [
    "MAX_CHUNKS",
    "MAX_CHUNK_CHARS",
    "PRIVATE_OUTPUT_TOOLS",
    "WEB_SEARCH_TOOL_NAME",
    "PrivateContext",
    "WebSearchRuntime",
]

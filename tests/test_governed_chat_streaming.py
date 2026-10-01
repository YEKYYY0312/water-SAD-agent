"""GovernedChatModel 流式路径测试：``_stream`` 经 ``ProviderGateway.stream_call``。

三组：
1. 假 BaseChatModel（离线）：逐块到达、tools 透传、不支持流式时 NotImplementedError 且不进网关、
   ``invoke`` 默认不流式（只有流式回调 / ``stream=True`` 才流式）、``bind_tools`` 保持治理、
   与上层 ``_BudgetChatModel._stream`` 的回退契约；
2. usage 提取：缺失返回 None 的变体；
3. 真实 ``ChatOpenAI`` 打本机假 OpenAI 兼容 SSE 服务（127.0.0.1，不出网）：验证
   ``stream_usage=True`` 真的发出 ``stream_options.include_usage``、最后一块的 usage 进账本、
   首块前 503/超时重试、401 零重试、中途断流不重试、调用方关闭会释放 HTTP 连接。
"""
from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableBinding, RunnableLambda
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import ConfigDict, Field, SecretStr

from water_agent.generation.chat_model import build_chat_model
from water_agent.generation.governed_chat import (
    GovernedChatModel,
    usage_from_langchain,
    usage_from_langchain_or_none,
)
from water_agent.graphs.agent_rag import _BudgetChatModel
from water_agent.runtime.governance.config import (
    BudgetConfig,
    CircuitBreakerPolicy,
    GovernanceConfig,
    ProviderProfile,
    RetryPolicy,
)
from water_agent.runtime.governance.contracts import (
    CODE_PROVIDER_AUTH_FAILED,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_USAGE_UNKNOWN,
    BudgetPolicy,
    GovernanceError,
    Principal,
    ProviderKind,
    RequestContext,
    UsageRecord,
)
from water_agent.runtime.governance.gateway import ProviderGateway

_TOOL_DEF = {
    "type": "function",
    "function": {"name": "calc_velocity", "description": "流速", "parameters": {"type": "object"}},
}
_USAGE = {
    "input_tokens": 12,
    "output_tokens": 5,
    "total_tokens": 17,
    "input_token_details": {"cache_read": 4},
}


class _StreamingFake(BaseChatModel):
    """实现了 ``_stream`` 的假模型：逐 token 产出，最后一块单独带 usage（同 OpenAI 兼容接口）。"""

    tokens: list[str] = Field(default_factory=lambda: ["你", "好", "世界"])
    with_usage: bool = True
    #: 产出到第几块后抛这个错（None = 不抛）。
    fail_after: int | None = None
    fail_with: BaseException | None = None
    generate_calls: list[dict[str, Any]] = Field(default_factory=list)
    stream_calls: list[dict[str, Any]] = Field(default_factory=list)
    produced: int = 0
    finished: bool = False
    finalized: bool = False

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "streaming-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.generate_calls.append(dict(kwargs))
        message = AIMessage(
            content="".join(self.tokens), usage_metadata=_USAGE if self.with_usage else None
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs: Any
                ) -> Iterator[ChatGenerationChunk]:
        self.stream_calls.append(dict(kwargs))
        try:
            for token in self.tokens:
                if self.fail_after is not None and self.produced >= self.fail_after:
                    assert self.fail_with is not None
                    raise self.fail_with
                self.produced += 1
                yield ChatGenerationChunk(message=AIMessageChunk(content=token))
            if self.with_usage:
                yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=_USAGE))
            self.finished = True
        finally:
            self.finalized = True

    def bind_tools(self, tools, **kwargs: Any):  # type: ignore[override]
        return self.bind(tools=list(tools), **kwargs)


class _StreamingHandler(BaseCallbackHandler):
    """满足 LangChain ``_StreamingCallbackHandler`` 协议的回调：挂上它 = 调用方要流式。"""

    def __init__(self) -> None:
        self.tokens: list[str] = []

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        self.tokens.append(token)

    def tap_output_iter(self, run_id: UUID, output: Iterator[Any]) -> Iterator[Any]:
        return output

    def tap_output_aiter(self, run_id: UUID, output: Any) -> Any:
        return output


def _ctx(thread: str = "t-stream") -> RequestContext:
    return RequestContext(principal=Principal.local(thread), request_id=f"req-{thread}",
                          thread_id=thread)


def _gateway(records: list[UsageRecord], *, timeout: float = 5.0, threshold: int = 100,
             slept: list[float] | None = None) -> ProviderGateway:
    config = GovernanceConfig(
        budget=BudgetConfig(policy=BudgetPolicy.BLOCK, daily_token_cap=1_000_000),
        profiles={
            ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                timeout_seconds=timeout,
                retry=RetryPolicy(max_attempts=3, base_backoff_seconds=0.0),
                breaker=CircuitBreakerPolicy(failure_threshold=threshold),
            )
        },
    )
    return ProviderGateway(
        config=config,
        on_usage=records.append,
        sleep=(slept.append if slept is not None else (lambda _d: None)),
    )


def _governed(inner: BaseChatModel, gateway: ProviderGateway) -> GovernedChatModel:
    return GovernedChatModel(
        inner=inner, model_name_hint="deepseek-chat", gateway=gateway, request_context=_ctx()
    )


def _reserved(gateway: ProviderGateway) -> int:
    return gateway.budget_ledger.snapshot(gateway.budget_key(_ctx()), gateway.config.budget
                                          ).reserved_tokens


class TestGovernedStream:
    def test_chunks_arrive_one_by_one_through_gateway(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records)
        fake = _StreamingFake()
        it = _governed(fake, gw)._stream([HumanMessage(content="hi")])
        for index, token in enumerate(fake.tokens):
            chunk = next(it)
            assert chunk.message.content == token
            assert fake.produced == index + 1  # 上游只多产出一块：真流式，没有攒齐再吐
        rest = list(it)
        assert [c.message.content for c in rest] == [""]  # 最后一块只带 usage
        assert gw.provider_call_count == 1
        assert [r.result_status for r in records] == ["ok"]
        rec = records[0]
        assert (rec.input_tokens, rec.output_tokens, rec.cached_input_tokens) == (12, 5, 4)
        assert _reserved(gw) == 0

    def test_public_stream_api_goes_through_gateway(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records)
        fake = _StreamingFake()
        chunks = list(_governed(fake, gw).stream("hi"))
        assert "".join(str(c.content) for c in chunks) == "你好世界"
        assert len(fake.stream_calls) == 1 and fake.generate_calls == []
        assert [r.result_status for r in records] == ["ok"]

    def test_inner_without_stream_raises_not_implemented_and_skips_gateway(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records)
        inner = FakeMessagesListChatModel(responses=[AIMessage(content="x")])
        with pytest.raises(NotImplementedError):
            next(_governed(inner, gw)._stream([HumanMessage(content="hi")]))
        assert gw.provider_call_count == 0
        assert records == []
        assert _reserved(gw) == 0

    def test_tools_kwargs_are_forwarded_to_inner_stream(self) -> None:
        gw = _gateway([])
        fake = _StreamingFake()
        list(_governed(fake, gw)._stream([HumanMessage(content="hi")], tools=[_TOOL_DEF],
                                         tool_choice="auto"))
        assert fake.stream_calls[0]["tools"] == [_TOOL_DEF]
        assert fake.stream_calls[0]["tool_choice"] == "auto"

    def test_caller_close_releases_reservation_and_closes_inner_stream(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records)
        fake = _StreamingFake()
        it = _governed(fake, gw)._stream([HumanMessage(content="hi")])
        next(it)
        it.close()
        assert fake.finalized is True and fake.finished is False
        assert [r.result_status for r in records] == [CODE_USAGE_UNKNOWN]
        assert _reserved(gw) == 0

    def test_error_after_first_chunk_surfaces_as_governance_error(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records)
        fake = _StreamingFake(fail_after=1, fail_with=ConnectionError("reset"))
        received: list[str] = []
        with pytest.raises(GovernanceError) as exc:
            for chunk in _governed(fake, gw)._stream([HumanMessage(content="hi")]):
                received.append(str(chunk.message.content))
        assert exc.value.code == CODE_PROVIDER_UNAVAILABLE
        assert received == ["你"]
        assert len(fake.stream_calls) == 1  # 不重试
        assert _reserved(gw) == 0


class TestStreamingDecision:
    """``invoke`` 默认不流式：只有挂了流式回调或显式 ``stream=True`` 才走 ``_stream``。"""

    def test_invoke_uses_generate_by_default(self) -> None:
        gw = _gateway([])
        fake = _StreamingFake()
        result = _governed(fake, gw).invoke("hi")
        assert result.content == "你好世界"
        assert len(fake.generate_calls) == 1 and fake.stream_calls == []
        assert gw.provider_call_count == 1

    def test_invoke_with_stream_true_streams(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records)
        fake = _StreamingFake()
        result = _governed(fake, gw).invoke("hi", stream=True)
        assert result.content == "你好世界"
        assert result.usage_metadata is not None and result.usage_metadata["input_tokens"] == 12
        assert len(fake.stream_calls) == 1 and fake.generate_calls == []
        assert [r.result_status for r in records] == ["ok"]

    def test_invoke_with_streaming_callback_streams_token_by_token(self) -> None:
        gw = _gateway([])
        fake = _StreamingFake()
        handler = _StreamingHandler()
        result = _governed(fake, gw).invoke("hi", config={"callbacks": [handler]})
        assert result.content == "你好世界"
        assert len(fake.stream_calls) == 1 and fake.generate_calls == []
        assert handler.tokens[:3] == ["你", "好", "世界"]

    def test_plain_callback_does_not_trigger_streaming(self) -> None:
        class _PlainHandler(BaseCallbackHandler):
            pass

        gw = _gateway([])
        fake = _StreamingFake()
        _governed(fake, gw).invoke("hi", config={"callbacks": [_PlainHandler()]})
        assert len(fake.generate_calls) == 1 and fake.stream_calls == []

    def test_non_streaming_inner_is_never_routed_to_stream(self) -> None:
        """包着不支持流式的模型时，流式回调 / stream=True / .stream() 都回落 _generate，不抛错。"""
        gw = _gateway([])
        inner = FakeMessagesListChatModel(responses=[AIMessage(content="答")] * 4)
        model = _governed(inner, gw)
        assert model.invoke("hi", stream=True).content == "答"
        assert model.invoke("hi", config={"callbacks": [_StreamingHandler()]}).content == "答"
        assert [c.content for c in model.stream("hi")] == ["答"]
        assert gw.provider_call_count == 3


class TestBindTools:
    def test_bind_tools_keeps_gateway_on_generate_and_stream(self) -> None:
        gw = _gateway([])
        fake = _StreamingFake()
        model = _governed(fake, gw)
        bound = model.bind_tools([_TOOL_DEF], tool_choice="auto")
        assert isinstance(bound, RunnableBinding) and bound.bound is model

        bound.invoke("hi")
        assert fake.generate_calls[0]["tools"] == [_TOOL_DEF]
        assert fake.generate_calls[0]["tool_choice"] == "auto"
        assert gw.provider_call_count == 1

        list(bound.stream("hi"))
        assert fake.stream_calls[0]["tools"] == [_TOOL_DEF]
        assert gw.provider_call_count == 2

    def test_bind_tools_with_chat_openai_no_longer_raises(self) -> None:
        """此前 GovernedChatModel(inner=<RunnableBinding>) 必然 ValidationError。"""

        @tool
        def add(a: int, b: int) -> int:
            """两数相加。"""
            return a + b

        inner = ChatOpenAI(model="deepseek-chat", base_url="http://127.0.0.1:9/v1",
                           api_key=SecretStr("sk-test"), max_retries=0)
        model = _governed(inner, _gateway([]))
        bound = model.bind_tools([add])
        assert bound.bound is model  # 治理包装仍在最外层
        assert bound.kwargs["tools"][0]["function"]["name"] == "add"

    def test_bind_tools_refuses_binding_it_cannot_keep_governed(self) -> None:
        class _OddBinder(_StreamingFake):
            def bind_tools(self, tools, **kwargs: Any):  # type: ignore[override]
                return RunnableLambda(lambda x: x)

        with pytest.raises(TypeError, match="无法在保持治理包装的前提下绑定工具"):
            _governed(_OddBinder(), _gateway([])).bind_tools([_TOOL_DEF])


class TestBudgetChatModelContract:
    """上层 ``_BudgetChatModel._stream`` 依赖的契约（只用不改 graphs/）。"""

    def test_falls_back_to_generate_when_inner_cannot_stream(self) -> None:
        gw = _gateway([])
        inner = FakeMessagesListChatModel(responses=[AIMessage(content="单块")])
        budget = _BudgetChatModel(inner=_governed(inner, gw), max_tool_turns=3,
                                  holder={"turns": 0})
        chunks = list(budget._stream([HumanMessage(content="hi")]))
        assert [c.message.content for c in chunks] == ["单块"]
        assert gw.provider_call_count == 1  # 只有回退的那一次 _generate 进了网关

    def test_streams_through_gateway_when_inner_can_stream(self) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records)
        fake = _StreamingFake()
        budget = _BudgetChatModel(inner=_governed(fake, gw), max_tool_turns=3,
                                  holder={"turns": 0})
        chunks = list(budget._stream([HumanMessage(content="hi")]))
        assert [c.message.content for c in chunks] == ["你", "好", "世界", ""]
        assert len(fake.stream_calls) == 1 and fake.generate_calls == []
        assert [r.result_status for r in records] == ["ok"]


class TestUsageExtraction:
    def test_missing_usage_is_none_not_unknown(self) -> None:
        assert usage_from_langchain_or_none(AIMessageChunk(content="x")) is None
        assert usage_from_langchain(AIMessageChunk(content="x")).known is False

    def test_usage_chunk_is_parsed(self) -> None:
        usage = usage_from_langchain_or_none(AIMessageChunk(content="", usage_metadata=_USAGE))
        assert usage is not None and usage.known is True
        assert (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) == (12, 5, 4)

    def test_malformed_usage_is_none(self) -> None:
        odd = SimpleNamespace(usage_metadata={"input_tokens": "12", "output_tokens": 5})
        assert usage_from_langchain_or_none(odd) is None


# ---------------------------------------------------------------------------
# 真实 ChatOpenAI × 本机假 OpenAI 兼容 SSE 服务
# ---------------------------------------------------------------------------


def _delta(content: str | None = None, *, role: str | None = None,
           finish_reason: str | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-chat",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


_USAGE_CHUNK = {
    "id": "chatcmpl-test",
    "object": "chat.completion.chunk",
    "created": 1,
    "model": "deepseek-chat",
    "choices": [],
    "usage": {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "prompt_tokens_details": {"cached_tokens": 4},
    },
}


class _FakeOpenAIServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeOpenAIHandler)
        self.behaviors: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.client_gone = threading.Event()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # 客户端主动断开等预期内的错误，不往测试输出里打 traceback


class _FakeOpenAIHandler(http.server.BaseHTTPRequestHandler):
    """按 server.behaviors 逐个请求决定响应。流式响应按 OpenAI 规范：

    ``include_usage`` 请求时非最后一块带 ``"usage": null``，最后多一块 ``choices: []`` 的
    usage 块；没请求就不发 usage——与真实接口一致，才能测出 stream_usage 是否生效。
    """

    protocol_version = "HTTP/1.1"
    server: _FakeOpenAIServer

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(body)
        behavior = self.server.behaviors.pop(0) if self.server.behaviors else "ok"
        try:
            self._respond(behavior, body)
        except (BrokenPipeError, ConnectionResetError):
            self.server.client_gone.set()
            self.close_connection = True

    def _respond(self, behavior: str, body: dict[str, Any]) -> None:
        kind, _, arg = behavior.partition(":")
        if kind == "status":
            code_text, _, retry_after = arg.partition(":")
            self._json_error(int(code_text), retry_after or None)
            return
        if kind == "sleep":
            time.sleep(float(arg))  # 客户端早已超时断开；随后的写入触发 BrokenPipe
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        events = [_delta("", role="assistant"), _delta("你好"), _delta("，世界"),
                  _delta(finish_reason="stop")]
        if include_usage:
            events = [{**event, "usage": None} for event in events] + [_USAGE_CHUNK]
        self._start_sse()
        if kind == "drop":
            for event in events[: int(arg)]:
                self._sse(event)
            self.close_connection = True  # 不发结束块就断开：incomplete chunked read
            return
        if kind == "trickle":
            # 慢慢吐块，直到写失败（= 客户端已关闭连接）或 3 秒兜底
            self._sse(events[0])
            self._sse(events[1])
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                time.sleep(0.05)
                self._sse(_delta("…"))
            self._end()
            return
        for event in events:
            self._sse(event)
        self._sse("[DONE]")
        self._end()

    def _json_error(self, status: int, retry_after: str | None) -> None:
        data = json.dumps({"error": {"message": f"fake {status}", "type": "fake_error"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if retry_after:
            self.send_header("Retry-After", retry_after)
        self.end_headers()
        self.wfile.write(data)

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse(self, event: dict[str, Any] | str) -> None:
        text = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
        data = f"data: {text}\n\n".encode()
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _end(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


@pytest.fixture
def openai_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeOpenAIServer]:
    # 本机回环不得被代理环境变量劫持（httpx 默认 trust_env）
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = _FakeOpenAIServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _real_model(server: _FakeOpenAIServer, gateway: ProviderGateway) -> BaseChatModel:
    return build_chat_model(base_url=server.base_url, api_key="sk-test", gateway=gateway,
                            request_context=_ctx())


class TestChatOpenAIOverLocalSSE:
    def test_stream_requests_include_usage_and_settles_real_usage(
        self, openai_server: _FakeOpenAIServer
    ) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records)
        chunks = list(_real_model(openai_server, gw)._stream([HumanMessage(content="你好")]))

        assert "".join(str(c.message.content) for c in chunks) == "你好，世界"
        request = openai_server.requests[0]
        assert request["stream"] is True
        assert request["stream_options"] == {"include_usage": True}
        assert [r.result_status for r in records] == ["ok"]
        rec = records[0]
        assert (rec.input_tokens, rec.output_tokens, rec.cached_input_tokens) == (12, 5, 4)
        assert rec.cost_usd is not None
        assert _reserved(gw) == 0

    def test_custom_base_url_without_stream_usage_gets_no_usage(
        self, openai_server: _FakeOpenAIServer
    ) -> None:
        """记录 langchain-openai 1.6.0 的默认行为：自定义 base_url 不自动开 stream_usage。

        这正是 build_chat_model 必须显式传 ``stream_usage=True`` 的原因：否则流式调用的
        usage 恒为 USAGE_UNKNOWN，预算按 0 结清。
        """
        records: list[UsageRecord] = []
        gw = _gateway(records)
        inner = ChatOpenAI(model="deepseek-chat", base_url=openai_server.base_url,
                           api_key=SecretStr("sk-test"), max_retries=0, timeout=5.0)
        assert inner.stream_usage is None
        list(_governed(inner, gw)._stream([HumanMessage(content="你好")]))
        assert "stream_options" not in openai_server.requests[0]
        assert [r.result_status for r in records] == [CODE_USAGE_UNKNOWN]

    def test_503_before_first_chunk_is_retried_with_retry_after(
        self, openai_server: _FakeOpenAIServer
    ) -> None:
        records: list[UsageRecord] = []
        slept: list[float] = []
        gw = _gateway(records, slept=slept)
        openai_server.behaviors = ["status:503:2", "ok"]
        chunks = list(_real_model(openai_server, gw)._stream([HumanMessage(content="你好")]))
        assert "".join(str(c.message.content) for c in chunks) == "你好，世界"
        assert len(openai_server.requests) == 2
        assert slept == [2.0]
        assert records[-1].result_status == "ok" and records[-1].retry_count == 1

    def test_read_timeout_before_first_chunk_is_retried(
        self, openai_server: _FakeOpenAIServer
    ) -> None:
        """langchain-openai 1.6 把超时重包装成 OpenAITimeoutError：须按瞬时错误重试。

        旧分类器只比对类名，会把它判成永久错误：1 次请求后直接 PROVIDER_UNAVAILABLE。
        （断言留余量：负载很高的机器上第二次请求也可能超时，那就再重试一次。）
        """
        records: list[UsageRecord] = []
        gw = _gateway(records, timeout=0.5)
        openai_server.behaviors = ["sleep:2.0", "ok"]
        chunks = list(_real_model(openai_server, gw)._stream([HumanMessage(content="你好")]))
        assert "".join(str(c.message.content) for c in chunks) == "你好，世界"
        assert len(openai_server.requests) >= 2
        assert records[-1].result_status == "ok" and records[-1].retry_count >= 1

    def test_401_is_not_retried_and_does_not_trip_breaker(
        self, openai_server: _FakeOpenAIServer
    ) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records, threshold=1)
        openai_server.behaviors = ["status:401"]
        model = _real_model(openai_server, gw)
        with pytest.raises(GovernanceError) as exc:
            list(model._stream([HumanMessage(content="你好")]))
        assert exc.value.code == CODE_PROVIDER_AUTH_FAILED
        assert len(openai_server.requests) == 1
        assert gw.breaker_open(model._provider_call([HumanMessage(content="你好")])) is False

    def test_connection_drop_mid_stream_is_not_retried(
        self, openai_server: _FakeOpenAIServer
    ) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records, threshold=1)
        openai_server.behaviors = ["drop:2"]
        model = _real_model(openai_server, gw)
        received: list[str] = []
        with pytest.raises(GovernanceError) as exc:
            for chunk in model._stream([HumanMessage(content="你好")]):
                received.append(str(chunk.message.content))
        assert exc.value.code == CODE_PROVIDER_UNAVAILABLE
        # openai 3.x 走 httpx2：原始传输异常不经 SDK 包装直接冒出（类名与 httpx 相同）
        cause = exc.value.__cause__
        assert type(cause).__name__ == "RemoteProtocolError"
        assert type(cause).__module__.startswith("httpx")
        assert "".join(received) == "你好"  # 断流前已交出的内容
        assert len(openai_server.requests) == 1  # 首块之后不重试
        assert [r.result_status for r in records] == [CODE_PROVIDER_UNAVAILABLE]
        # 中途断连属瞬时错误：计一次熔断失败（阈值 1 → 开路）
        assert gw.breaker_open(model._provider_call([HumanMessage(content="你好")])) is True
        assert _reserved(gw) == 0

    def test_caller_close_releases_the_http_stream(
        self, openai_server: _FakeOpenAIServer
    ) -> None:
        records: list[UsageRecord] = []
        gw = _gateway(records)
        openai_server.behaviors = ["trickle"]
        it = _real_model(openai_server, gw)._stream([HumanMessage(content="你好")])
        next(it)
        it.close()
        # 服务端继续写块会因连接已被客户端关闭而失败（而不是一直写到 3 秒兜底）
        assert openai_server.client_gone.wait(timeout=2.5)
        assert [r.result_status for r in records] == [CODE_USAGE_UNKNOWN]
        assert _reserved(gw) == 0

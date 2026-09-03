"""可选 OTel GenAI 观测（Step 10，guarded import）。

本模块遵循"不阻塞"原则：``opentelemetry`` SDK 未安装时，所有 API 均为 no-op，
导入与调用都不会报错，也不影响任何现有流程。安装后自动切换到真实 span 记录。

Span 结构（对齐 OTel GenAI 语义约定，见 skill-otel-genai.md）：
- ``invoke_agent``：每次 agent 运行（INTERNAL kind，agent name 来自 ``gen_ai.agent.name``）；
- ``chat``：每次 LLM 调用（``gen_ai.operation.name=chat``）；
- ``tool_call``：每次工具调用（``gen_ai.tool.name``）。

内容捕获默认关闭：span 上只记录输入/输出摘要引用，不内联完整 prompt/响应
（PII 与密钥泄漏风险，见 hard-reject）。
"""
from __future__ import annotations

import contextlib
from typing import Any

try:  # pragma: no cover — guarded import：未装 SDK 不报错
    from opentelemetry import trace as _otel_trace  # type: ignore[import-not-found]
    from opentelemetry.trace import Span as _OtelSpan  # type: ignore[import-not-found]

    _OTEL_AVAILABLE = True
except Exception:  # pragma: no cover  # noqa: BLE001 - SDK 缺失/损坏一律 no-op
    _otel_trace = None
    _OtelSpan = None
    _OTEL_AVAILABLE = False


def otel_available() -> bool:
    """OpenTelemetry SDK 是否可用。"""
    return _OTEL_AVAILABLE


def get_tracer(name: str = "water_agent") -> Any | None:
    """返回 tracer；SDK 未装时返回 None（调用方按 no-op 处理）。"""
    if not _OTEL_AVAILABLE or _otel_trace is None:
        return None
    return _otel_trace.get_tracer(name)


@contextlib.contextmanager
def start_span(
    name: str,
    *,
    kind: str = "internal",
    attributes: dict[str, Any] | None = None,
):
    """启动一个 OTel span；SDK 未装时为空操作 context manager。"""
    tracer = get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as span:  # type: ignore[union-attr]
        _set_kind(span, kind)
        if attributes:
            span.set_attributes(attributes)
        yield span


def _set_kind(span: Any, kind: str) -> None:
    """按 kind 字符串设置 span kind（INTERNAL/CLIENT）。"""
    if span is None or not _OTEL_AVAILABLE or _otel_trace is None:
        return
    with contextlib.suppress(Exception):
        kind_map = {
            "internal": _otel_trace.SpanKind.INTERNAL,
            "client": _otel_trace.SpanKind.CLIENT,
        }
        span_kind = kind_map.get(kind, _otel_trace.SpanKind.INTERNAL)
        span.set_attribute("gen_ai.call_kind", kind)
        _ = span_kind


def record_llm_call(*, model: str, provider: str = "deepseek", **kwargs: Any) -> None:
    """在 OTel（可用时）记录一次 chat span 的摘要属性。

    设计对齐 skill-otel-genai.md：只记摘要不内联内容（``*.reference_id`` 由调用方
    传入时记录；默认无内容字段）。SDK 未装时静默 no-op。
    """
    if not _OTEL_AVAILABLE:
        return
    with start_span(
        "chat",
        kind="client",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": model,
        },
    ) as span:
        if span is None:
            return
        for key, value in kwargs.items():
            if value is not None:
                with contextlib.suppress(Exception):
                    span.set_attribute(key, value)


def record_tool_call(*, tool_name: str, **attributes: Any) -> None:
    """在 OTel（可用时）记录一次 tool_call span 的摘要属性。"""
    if not _OTEL_AVAILABLE:
        return
    with start_span(
        "tool_call",
        kind="internal",
        attributes={
            "gen_ai.tool.name": tool_name,
        },
    ) as span:
        if span is None:
            return
        for key, value in attributes.items():
            if value is not None:
                with contextlib.suppress(Exception):
                    span.set_attribute(key, value)

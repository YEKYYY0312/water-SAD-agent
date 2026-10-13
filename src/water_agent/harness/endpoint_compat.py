"""OpenAI 兼容对话端点的兼容性探针（P7-S10，规格 P7-S10-A02）。

``python -m water_agent.harness.endpoint_compat --out <报告路径>``：读 ``resolve_chat_endpoint()``（与装配同一
来源），对那个端点 + 模型做七项检查，全部 ``passed`` 才写出 ``status=compatible`` 的报告；装配期与 Web 预检
据此放行非缺省端点（``generation/compatibility.py``）。

**会产生少量计费调用**（约五次对话请求：两次工具调用、一次完整流式、一次首块即取消的流式、一次故意写错
模型名的请求）。没有 Key 即结构化 SKIP（不发请求、不写报告）；有 Key 但检查没全过照样写报告（如实记
``incomplete`` / ``blocked``）、退出码非零。

检查项：

- ``function_calling`` / ``structured_citations`` / ``programmatic_calculation`` / ``tool_schema_budget``：与
  P5 Step 46 本地模型探针同一实现（``local_model_compat.model_facing_checks``，真实调用）；
- ``streaming_usage``：流式末块带 usage——没有它，流式调用的预算只能按 unknown 结清；
- ``error_handling``：故意写错模型名，必须在时限内得到结构化失败（``GovernanceError``，不是超时挂起，也
  不是被服务商悄悄路由到别的模型而"成功"）；
- ``cancellation_timeout``：发一次流式请求、首块后关闭，连接必须在时限内关闭。

``memory_p0`` 是离线套件、与端点无关，不在远程端点的检查集里（本地模型路径照旧保留它）。报告不写 Key、
不写请求原文与响应正文，只写检查结果、端点、模型、探针版本与时间。

退出码：0 全部通过或 SKIP；1 有检查未通过（报告照写）；2 参数或配置错误（含本地模型路径——请用
``harness.local_model_compat``）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from water_agent.generation.compatibility import (
    ENDPOINT_COMPATIBILITY_CHECKS,
    ENDPOINT_PROBE_VERSION,
    CheckStatus,
    CompatibilityReport,
    write_report,
)
from water_agent.generation.endpoints import (
    SOURCE_LOCAL_MODEL,
    ChatEndpoint,
    EndpointConfigError,
    resolve_chat_endpoint,
)

PROBE_VERSION = ENDPOINT_PROBE_VERSION
#: ``error_handling`` 故意使用的模型名（不应存在于任何服务商）。
ERROR_PROBE_MODEL = "water-agent-compat-probe-missing-model"
#: 写错模型名的请求必须在这段时间内得到结构化失败（同时是该次请求的超时）。
ERROR_DEADLINE_SECONDS = 30.0
#: 首块之后关闭流式请求，关闭本身必须在这段时间内完成。
CANCEL_DEADLINE_SECONDS = 10.0
STREAM_PROMPT = "只回答一个阿拉伯数字：1 加 1 等于几？"
CANCEL_PROMPT = "请逐条、详细地列出建筑给水排水设计中需要注意的二十个要点，每条不少于五十字。"


def _probe_gateway(timeout_seconds: float) -> Any:
    """探针专用 gateway：不重试（失败如实记录，不放大调用量），超时由调用方给定。"""

    from water_agent.runtime.governance.binding import (
        default_gateway,
        gateway_for_profile_overrides,
    )
    from water_agent.runtime.governance.config import ProviderProfile, RetryPolicy
    from water_agent.runtime.governance.contracts import ProviderKind

    base = default_gateway().timeout_for(ProviderKind.DEEPSEEK_CHAT)
    return gateway_for_profile_overrides(
        {
            ProviderKind.DEEPSEEK_CHAT: ProviderProfile(
                timeout_seconds=min(base, timeout_seconds) if timeout_seconds else base,
                retry=RetryPolicy(max_attempts=1),
            )
        }
    )


def _model(
    endpoint: ChatEndpoint, api_key: str, *, model: str | None = None, timeout: float = 0.0
) -> Any:
    from water_agent.generation.chat_model import build_chat_model
    from water_agent.runtime.governance.binding import local_request_context

    return build_chat_model(
        model=model or endpoint.model,
        base_url=endpoint.base_url,
        api_key=api_key,
        gateway=_probe_gateway(timeout),
        request_context=local_request_context(thread_id="endpoint-compat"),
    )


def _failure_label(exc: BaseException) -> str:
    """只记异常类型与稳定错误码，不记消息正文（可能回显请求内容）。"""

    code = getattr(exc, "code", None)
    return f"{type(exc).__name__}:{code}" if code else type(exc).__name__


def check_streaming_usage(model: Any) -> tuple[CheckStatus, dict[str, Any]]:
    total: Any = None
    for chunk in model.stream(STREAM_PROMPT):
        total = chunk if total is None else total + chunk
    usage = getattr(total, "usage_metadata", None) or {}
    reported = isinstance(usage.get("input_tokens"), int) and isinstance(
        usage.get("output_tokens"), int
    )
    return ("passed" if reported else "failed"), {"stream_usage_reported": reported}


def check_error_handling(
    endpoint: ChatEndpoint, api_key: str
) -> tuple[CheckStatus, dict[str, Any]]:
    from water_agent.runtime.governance.contracts import CODE_PROVIDER_TIMEOUT, GovernanceError

    bogus = _model(endpoint, api_key, model=ERROR_PROBE_MODEL, timeout=ERROR_DEADLINE_SECONDS)
    started = time.monotonic()
    try:
        bogus.invoke("ping")
    except GovernanceError as exc:
        elapsed = time.monotonic() - started
        structured = exc.code != CODE_PROVIDER_TIMEOUT and elapsed <= ERROR_DEADLINE_SECONDS
        return ("passed" if structured else "failed"), {
            "error_code": exc.code,
            "error_seconds": round(elapsed, 3),
        }
    except Exception as exc:  # noqa: BLE001 — 非结构化失败正是要记下的事实
        return "failed", {"error_code": None, "error_type": _failure_label(exc)}
    return "failed", {"error_code": None, "accepted_unknown_model": True}


def check_cancellation(model: Any) -> tuple[CheckStatus, dict[str, Any]]:
    stream = model.stream(CANCEL_PROMPT)
    try:
        next(stream)
    except StopIteration:
        return "failed", {"cancel_first_chunk": False}
    started = time.monotonic()
    stream.close()
    elapsed = time.monotonic() - started
    return ("passed" if elapsed <= CANCEL_DEADLINE_SECONDS else "failed"), {
        "cancel_first_chunk": True,
        "cancel_close_seconds": round(elapsed, 3),
    }


def endpoint_checks(
    endpoint: ChatEndpoint, api_key: str
) -> tuple[dict[str, CheckStatus], int, dict[str, Any]]:
    """七项检查。某一项抛错只把它（或共用一次调用的那组）记为 ``blocked``，其余照常跑。"""

    from water_agent.harness.local_model_compat import model_facing_checks

    model = _model(endpoint, api_key)
    checks: dict[str, CheckStatus] = {}
    evidence: dict[str, Any] = {}
    schema_tokens = 0
    try:
        facing, schema_tokens, facing_evidence = model_facing_checks(model)
        checks.update(facing)
        evidence.update(facing_evidence)
    except Exception as exc:  # noqa: BLE001
        for name in (
            "function_calling",
            "structured_citations",
            "programmatic_calculation",
            "tool_schema_budget",
        ):
            checks[name] = "blocked"
        evidence["model_facing_error"] = _failure_label(exc)

    single: Mapping[str, Callable[[], tuple[CheckStatus, dict[str, Any]]]] = {
        "streaming_usage": lambda: check_streaming_usage(model),
        "error_handling": lambda: check_error_handling(endpoint, api_key),
        "cancellation_timeout": lambda: check_cancellation(model),
    }
    for name, run_check in single.items():
        try:
            status, extra = run_check()
        except Exception as exc:  # noqa: BLE001
            status, extra = "blocked", {f"{name}_error": _failure_label(exc)}
        checks[name] = status
        evidence.update(extra)
    return checks, schema_tokens, evidence


def _status(checks: Mapping[str, CheckStatus]) -> str:
    if set(checks) == set(ENDPOINT_COMPATIBILITY_CHECKS) and all(
        v == "passed" for v in checks.values()
    ):
        return "compatible"
    return "incomplete" if any(v == "failed" for v in checks.values()) else "blocked"


def run_endpoint_compatibility(
    *,
    getenv: Callable[[str], str | None] = os.getenv,
    out_path: str | Path | None = None,
    checker: Callable[
        [ChatEndpoint, str], tuple[dict[str, CheckStatus], int, dict[str, Any]]
    ] = endpoint_checks,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[CompatibilityReport | None, dict[str, Any] | None]:
    """返回 ``(报告, None)``，或没有 Key 时的 ``(None, SKIP 说明)``。``out_path`` 给出时写报告（0600）。"""

    endpoint = resolve_chat_endpoint(getenv=getenv)
    if endpoint.source == SOURCE_LOCAL_MODEL:
        raise EndpointConfigError(
            "LOCAL_MODEL_ENABLED 时请用 python -m water_agent.harness.local_model_compat（本地模型路径的检查集）"
        )
    api_key = endpoint.api_key(getenv=getenv)
    if not api_key:
        return None, {
            "status": "skipped",
            "requires": [endpoint.api_key_name],
            "endpoint_host": endpoint.host,
            "model": endpoint.model,
            "reason": "没有对话端点的 Key，未发任何请求",
        }
    checks, schema_tokens, evidence = checker(endpoint, api_key)
    report = CompatibilityReport(
        version=PROBE_VERSION,
        status=_status(checks),  # type: ignore[arg-type]
        model=endpoint.model,
        endpoint=endpoint.base_url,
        checks=dict(checks),
        schema_tokens=schema_tokens,
        evidence={
            "probe_version": PROBE_VERSION,
            "kind": "openai_compatible_endpoint",
            "endpoint_host": endpoint.host,
            "checked_at": now().isoformat(timespec="seconds"),
            **evidence,
        },
    )
    if out_path is not None:
        write_report(out_path, report)
    return report, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P7-S10 OpenAI-compatible chat endpoint compatibility probe"
    )
    parser.add_argument("--out", default=None, help="报告输出路径（不给就只打印）")
    args = parser.parse_args(argv)
    print(
        "注意：本探针会对当前配置的对话端点发出约五次计费请求（不含 Key 与请求原文的报告见 --out）",
        file=sys.stderr,
    )
    try:
        report, skipped = run_endpoint_compatibility(out_path=args.out)
    except EndpointConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if report is None:
        print(json.dumps(skipped, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if report.passes(ENDPOINT_COMPATIBILITY_CHECKS) else 1


__all__ = [
    "CANCEL_DEADLINE_SECONDS",
    "ERROR_DEADLINE_SECONDS",
    "ERROR_PROBE_MODEL",
    "PROBE_VERSION",
    "check_cancellation",
    "check_error_handling",
    "check_streaming_usage",
    "endpoint_checks",
    "main",
    "run_endpoint_compatibility",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

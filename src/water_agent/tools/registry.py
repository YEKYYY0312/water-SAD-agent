"""工具注册表（Step 4）：注册 / 唯一执行入口 / 结构化错误转换。

执行路径唯一化：所有工具执行统一走 ``ToolRegistry.invoke``；LangChain/LLM 适配层
（Step 6）只做参数与结果转换，不复制校验与错误语义。异常一律转结构化错误返回，
绝不向循环抛裸异常。

P2.5 Step 12：``ToolSpec.timeout_seconds`` 接入**协作式 deadline** —— 执行后
校验耗时，超时返回结构化 ``TOOL_TIMEOUT``。当前 4 个工具都是只读/纯计算，
故不用杀线程实现（规格 §4 禁止用杀线程破坏不可逆操作）。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from water_agent.runtime.governance.contracts import ConfirmationGrant, Principal
from water_agent.runtime.governance.pipeline import (
    GuardrailPipeline,
    default_guardrail_pipeline,
)
from water_agent.tools.base import ToolSpec

# 结构化错误码
CODE_UNKNOWN_TOOL = "UNKNOWN_TOOL"
CODE_INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
CODE_TOOL_ERROR = "TOOL_ERROR"
#: 工具执行超过 ``ToolSpec.timeout_seconds``（协作式 deadline，Step 12）。
CODE_TOOL_TIMEOUT = "TOOL_TIMEOUT"
#: 护栏前置校验拒绝（capability/参数大小/allowlist/参数注入，Step 14）。
CODE_GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"


def _hash_args(args: dict[str, Any]) -> str:
    """工具参数的稳定哈希（Step 15 观测用）。

    只进 trace 的 ``args_hash``，**不写参数原文** —— 工具参数可能含 PII
    或注入载荷。``default=str`` 保证不可序列化对象也能得到稳定摘要，
    哈希失败时返回空串（观测不得影响执行）。
    """
    try:
        payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _structured_error(code: str, message: str, suggestions: list[str] | None = None) -> dict:
    return {
        "error": True,
        "code": code,
        "message": message,
        "suggestions": suggestions or [],
    }


class ToolRegistry:
    """按名称注册与调度 ``ToolSpec`` 的注册表。

    P2.5 Step 14：``invoke`` 在执行前**强制**经 ``GuardrailPipeline.validate_tool_call``
    做 capability / 参数大小 / 路径与域名 allowlist / 参数注入校验。
    ``pipeline`` 参数可选，但为 ``None`` 时取**进程级默认管线**而非跳过校验 ——
    因此不存在"不传即绕过"的后门（规格 §1.2）。

    P2.5 Step 15：可选 ``trace`` 记录器把**护栏裁定**与**工具执行**成对写入 trace
    （共享 ``invocation_id``），使 `guardrail_bypass` 检测器能事后证明"这次执行
    确实过了护栏"。``trace`` 为 ``None`` 时不记录 —— 观测缺失不得影响执行语义，
    但也因此**不能**用"没有 trace"当作"没有绕过"的证据。

    Args:
        pipeline: 护栏管线；``None`` 用 ``default_guardrail_pipeline()``。
        principal: 执行主体；``None`` 用 CLI 本地主体（默认能力集）。
        trace: trace 记录器（需已 ``start``）；``None`` 不记录观测事件。
    """

    def __init__(
        self,
        *,
        pipeline: GuardrailPipeline | None = None,
        principal: Principal | None = None,
        trace: Any | None = None,
    ) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._pipeline = pipeline
        self._principal = principal
        self._trace = trace

    def _resolve_pipeline(self) -> GuardrailPipeline:
        return self._pipeline if self._pipeline is not None else default_guardrail_pipeline()

    def _resolve_principal(self) -> Principal:
        if self._principal is not None:
            return self._principal
        return Principal.local("tool-registry")

    def _emit(self, method: str, **kwargs: Any) -> None:
        """尽力而为地写一条 trace 事件。

        观测**永不**影响执行语义：recorder 未 start、方法不存在或写入抛错，
        都只是静默丢弃这条事件。反过来说，缺事件不能当作"没绕过"的证据 ——
        这一点写进了 operator 文档。
        """
        if self._trace is None:
            return
        emitter = getattr(self._trace, method, None)
        if emitter is None:
            return
        try:
            emitter(**kwargs)
        except Exception:  # noqa: BLE001 — 观测失败不得影响工具执行
            return

    def register(self, spec: ToolSpec) -> None:
        """注册工具；重名抛 ValueError。名称已在 ToolSpec 校验为 snake_case。"""
        if spec.name in self._tools:
            raise ValueError(f"工具 {spec.name!r} 已注册")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_tools(self) -> list[dict]:
        """导出 OpenAI 风格工具定义列表，供模型（DeepSeek/OpenAI）消费。"""
        return [spec.to_openai_tool() for spec in self._tools.values()]

    def list_all_names(self) -> list[str]:
        """返回已注册工具名列表（供 LangChain 适配层批量转换）。"""
        return sorted(self._tools)

    def invoke(self, name: str, **args: Any) -> dict:
        """执行工具：护栏前置校验 → schema 校验 → guard → 执行（含协作式 deadline）。

        成功返回 execute 的输出 dict；任何失败返回结构化错误 dict，不抛裸异常。
        护栏拒绝返回 ``GUARDRAIL_BLOCKED``，并带脱敏原因（不含原始命中文本）。

        P2.5 Step 15：每条返回路径都写一条工具事件（``executed`` 区分"真跑了"
        与"被拦在执行前"），护栏裁定事件与之共享 ``invocation_id``。
        """
        return self._invoke(name, args, grant=None)

    def invoke_confirmed(
        self, name: str, args: dict[str, Any], *, grant: ConfirmationGrant
    ) -> dict:
        """经人工确认的执行（P7-S7）：与 :meth:`invoke` 同一条路径，只多把凭证交给护栏。

        只有外层确认门在合法的批准之后调用它。凭证绑定工具名、参数摘要与主体，单次有效；
        不一致或重放都由护栏拒绝（``GUARDRAIL_BLOCKED``），与其他护栏拒绝同一种返回形态。
        参数是独立的 dict 而不是 ``**args``：``invoke`` 的关键字参数全是工具参数，凭证若混在
        里面会与工具字段撞名。
        """
        return self._invoke(name, dict(args), grant=grant)

    def _invoke(
        self, name: str, args: dict[str, Any], *, grant: ConfirmationGrant | None
    ) -> dict:
        invocation_id = uuid4().hex[:16]
        args_hash = _hash_args(args)

        spec = self._tools.get(name)
        if spec is None:
            self._emit(
                "record_tool_event",
                tool_name=name,
                status="failed",
                code=CODE_UNKNOWN_TOOL,
                invocation_id=invocation_id,
                args_hash=args_hash,
                executed=False,
            )
            return _structured_error(
                CODE_UNKNOWN_TOOL,
                f"未知工具 {name!r}；已注册：{sorted(self._tools)}",
                [f"可用工具：{sorted(self._tools)}"],
            )

        # P2.5 Step 14：护栏前置校验（不可绕过）。必须在 schema 校验与 execute 之前 ——
        # 越权/注入参数不应有机会通过 pydantic 后进入执行路径。
        guard_started = time.monotonic()
        outcome = self._resolve_pipeline().validate_tool_call(
            name, dict(args), self._resolve_principal(), confirmation_grant=grant
        )
        guard_ms = (time.monotonic() - guard_started) * 1000
        payload = outcome.trace_payload()
        self._emit(
            "record_guardrail_event",
            stage="tool",
            action=payload.get("action", ""),
            rule_id=payload.get("rule_id", ""),
            ruleset_version=payload.get("ruleset_version", ""),
            code=payload.get("code"),
            hits=payload.get("hits", []),
            invocation_id=invocation_id,
            duration_ms=guard_ms,
        )
        if not outcome.allowed:
            self._emit(
                "record_tool_event",
                tool_name=name,
                status="blocked",
                code=CODE_GUARDRAIL_BLOCKED,
                invocation_id=invocation_id,
                args_hash=args_hash,
                executed=False,
            )
            return _structured_error(
                CODE_GUARDRAIL_BLOCKED,
                f"工具 {name!r} 未通过护栏校验：{outcome.decision.detail}",
                ["调整参数或申请相应能力后重试"],
            )

        try:
            model = spec.args_model.model_validate(args)
        except ValidationError as exc:
            # 只聚合首个错误位置对应的字段名（pydantic 2 的 errors()[0]['loc']）
            field = ".".join(str(part) for part in exc.errors()[0]["loc"])
            msg = exc.errors()[0].get("msg", "schema 校验失败")
            self._emit(
                "record_tool_event",
                tool_name=name,
                status="failed",
                code=CODE_INVALID_ARGUMENTS,
                invocation_id=invocation_id,
                args_hash=args_hash,
                executed=False,
            )
            return _structured_error(
                CODE_INVALID_ARGUMENTS,
                f"参数 {field!r} 非法：{msg}",
                [f"请提供合法 {field}"],
            )

        if spec.guard is not None:
            try:
                spec.guard(model)
            except Exception as exc:  # noqa: BLE001 — guard 失败转结构化错误
                self._emit(
                    "record_tool_event",
                    tool_name=name,
                    status="failed",
                    code=CODE_TOOL_ERROR,
                    invocation_id=invocation_id,
                    args_hash=args_hash,
                    executed=False,
                )
                return _structured_error(CODE_TOOL_ERROR, f"安全校验未通过：{exc}", ["调整参数后重试"])

        try:
            started = time.monotonic()
            result = spec.execute(model)
            elapsed = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001 — 工具异常必须转结构化错误，绝不外泄
            # executed=True：execute 确实被调用了（只是抛了异常），
            # 这与"被拦在执行前"是不同的事实，检测器需要区分。
            self._emit(
                "record_tool_event",
                tool_name=name,
                status="failed",
                code=CODE_TOOL_ERROR,
                invocation_id=invocation_id,
                args_hash=args_hash,
                executed=True,
            )
            return _structured_error(
                CODE_TOOL_ERROR,
                f"工具 {name!r} 执行失败：{exc}",
                ["请核对参数或稍后重试"],
            )
        if elapsed > spec.timeout_seconds:
            # 协作式 deadline：结果已产出但已超时。只读/纯计算工具丢弃结果安全；
            # 不杀线程（规格 §4：不得用杀线程破坏不可逆操作）。
            self._emit(
                "record_tool_event",
                tool_name=name,
                status="failed",
                code=CODE_TOOL_TIMEOUT,
                invocation_id=invocation_id,
                args_hash=args_hash,
                executed=True,
                duration_ms=elapsed * 1000,
            )
            return _structured_error(
                CODE_TOOL_TIMEOUT,
                f"工具 {name!r} 执行耗时 {elapsed:.3f}s 超过上限 {spec.timeout_seconds}s",
                ["缩小输入规模后重试"],
            )
        if not isinstance(result, dict):
            self._emit(
                "record_tool_event",
                tool_name=name,
                status="failed",
                code=CODE_TOOL_ERROR,
                invocation_id=invocation_id,
                args_hash=args_hash,
                executed=True,
                duration_ms=elapsed * 1000,
            )
            return _structured_error(
                CODE_TOOL_ERROR,
                f"工具 {name!r} 必须返回 dict，得到 {type(result).__name__}",
                [],
            )
        self._emit(
            "record_tool_event",
            tool_name=name,
            status="succeeded",
            invocation_id=invocation_id,
            args_hash=args_hash,
            executed=True,
            duration_ms=elapsed * 1000,
        )
        return result
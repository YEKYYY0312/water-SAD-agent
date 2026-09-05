"""工具注册表（Step 4）：注册 / 唯一执行入口 / 结构化错误转换。

执行路径唯一化：所有工具执行统一走 ``ToolRegistry.invoke``；LangChain/LLM 适配层
（Step 6）只做参数与结果转换，不复制校验与错误语义。异常一律转结构化错误返回，
绝不向循环抛裸异常。

P2.5 Step 12：``ToolSpec.timeout_seconds`` 接入**协作式 deadline** —— 执行后
校验耗时，超时返回结构化 ``TOOL_TIMEOUT``。当前 4 个工具都是只读/纯计算，
故不用杀线程实现（规格 §4 禁止用杀线程破坏不可逆操作）。
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from water_agent.tools.base import ToolSpec

# 结构化错误码
CODE_UNKNOWN_TOOL = "UNKNOWN_TOOL"
CODE_INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
CODE_TOOL_ERROR = "TOOL_ERROR"
#: 工具执行超过 ``ToolSpec.timeout_seconds``（协作式 deadline，Step 12）。
CODE_TOOL_TIMEOUT = "TOOL_TIMEOUT"


def _structured_error(code: str, message: str, suggestions: list[str] | None = None) -> dict:
    return {
        "error": True,
        "code": code,
        "message": message,
        "suggestions": suggestions or [],
    }


class ToolRegistry:
    """按名称注册与调度 ``ToolSpec`` 的注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

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
        """执行工具：先校验 schema，再执行并捕获异常。

        成功返回 execute 的输出 dict；任何失败返回结构化错误 dict，不抛裸异常。
        """
        spec = self._tools.get(name)
        if spec is None:
            return _structured_error(
                CODE_UNKNOWN_TOOL,
                f"未知工具 {name!r}；已注册：{sorted(self._tools)}",
                [f"可用工具：{sorted(self._tools)}"],
            )
        try:
            model = spec.args_model.model_validate(args)
        except ValidationError as exc:
            # 只聚合首个错误位置对应的字段名（pydantic 2 的 errors()[0]['loc']）
            field = ".".join(str(part) for part in exc.errors()[0]["loc"])
            msg = exc.errors()[0].get("msg", "schema 校验失败")
            return _structured_error(
                CODE_INVALID_ARGUMENTS,
                f"参数 {field!r} 非法：{msg}",
                [f"请提供合法 {field}"],
            )

        if spec.guard is not None:
            try:
                spec.guard(model)
            except Exception as exc:  # noqa: BLE001 — guard 失败转结构化错误
                return _structured_error(CODE_TOOL_ERROR, f"安全校验未通过：{exc}", ["调整参数后重试"])

        try:
            started = time.monotonic()
            result = spec.execute(model)
            elapsed = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001 — 工具异常必须转结构化错误，绝不外泄
            return _structured_error(
                CODE_TOOL_ERROR,
                f"工具 {name!r} 执行失败：{exc}",
                ["请核对参数或稍后重试"],
            )
        if elapsed > spec.timeout_seconds:
            # 协作式 deadline：结果已产出但已超时。只读/纯计算工具丢弃结果安全；
            # 不杀线程（规格 §4：不得用杀线程破坏不可逆操作）。
            return _structured_error(
                CODE_TOOL_TIMEOUT,
                f"工具 {name!r} 执行耗时 {elapsed:.3f}s 超过上限 {spec.timeout_seconds}s",
                ["缩小输入规模后重试"],
            )
        if not isinstance(result, dict):
            return _structured_error(
                CODE_TOOL_ERROR,
                f"工具 {name!r} 必须返回 dict，得到 {type(result).__name__}",
                [],
            )
        return result
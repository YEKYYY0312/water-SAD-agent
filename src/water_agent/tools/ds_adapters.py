"""ToolSpec → BaseTool 适配层（Step 6）。

适配层只做 LangChain 适配，不复制校验与结构化错误转换：
- ``_parse_input`` 跳过 schema 校验（否则 ``StructuredTool`` 会在 ``_run`` 之前
  抛 ``ValidationError``，把结构化错误变成裸异常）；
- ``_run`` 直接代理到 ``ToolRegistry.invoke``，异常/guard/校验全部由 Registry
  统一转换为结构化错误 dict；
- ``args_schema`` 保留原 spec 模型（供模型侧 function-calling schema 使用），
  ``description`` 原样透传。

这样任意 ToolSpec 都可以被 ``create_react_agent`` / ``ToolNode`` 直接消费，
且执行路径唯一（Registry.invoke）。
"""
from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import ConfigDict

from water_agent.tools.registry import ToolRegistry


class _RegistryBackedTool(StructuredTool):
    """代理到 ``ToolRegistry.invoke`` 的 LangChain 工具。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    registry: ToolRegistry | None = None
    tool_name: str = ""

    def _parse_input(self, tool_input: Any, tool_call_id: str | None = None) -> Any:
        # 跳过 StructuredTool 自带 schema 校验：输入校验统一由 Registry.invoke 承担
        return tool_input

    def _run(
        self,
        *args: Any,
        config: RunnableConfig | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
        **kwargs: Any,
    ) -> Any:
        if self.registry is None:
            raise RuntimeError("工具未绑定 registry")
        return self.registry.invoke(self.tool_name, **kwargs)


def tool_spec_to_base_tool(registry: ToolRegistry, tool_name: str) -> StructuredTool:
    """把已注册的 ToolSpec 转为 LangChain ``BaseTool``。

    只负责 LangChain 适配，不复制校验与错误语义：所有输入校验、guard、异常包装
    都由 ``ToolRegistry.invoke`` 统一承担。
    """
    spec = registry.get(tool_name)
    if spec is None:
        raise ValueError(f"工具 {tool_name!r} 未注册")
    return _RegistryBackedTool(
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_model,
        registry=registry,
        tool_name=tool_name,
    )

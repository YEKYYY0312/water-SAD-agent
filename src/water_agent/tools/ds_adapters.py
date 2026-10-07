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

P7-S7：要求人工确认的工具在 ToolNode 里换成 :func:`tool_spec_to_deferred_tool`——同名、同
schema 的 ``return_direct`` 延迟工具，执行时什么都不调用，只回一条"待确认"结果；真正的执行由
外层图的确认门在人工批准之后经 ``ToolRegistry.invoke_confirmed`` 完成。
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import ConfigDict

from water_agent.runtime.cancellation import raise_if_cancelled
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
        # 已取消的运行不再执行新的工具调用（RunCancelled 由 ToolNode 原样上抛，不转成 observation）
        raise_if_cancelled()
        return self.registry.invoke(self.tool_name, **kwargs)


#: 延迟工具回填结果的 ``status``，以及写在 ``ToolMessage.artifact`` 里的待确认标记键（P7-S7）。
#: 判定"这是一条待确认消息"只认 artifact：artifact 由本地代码产生、不发给模型，远端工具的输出
#: 只能写进正文——正文写成同样的 JSON 也伪造不出待确认状态。
PENDING_CONFIRMATION_STATUS = "pending_confirmation"
PENDING_CONFIRMATION_ARTIFACT = "water_agent_pending_confirmation"


class _DeferredConfirmationTool(_RegistryBackedTool):
    """需人工确认的工具在 ToolNode 里的替身：执行时**不调用任何东西**（不碰注册表、不碰 MCP），
    只回一条带 ``confirmation_id`` 的待确认结果。``return_direct=True`` 让内层 react 循环在它之后
    直接结束、不再调模型；外层确认门据此挂起。``registry`` 只用于标明它属于哪次装配。"""

    def _run(
        self,
        *args: Any,
        config: RunnableConfig | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
        **kwargs: Any,
    ) -> Any:
        raise_if_cancelled()
        confirmation_id = uuid4().hex
        content = json.dumps(
            {
                "status": PENDING_CONFIRMATION_STATUS,
                "confirmation_id": confirmation_id,
                "tool": self.tool_name,
            },
            ensure_ascii=False,
        )
        return content, {PENDING_CONFIRMATION_ARTIFACT: confirmation_id}


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


def tool_spec_to_deferred_tool(registry: ToolRegistry, tool_name: str) -> StructuredTool:
    """把已注册、要求人工确认的 ToolSpec 转为延迟工具（P7-S7）。

    名字、描述、``args_schema`` 与 :func:`tool_spec_to_base_tool` 完全相同——模型看到的工具定义
    不因确认门而改变；区别只在执行：延迟工具不执行，``return_direct`` 结束内层循环。
    """
    spec = registry.get(tool_name)
    if spec is None:
        raise ValueError(f"工具 {tool_name!r} 未注册")
    return _DeferredConfirmationTool(
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_model,
        registry=registry,
        tool_name=tool_name,
        return_direct=True,
        response_format="content_and_artifact",
    )

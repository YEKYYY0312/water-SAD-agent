"""工具抽象层：ToolSpec（Step 4）。

工具是"模型可调用"的单一边界：一个工具、一个动作。``ToolSpec`` 承担工具的全部
自描述，满足 function-calling 设计四原则与分析 schema linter 的硬性约束：
- ``name`` snake_case、``^[a-z][a-z0-9_]{1,40}$``；
- ``description`` 40–1024 字符，含 "Use when / Do not for" 模式（选择准确率）；
- ``args_model`` 用 pydantic 建模，枚举字段用 Literal/Enum 约束（防幻觉参数）；
- ``execute(args_model) -> dict`` 返回 JSON 可序列化 dict，LLM 永不参与数值运算；
- ``guard`` 可选安全检查钩子（白名单/路径校验），执行前调用。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, field_validator

from water_agent.runtime.governance.contracts import SourceTag

# 工具名约束：snake_case，首字母小写，仅小写字母/数字/下划线，最长 40
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

_INJECTION_MARKERS = (
    "<SYSTEM>",
    "ignore previous instructions",
    "ignore the above",
    "forget everything",
)


class ToolSpec(BaseModel):
    """工具规格：名称 / 描述 / 输入 schema / 执行函数 / 超时 / 安全钩子。

    P2.5 Step 12 变更（规格 §4「禁止保留看似生效的死字段」）：

    - **删除 ``max_retries``**：从未被任何执行路径读取。工具重试语义与远程调用
      不同——``retrieve_clauses`` 内部的远程 embedding 重试已由 ``ProviderGateway``
      承担，纯计算工具重试无意义（同输入必得同输出）。保留一个不生效的字段
      会让读者以为重试已配置。
    - **``timeout_seconds`` 接入真实执行语义**：由 ``ToolRegistry.invoke`` 以
      **协作式 deadline** 校验（执行后检查耗时并结构化报错）。
      当前 4 个工具都是只读/纯计算，故**不用杀线程**实现超时——规格 §4 明确
      禁止用杀线程破坏不可逆操作。未来若加入写入类工具，需另设 deadline 协议。
    """

    model_config = {
        "arbitrary_types_allowed": True,
        "extra": "forbid",
    }

    name: str
    description: str
    args_model: type[BaseModel] = Field(description="pydantic 参数模型；枚举字段必须用 Literal/Enum 约束")
    execute: Callable[[Any], dict] = Field(description="执行函数，入参为 args_model 实例，返回 JSON 可序列化 dict")
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="协作式 deadline（秒）：ToolRegistry.invoke 执行后校验耗时，超时返回结构化 TOOL_TIMEOUT",
    )
    guard: Callable[[Any], None] | None = None
    #: P7-S11：本工具的结果来自外部网络时声明 ``SourceTag.WEB_CONTENT``——图据此置本回合污点
    #: （答案缓存拒绝写入、记忆写入拦截、终答追加披露行、终答网址须可追溯）。缺省 ``None``：现有工具一律不变。
    result_source: SourceTag | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError(
                f"工具名 {value!r} 必须匹配 snake_case 模式 {_NAME_RE.pattern!r}"
            )
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        length = len(value)
        if length < 40 or length > 1024:
            raise ValueError(f"description 长度必须 40–1024，当前 {length}")
        lowered = value.lower()
        if not (("use when" in lowered) and any(m in lowered for m in ("do not", "do not use"))):
            raise ValueError("description 必须包含 'Use when …' 与 'Do not use for …' 模式")
        for marker in _INJECTION_MARKERS:
            if marker in lowered:
                raise ValueError(f"description 含疑似注入标记 {marker!r}")
        return value

    def to_openai_tool(self) -> dict:
        """导出为 OpenAI/DeepSeek 可直接消费的工具定义（含 additionalProperties: false）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }
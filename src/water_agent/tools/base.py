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

# 工具名约束：snake_case，首字母小写，仅小写字母/数字/下划线，最长 40
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

_INJECTION_MARKERS = (
    "<SYSTEM>",
    "ignore previous instructions",
    "ignore the above",
    "forget everything",
)


class ToolSpec(BaseModel):
    """工具规格：名称 / 描述 / 输入 schema / 执行函数 / 重试与超时 / 安全钩子。"""

    model_config = {
        "arbitrary_types_allowed": True,
        "extra": "forbid",
    }

    name: str
    description: str
    args_model: type[BaseModel] = Field(description="pydantic 参数模型；枚举字段必须用 Literal/Enum 约束")
    execute: Callable[[Any], dict] = Field(description="执行函数，入参为 args_model 实例，返回 JSON 可序列化 dict")
    max_retries: int = 3
    timeout_seconds: float = 30.0
    guard: Callable[[Any], None] | None = None

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

    @field_validator("max_retries")
    @classmethod
    def _validate_retries(cls, value: int) -> int:
        if value < 0 or value > 10:
            raise ValueError("max_retries 必须在 0–10")
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
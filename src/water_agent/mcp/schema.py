"""MCP 工具 inputSchema → pydantic 参数模型 + 不可信描述安检（P4 Step 23/25）。

schema 只支持**可确定性校验**的特性（规格 §4.3）：

- 顶层 ``type == "object"`` + ``properties`` + 可选 ``required``；
- 属性类型仅 ``string`` / ``integer`` / ``number`` / ``boolean`` /
  ``object``（自由 ``additionalProperties:true``）/ ``array``（自由 ``items``）；
- 约束仅 ``minimum`` / ``maximum`` / ``minLength`` / ``maxLength`` /
  ``default`` / ``enum``（string 枚举）；
- 其余特性（oneOf/allOf、$ref、pattern…）→
  :class:`UnsupportedSchemaError`，该工具**不暴露**（fail closed，不猜语义）。
- ``title`` / ``additionalProperties`` 仅为装饰性键，校验时忽略；
- 可空形态 ``anyOf: [T, {"type":"null"}]``（FastMCP 为可选参数生成）→
  归一化剥掉外层，直接用 ``T``。其他 anyOf/oneOf 仍拒绝——确定性优先。

``object``（自由 dict）与 ``array``（自由 list）映射为宽松类型，**不深入**
校验内部结构 —— 它们承载的是 ezdxf 头部无状态的 ``data``/``points`` 等
"在哪里落笔"的自由参数，收敛到"能调"即可，安全仍由护栏与 ToolRegistry 兜底。
生成模型恒 ``extra="forbid"``。

工具描述（``tools/list`` 的 description 与 schema 内嵌 description）是
**不可信文本**（工具投毒是 MCP 已公开攻击面）：:func:`ensure_description_safe`
先清洗（NFKC + 去零宽 + 折叠空白）再过注入/泄漏检测，命中即拒绝该工具。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from water_agent.mcp.errors import (
    MCP_DESCRIPTION_UNSAFE,
    MCP_UNSUPPORTED_SCHEMA,
    MCPError,
)
from water_agent.runtime.governance.rules import detect_injection, strip_invisible

_SUPPORTED_TYPES = frozenset({"string", "integer", "number", "boolean", "object", "array"})
_FREE_OBJECT_TYPE = "object"
_FREE_ARRAY_TYPE = "array"

_ALLOWED_PROPERTY_KEYS = frozenset(
    {"type", "description", "minimum", "maximum", "minLength", "maxLength",
     "default", "enum", "title", "additionalProperties", "anyOf", "items"}
)
_ALLOWED_TOP_KEYS = frozenset({"type", "properties", "required", "title", "additionalProperties"})

_SERVER_DESC_MAX_CHARS = 400


class UnsupportedSchemaError(MCPError):
    """inputSchema 含不支持的特性；``code`` 恒为 ``MCP_UNSUPPORTED_SCHEMA``。"""

    def __init__(self, message: str) -> None:
        super().__init__(MCP_UNSUPPORTED_SCHEMA, message)


def _type_name(value: Any) -> str:
    return type(value).__name__


def sanitize_description(description: str, *, max_chars: int = _SERVER_DESC_MAX_CHARS) -> str:
    """清洗不可信描述：NFKC + 去零宽 + 折叠空白 + 截断（标注截断）。"""
    normalized = unicodedata.normalize("NFKC", description)
    cleaned, _ = strip_invisible(normalized)
    collapsed = " ".join(cleaned.split())
    if len(collapsed) > max_chars:
        collapsed = collapsed[:max_chars] + "…[截断]"
    return collapsed


def ensure_description_safe(description: str, *, where: str = "工具描述") -> str:
    """安检不可信描述：清洗 → 注入/泄漏检测。命中即抛，**不脱敏放行**。"""
    cleaned = sanitize_description(description)
    hits = detect_injection(cleaned)
    if hits:
        summary = "; ".join(f"{h.rule_id}×{h.count}" for h in hits)
        raise MCPError(
            MCP_DESCRIPTION_UNSAFE,
            f"{where} 命中注入/提示索取规则（{summary}），工具不暴露；"
            "server 提供的描述是不可信文本，不得进入系统提示",
        )
    return cleaned


def _unwrap_nullable(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """剥掉 FastMCP 的可空包装 ``anyOf: [T, {"type":"null"}]``。

    只接受 **exactly** 可空形态：anyOf 长度 2 且其一为 ``{"type":"null"}``。
    其他 anyOf（一选多、非 null）仍属不支持（fail closed）。
    返回 (归一化后的 T 拷贝 + 外层 default/title/description 合并, was_nullable)。
    """
    if "anyOf" not in schema:
        return dict(schema), False
    allowed_outer = {"anyOf", "default", "title", "description"}
    if set(schema.keys()) - allowed_outer:
        raise UnsupportedSchemaError(
            f"属性含 anyOf 但外层还有不支持的键 {sorted(set(schema.keys()) - allowed_outer)}"
            "（只接受 anyOf 可空包装）"
        )
    any_of = schema["anyOf"]
    if not isinstance(any_of, list) or len(any_of) != 2:
        raise UnsupportedSchemaError("anyOf 只接受可空二选一形态（[T, {\"type\":\"null\"}]）")
    branches = [b for b in any_of if isinstance(b, dict)]
    if len(branches) != 2:
        raise UnsupportedSchemaError("anyOf 分支必须是 object")
    null_branch = next((b for b in branches if b.get("type") == "null" and set(b.keys()) <= {"type", "title"}), None)
    if null_branch is None:
        raise UnsupportedSchemaError("anyOf 只接受与 {\"type\":\"null\"} 的可空形态，其他 anyOf 不在支持子集")
    typed_branch = next(b for b in branches if b is not null_branch)
    merged: dict[str, Any] = dict(typed_branch)
    if "default" in schema and "default" not in merged:
        merged["default"] = schema["default"]
    if "description" in schema and "description" not in merged:
        merged["description"] = schema["description"]
    if "title" in schema and "title" not in merged:
        merged["title"] = schema["title"]
    return merged, True


def _validate_property_schema(name: str, schema: dict[str, Any]) -> None:
    normalized, _ = _unwrap_nullable(schema)
    schema = normalized
    unknown = set(schema) - _ALLOWED_PROPERTY_KEYS
    if unknown:
        raise UnsupportedSchemaError(
            f"属性 {name!r} 含不支持的 schema 键 {sorted(unknown)}"
            "（schema 不在支持子集）"
        )
    prop_type = schema.get("type")
    if prop_type not in _SUPPORTED_TYPES:
        raise UnsupportedSchemaError(
            f"属性 {name!r} 类型 {prop_type!r} 不在支持子集"
            f"（仅支持 {sorted(_SUPPORTED_TYPES)}）"
        )
    if prop_type == _FREE_OBJECT_TYPE:
        if "properties" in schema:
            raise UnsupportedSchemaError(
                f"属性 {name!r} 为 nested object（含 properties），不在支持子集"
            )
        if "additionalProperties" not in schema:
            raise UnsupportedSchemaError(
                f"属性 {name!r} 为 object 但缺 additionalProperties（只接受自由 object additionalProperties:true，不在支持子集）"
            )
        extra = set(schema) - {"type", "description", "title", "additionalProperties", "default"}
        if extra:
            raise UnsupportedSchemaError(f"自由 object 属性 {name!r} 含不支持的键 {sorted(extra)}")
        return
    if prop_type == _FREE_ARRAY_TYPE:
        extra = set(schema) - {"type", "description", "title", "items", "default"}
        if extra:
            raise UnsupportedSchemaError(f"array 属性 {name!r} 含不支持的键 {sorted(extra)}")
        return
    enum_values = schema.get("enum")
    if enum_values is not None:
        if prop_type != "string" or not isinstance(enum_values, list) or not enum_values:
            raise UnsupportedSchemaError(
                f"属性 {name!r} 的 enum 必须是非空 string 数组（不在支持子集）"
            )
        if not all(isinstance(v, str) for v in enum_values):
            raise UnsupportedSchemaError(f"属性 {name!r} 的 enum 成员必须全是 string")
        if len(set(enum_values)) != len(enum_values):
            raise UnsupportedSchemaError(f"属性 {name!r} 的 enum 成员重复")
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and not isinstance(minimum, (int, float)):
        raise UnsupportedSchemaError(f"属性 {name!r} 的 minimum 必须是数值")
    if maximum is not None and not isinstance(maximum, (int, float)):
        raise UnsupportedSchemaError(f"属性 {name!r} 的 maximum 必须是数值")
    for bound_key in ("minLength", "maxLength"):
        bound = schema.get(bound_key)
        if bound is not None and (not isinstance(bound, int) or bound < 0):
            raise UnsupportedSchemaError(f"属性 {name!r} 的 {bound_key} 必须是非负整数")
    if (
        isinstance(minimum, (int, float))
        and isinstance(maximum, (int, float))
        and minimum > maximum
    ):
        raise UnsupportedSchemaError(f"属性 {name!r} 的 minimum > maximum")


def validate_input_schema(schema: Any) -> dict[str, Any]:
    """校验顶层 inputSchema 形状；不支持即抛 :class:`UnsupportedSchemaError`。"""
    if not isinstance(schema, dict):
        raise UnsupportedSchemaError(
            f"inputSchema 必须是 object，得到 {_type_name(schema)}（不在支持子集）"
        )
    unknown_top = set(schema) - _ALLOWED_TOP_KEYS
    if unknown_top:
        raise UnsupportedSchemaError(
            f"inputSchema 含不支持的顶层键 {sorted(unknown_top)}"
            "（schema 不在支持子集：只支持 type/properties/required）"
        )
    if schema.get("type") != "object":
        raise UnsupportedSchemaError(
            f"inputSchema.type 必须是 \"object\"，得到 {schema.get('type')!r}（不在支持子集）"
        )
    properties = schema.get("properties")
    if properties is None:
        raise UnsupportedSchemaError(
            "inputSchema 缺 properties（零参工具也应有空 properties，不在支持子集）"
        )
    if not isinstance(properties, dict):
        raise UnsupportedSchemaError(
            f"properties 必须是 object，得到 {_type_name(properties)}（不在支持子集）"
        )
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise UnsupportedSchemaError("required 必须是 string 数组（不在支持子集）")
    property_names = set(properties)
    missing = [r for r in required if r not in property_names]
    if missing:
        raise UnsupportedSchemaError(f"required 引用了不存在的属性：{missing}")
    for name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            raise UnsupportedSchemaError(
                f"属性 {name!r} 的 schema 必须是 object，得到 {_type_name(prop_schema)}"
            )
        _validate_property_schema(str(name), prop_schema)
    return schema


class _EnumChecker:
    def __init__(self, field_name: str, allowed: tuple[str, ...]) -> None:
        self.field_name = field_name
        self.allowed = allowed

    def __call__(self, value: str) -> str:
        if value not in self.allowed:
            legal = ", ".join(repr(v) for v in self.allowed)
            raise ValueError(
                f"参数 {self.field_name!r} 非法：{value!r} 不在枚举内；合法值：{legal}"
            )
        return value


def _string_field(
    name: str, schema: dict[str, Any], required: bool
) -> tuple[tuple[Any, Any], _EnumChecker | None]:
    schema, was_nullable = _unwrap_nullable(schema)
    raw_description = schema.get("description") or None
    description = sanitize_description(raw_description) if raw_description else None
    min_length = schema.get("minLength")
    max_length = schema.get("maxLength")
    enum_values = schema.get("enum")
    default = schema.get("default")
    nullable = was_nullable or default is None
    if enum_values is not None:
        allowed = tuple(str(v) for v in enum_values)
        if default is not None and default not in allowed:
            raise UnsupportedSchemaError(
                f"enum 属性的 default {default!r} 不在枚举成员内"
            )
        if default is None and not required:
            default = allowed[0]
        if default is not None:
            field_info = Field(description=description, default=default)
        else:
            field_info = Field(description=description)
        return ((str, field_info), _EnumChecker(name, allowed))
    if default is None and (not required or nullable):
        default = "" if not nullable else None  # type: ignore[assignment]
        if nullable:
            return ((str | None, Field(description=description, default=None)), None)  # type: ignore[return-value]
    if default is not None:
        field_info = Field(
            description=description, default=default,
            min_length=min_length, max_length=max_length,
        )
    else:
        field_info = Field(
            description=description, min_length=min_length, max_length=max_length,
        )
    return ((str, field_info), None)


def _numeric_field(
    schema: dict[str, Any], required: bool, *, strict_int: bool
) -> tuple[Any, Any]:
    schema, was_nullable = _unwrap_nullable(dict(schema))
    raw_description = schema.get("description") or None
    description = sanitize_description(raw_description) if raw_description else None
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    default = schema.get("default")
    field_type: Any = int if strict_int else float
    if strict_int and isinstance(default, float) and not default.is_integer():
        raise UnsupportedSchemaError("integer 属性的 default 必须是整数值")
    if default is not None and not isinstance(default, (int, float)):
        raise UnsupportedSchemaError(f"数值属性的 default 必须是数值，得到 {_type_name(default)}")
    nullable = was_nullable or default is None
    if default is None and (not required or nullable):
        if nullable:
            return (field_type | None, Field(description=description, ge=minimum, le=maximum, default=None))  # type: ignore[return-value]
        default = 0
    if default is not None:
        field_info = Field(description=description, ge=minimum, le=maximum, default=default)
    else:
        field_info = Field(description=description, ge=minimum, le=maximum)
    return (field_type, field_info)


def _boolean_field(schema: dict[str, Any], required: bool) -> tuple[Any, Any]:
    schema, was_nullable = _unwrap_nullable(dict(schema))
    raw_description = schema.get("description") or None
    description = sanitize_description(raw_description) if raw_description else None
    default = schema.get("default")
    if default is not None and not isinstance(default, bool):
        raise UnsupportedSchemaError(f"boolean 属性的 default 必须是 bool，得到 {_type_name(default)}")
    if default is None and (not required or was_nullable):
        default = False if not was_nullable else None  # type: ignore[assignment]
        if was_nullable:
            return (bool | None, Field(description=description, default=None))  # type: ignore[return-value]
    if default is not None:
        field_info = Field(description=description, default=default)
    else:
        field_info = Field(description=description)
    return (bool, field_info)


def _free_object_field(schema: dict[str, Any], required: bool) -> tuple[Any, Any]:
    schema, was_nullable = _unwrap_nullable(dict(schema))
    raw_description = schema.get("description") or None
    description = sanitize_description(raw_description) if raw_description else None
    if was_nullable or not required:
        return (dict | None, Field(description=description, default=None))  # type: ignore[return-value]
    return (dict, Field(description=description))


def _free_array_field(schema: dict[str, Any], required: bool) -> tuple[Any, Any]:
    schema, was_nullable = _unwrap_nullable(dict(schema))
    raw_description = schema.get("description") or None
    description = sanitize_description(raw_description) if raw_description else None
    if was_nullable or not required:
        return (list | None, Field(description=description, default=None))  # type: ignore[return-value]
    return (list, Field(description=description))


def build_args_model(tool_name: str, schema: Any) -> type[BaseModel]:
    """把远程 inputSchema 编译为 pydantic 参数模型。"""
    validated = validate_input_schema(schema)
    properties = validated["properties"]
    required = set(validated.get("required", []))

    fields: dict[str, Any] = {}
    validators: dict[str, Any] = {}
    for name, prop_schema in properties.items():
        normalized, was_nullable = _unwrap_nullable(dict(prop_schema))
        prop_type = normalized.get("type")
        # 如果是可空包装，从 required 集合中移除（anyOf nullable 语义修复）
        is_required = name in required and not was_nullable
        if prop_type == "string":
            (annotation, field_info), checker = _string_field(str(name), prop_schema, is_required)
            if checker is not None:
                validators[f"_check_enum_{name}"] = field_validator(name)(checker)
        elif prop_type == "integer":
            annotation, field_info = _numeric_field(prop_schema, is_required, strict_int=True)
        elif prop_type == "number":
            annotation, field_info = _numeric_field(prop_schema, is_required, strict_int=False)
        elif prop_type == "boolean":
            annotation, field_info = _boolean_field(prop_schema, is_required)
        elif prop_type == "object":
            annotation, field_info = _free_object_field(prop_schema, is_required)
        elif prop_type == "array":
            annotation, field_info = _free_array_field(prop_schema, is_required)
        else:
            raise UnsupportedSchemaError(f"属性 {name!r} 类型 {prop_type!r} 不在支持子集")
        fields[name] = (annotation, field_info)

    model = cast(
        "type[BaseModel]",
        create_model(
            f"{re.sub(r'[^A-Za-z0-9]', '_', tool_name)}Args",
            __config__=ConfigDict(extra="forbid"),
            __validators__=validators,
            **fields,
        ),
    )
    return model


__all__ = [
    "UnsupportedSchemaError",
    "build_args_model",
    "ensure_description_safe",
    "sanitize_description",
    "validate_input_schema",
]

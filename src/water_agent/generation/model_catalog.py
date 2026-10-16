"""模型目录（P7-S15，规格 ``docs/design/18-p7-multi-model.md`` §3.1）。

管理员在设置页维护的一份**服务端白名单**：同一个端点、同一个 Key 槽下，用户可以按会话 / 按消息选用哪几个模型。
目录**不是路由**：模型从不因失败、成本或问题难度被自动更换；``tier`` 只是给人看的标签，不参与任何决策。

存放与回滚：凭据卷里的 ``llm_models.json``（0600、同目录临时文件 + ``os.replace`` 原子写，与
``endpoints.write_panel_endpoint`` 同构），**不碰** ``llm_endpoint.json``。没有这个文件 = 只有全局模型可用，请求、
响应、提示、装配快照与此前逐字相同；回滚 = 删文件。

条目只承载**模型身份与展示属性**，不承载端点与密钥：请求体、会话 PATCH、目录本身都不接受 ``base_url`` / ``api_key``
（密钥只发给所属服务商；首版同端点）。``thinking`` 描述这个模型的思考模式如何对待：

- ``default``：不传任何思考相关参数，沿用服务商默认（缺省值，适用于没有思考开关的模型，避免严格服务商因未知字段 400）；
- ``off``：显式关闭思考（``extra_body={"thinking": {"type": "disabled"}}``），行为与没有思考的模型同构——
  DeepSeek 新模型默认开思考，而工具循环里开着思考需要回传 ``reasoning_content``（S16 才做）；
- ``roundtrip``：思考型，需要回传与展示（S16）。**S15 里只能保存，不能被选**。

``price`` 是可选的估算单价（USD / 1M token），**永远视为未核对**（``confirmed=False`` 的口径），只用于 trace 成本估算，
不做任何硬拒绝。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from water_agent.generation.endpoints import EndpointConfigError, validate_model_name

CATALOG_FILE = "llm_models.json"
CATALOG_FORMAT = 1
MAX_ENTRIES = 32
MAX_LABEL_LENGTH = 40

Tier = Literal["fast", "advanced"]
Thinking = Literal["default", "off", "roundtrip"]

_ROLE_RE = re.compile(r"^[a-z][a-z_]{0,15}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class CatalogError(EndpointConfigError):
    """目录文件读不出或内容不成立；``str(exc)`` 不含请求值，可以原样回给管理员。"""


class CatalogPrice(BaseModel):
    """目录项的估算单价（USD / 1M token）。永远未核对，只用于估算记账。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_1m: float = Field(ge=0, le=10_000)
    output_per_1m: float = Field(ge=0, le=10_000)
    cached_input_per_1m: float | None = Field(default=None, ge=0, le=10_000)


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str = Field(min_length=1, max_length=MAX_LABEL_LENGTH)
    tier: Tier
    thinking: Thinking = "default"
    enabled: bool = True
    #: ``None`` = 所有可写角色；给出则只有这些角色能选（角色名由路由层与 ``UserRole`` 对照）
    allowed_roles: tuple[str, ...] | None = None
    price: CatalogPrice | None = None

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        try:
            return validate_model_name(value)
        except EndpointConfigError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("label")
    @classmethod
    def _valid_label(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or _CONTROL_RE.search(cleaned):
            raise ValueError("label 不能为空，也不能含控制字符")
        return cleaned

    @field_validator("allowed_roles")
    @classmethod
    def _valid_roles(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not value or len(set(value)) != len(value) or not all(_ROLE_RE.fullmatch(role) for role in value):
            raise ValueError("allowed_roles 必须是非空、不重复的小写角色名列表")
        return value

    def public_view(self) -> dict[str, Any]:
        """成员可见的字段：不含价格与角色限制（那是管理员的配置细节）。"""

        return {"id": self.id, "label": self.label, "tier": self.tier, "thinking": self.thinking}


@dataclass(frozen=True, slots=True)
class CatalogState:
    """读取结果。``error`` 非空时 ``entries`` 为空（失败关闭：读不出的目录不当作"有模型可选"）。"""

    entries: tuple[CatalogEntry, ...]
    error: str | None = None

    def find(self, model_id: str) -> CatalogEntry | None:
        return next((entry for entry in self.entries if entry.id == model_id), None)


def catalog_path() -> Path:
    from water_agent.config.credentials import credentials_dir

    return credentials_dir() / CATALOG_FILE


def parse_entries(raw: object) -> tuple[CatalogEntry, ...]:
    """校验一组条目（来自文件或管理员请求体）。不成立抛 :class:`CatalogError`，说明里不回显条目内容。"""

    if not isinstance(raw, list):
        raise CatalogError("models 必须是列表")
    if len(raw) > MAX_ENTRIES:
        raise CatalogError(f"目录最多 {MAX_ENTRIES} 个模型")
    entries: list[CatalogEntry] = []
    for index, item in enumerate(raw):
        try:
            entries.append(CatalogEntry.model_validate(item))
        except ValidationError as exc:
            fields = sorted({".".join(str(part) for part in error["loc"]) or "?" for error in exc.errors()})
            raise CatalogError(f"第 {index + 1} 项不合法（字段：{'、'.join(fields)}）") from exc
    ids = [entry.id for entry in entries]
    if len(set(ids)) != len(ids):
        raise CatalogError("模型 id 不能重复")
    return tuple(entries)


def load_catalog() -> CatalogState:
    """读目录。文件不存在 = 空目录（缺省，行为不变）；读不出 / 不成立 = 空目录 + ``error``（失败关闭）。"""

    path = catalog_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CatalogState(())
    except OSError:
        return CatalogState((), "模型目录文件读不出")
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict) or set(payload) != {"version", "models"}:
            raise CatalogError("文件字段必须恰好是 version 与 models")
        if payload["version"] != CATALOG_FORMAT:
            raise CatalogError("目录文件版本不受支持")
        return CatalogState(parse_entries(payload["models"]))
    except (ValueError, TypeError) as exc:
        return CatalogState((), f"模型目录不可用：{exc}")


def write_catalog(entries: tuple[CatalogEntry, ...] | list[CatalogEntry]) -> tuple[CatalogEntry, ...]:
    """校验并原子写出目录（同目录临时文件 + ``os.replace``，0600）。凭据卷不可写时抛 ``OSError``。"""

    validated = parse_entries([entry.model_dump(mode="json") for entry in entries])
    target = catalog_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    body = json.dumps(
        {"version": CATALOG_FORMAT, "models": [entry.model_dump(mode="json") for entry in validated]},
        ensure_ascii=False,
        indent=2,
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, (body + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    return validated


def entry_for(model_id: str | None) -> CatalogEntry | None:
    """目录里这个 id 的条目；没有目录 / 读不出 / 没这一项都是 ``None``（调用方按"目录之外"处理）。"""

    if not model_id:
        return None
    return load_catalog().find(model_id)


def thinking_extra_body(thinking: str) -> dict[str, Any] | None:
    """某个思考配置要附加的请求体字段；``None`` = 什么都不传。"""

    if thinking == "off":
        return {"thinking": {"type": "disabled"}}
    return None


def extra_body_for(model_id: str | None) -> dict[str, Any] | None:
    """装配与兼容性探针**共用**的入口：同一个模型在两处必须带同一份 ``extra_body``。

    目录读不出时按"没有目录"处理（全局模型的行为与 S15 之前相同）；逐条选择在读不出的目录上本来就过不了校验。
    """

    entry = entry_for(model_id)
    return thinking_extra_body(entry.thinking) if entry is not None else None


__all__ = [
    "CATALOG_FILE",
    "CATALOG_FORMAT",
    "MAX_ENTRIES",
    "CatalogEntry",
    "CatalogError",
    "CatalogPrice",
    "CatalogState",
    "catalog_path",
    "entry_for",
    "extra_body_for",
    "load_catalog",
    "parse_entries",
    "thinking_extra_body",
    "write_catalog",
]

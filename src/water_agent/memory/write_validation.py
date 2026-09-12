"""Schema and engineering invariants for P5 Step 40 candidates."""

from __future__ import annotations

import re
from uuid import UUID

from water_agent.memory.contracts import (
    MemoryKind,
    MemoryKindInvalidError,
    MemoryScopeDeniedError,
    MemoryScopeType,
)
from water_agent.memory.write_contracts import MemoryValueBasis
from water_agent.runtime.governance.rules import MAX_USER_INPUT_BYTES

_NUMERIC_VALUE = re.compile(r"\d")
_VALUE_WITH_UNIT = re.compile(
    r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*"
    r"(?:mm|cm|km|m|l/s|l/min|l/d|m3/h|m³/h|m3/d|m³/d|"
    r"kpa|mpa|pa|m/s|m²|m2|m³|m3|kw|kg|t/d|%|℃|°c|"
    r"层|个|根|台|座|条|人|户|天|年|月|日|h|min|s)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_CALCULATION_CONCLUSION = re.compile(
    r"计算(?:结果|结论)|程序计算|calculated\s+result", re.IGNORECASE
)


def as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def coerce_scope(value: MemoryScopeType | str) -> MemoryScopeType:
    try:
        return MemoryScopeType(value)
    except (TypeError, ValueError) as exc:
        raise MemoryScopeDeniedError("memory scope is invalid") from exc


def coerce_kind(value: MemoryKind | str) -> MemoryKind:
    try:
        return MemoryKind(value)
    except (TypeError, ValueError) as exc:
        raise MemoryKindInvalidError() from exc


def validate_candidate_schema(
    *,
    scope_type: MemoryScopeType,
    kind: MemoryKind,
    content: str,
    value_basis: MemoryValueBasis,
) -> None:
    if kind is MemoryKind.USER_PREFERENCE and scope_type is not MemoryScopeType.USER:
        raise MemoryKindInvalidError("user_preference must use user scope")
    if (
        kind in (MemoryKind.PROJECT_FACT, MemoryKind.PROJECT_DECISION)
        and scope_type is not MemoryScopeType.PROJECT
    ):
        raise MemoryKindInvalidError("project facts and decisions require project scope")
    if len(content.encode("utf-8")) > MAX_USER_INPUT_BYTES:
        raise MemoryKindInvalidError("memory content exceeds the P0 byte limit")
    if kind is MemoryKind.PROJECT_FACT and _NUMERIC_VALUE.search(content):
        if _VALUE_WITH_UNIT.search(content) is None:
            raise MemoryKindInvalidError("numeric project facts require an explicit unit")
        if value_basis is not MemoryValueBasis.APPROVED_INPUT:
            raise MemoryKindInvalidError("numeric project facts require approved input basis")
    if (
        kind is MemoryKind.PROJECT_DECISION
        and value_basis is not MemoryValueBasis.APPROVED_DECISION
    ):
        raise MemoryKindInvalidError("project decisions require approved decision basis")
    if (
        _CALCULATION_CONCLUSION.search(content)
        and value_basis is not MemoryValueBasis.APPROVED_DECISION
    ):
        raise MemoryKindInvalidError(
            "calculation conclusions may only be saved as an approved decision"
        )


__all__ = ["as_uuid", "coerce_kind", "coerce_scope", "validate_candidate_schema"]

"""P7-S7：一次性执行凭证（``ConfirmationGrant``）与 ``ToolRegistry.invoke_confirmed``。

S1 的执行期拒绝原样保留：没有凭证的需确认调用一律 ``tool.confirmation_required``。凭证是确认门
在人工批准之后签发的 Python 对象（模型只能产出 JSON，传不出它），绑定工具名、参数摘要与主体，
只能用一次。
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from water_agent.runtime.governance.contracts import (
    CODE_GUARDRAIL_BLOCKED,
    DEFAULT_LOCAL_CAPABILITIES,
    Capability,
    ConfirmationGrant,
    Principal,
    confirmation_args_hash,
)
from water_agent.runtime.governance.inventory import McpToolBinding, register_mcp_tool_binding
from water_agent.runtime.governance.pipeline import GuardrailPipeline
from water_agent.tools.base import ToolSpec
from water_agent.tools.registry import CODE_GUARDRAIL_BLOCKED as REGISTRY_BLOCKED
from water_agent.tools.registry import ToolRegistry

TOOL = "mcp_s7grant_write"


class _WriteArgs(BaseModel):
    target: str = ""


@pytest.fixture(scope="module", autouse=True)
def _binding() -> None:
    # 运行时绑定只增不删：独立 server id，不清空别人的登记
    register_mcp_tool_binding(
        McpToolBinding(
            tool_name=TOOL,
            server_id="s7grant",
            remote_tool="write",
            capability=Capability.MCP_INVOKE,
            requires_human_confirmation=True,
        )
    )


@pytest.fixture
def principal() -> Principal:
    return Principal.local(
        "s7-grant", capabilities=DEFAULT_LOCAL_CAPABILITIES | {Capability.MCP_INVOKE}
    )


@pytest.fixture
def registry(principal: Principal) -> Iterator[tuple[ToolRegistry, list[dict[str, Any]]]]:
    executed: list[dict[str, Any]] = []

    def execute(args: _WriteArgs) -> dict[str, Any]:
        executed.append(args.model_dump())
        return {"ok": True, "target": args.target}

    registry = ToolRegistry(principal=principal)
    registry.register(
        ToolSpec(
            name=TOOL,
            description=(
                "合成写操作工具（P7-S7 凭证测试）。Use when 测试确认凭证的放行规则。"
                "Do not use for 任何真实问答。"
            ),
            args_model=_WriteArgs,
            execute=execute,
        )
    )
    yield registry, executed


def _grant(
    principal: Principal,
    *,
    tool_name: str = TOOL,
    args: dict[str, Any] | None = None,
    confirmation_id: str | None = None,
) -> ConfirmationGrant:
    return ConfirmationGrant(
        confirmation_id=confirmation_id or uuid4().hex,
        tool_name=tool_name,
        args_hash=confirmation_args_hash(args if args is not None else {"target": "A"}),
        principal_hash=principal.principal_hash,
        decided_at=datetime.now(UTC),
    )


def test_grant_must_match_tool_args_and_principal(
    registry: tuple[ToolRegistry, list[dict[str, Any]]], principal: Principal
) -> None:
    """P7-S7-A02：无凭证仍 ``tool.confirmation_required``（与 S1 同一断言）；工具名、参数摘要、主体任一
    不一致都是 ``tool.confirmation_mismatch``、不执行；三项一致才执行一次。"""

    pipeline = GuardrailPipeline()
    blocked = pipeline.validate_tool_call(TOOL, {"target": "A"}, principal)
    assert not blocked.allowed
    assert blocked.decision.rule_id == "tool.confirmation_required"
    assert blocked.decision.code == CODE_GUARDRAIL_BLOCKED

    tool_registry, executed = registry
    assert tool_registry.invoke(TOOL, target="A")["code"] == REGISTRY_BLOCKED

    other = Principal.local(
        "s7-grant-other", capabilities=DEFAULT_LOCAL_CAPABILITIES | {Capability.MCP_INVOKE}
    )
    mismatches = {
        "tool": _grant(principal, tool_name="mcp_s7grant_other"),
        "args": _grant(principal, args={"target": "B"}),
        "principal": _grant(other),
    }
    for label, grant in mismatches.items():
        outcome = pipeline.validate_tool_call(TOOL, {"target": "A"}, principal, confirmation_grant=grant)
        assert not outcome.allowed, label
        assert outcome.decision.rule_id == "tool.confirmation_mismatch", label
        assert outcome.decision.code == CODE_GUARDRAIL_BLOCKED, label
        assert tool_registry.invoke_confirmed(TOOL, {"target": "A"}, grant=grant)["code"] == (
            REGISTRY_BLOCKED
        ), label
    # 批准的是 A 参数、拿去执行 B 参数：摘要对不上，拒绝
    swapped = tool_registry.invoke_confirmed(TOOL, {"target": "B"}, grant=_grant(principal))
    assert swapped["code"] == REGISTRY_BLOCKED
    assert executed == []

    ok = tool_registry.invoke_confirmed(TOOL, {"target": "A"}, grant=_grant(principal))
    assert ok == {"ok": True, "target": "A"}
    assert executed == [{"target": "A"}]


def test_grant_is_single_use(
    registry: tuple[ToolRegistry, list[dict[str, Any]]], principal: Principal
) -> None:
    """P7-S7-A02：凭证只能用一次——第二次（含换一个注册表、换一个护栏实例）都是
    ``tool.confirmation_replayed``，执行次数不变。"""

    tool_registry, executed = registry
    grant = _grant(principal)
    assert tool_registry.invoke_confirmed(TOOL, {"target": "A"}, grant=grant)["ok"] is True
    assert executed == [{"target": "A"}]

    replayed = GuardrailPipeline().validate_tool_call(
        TOOL, {"target": "A"}, principal, confirmation_grant=grant
    )
    assert not replayed.allowed
    assert replayed.decision.rule_id == "tool.confirmation_replayed"
    again = tool_registry.invoke_confirmed(TOOL, {"target": "A"}, grant=grant)
    assert again["code"] == REGISTRY_BLOCKED
    assert executed == [{"target": "A"}]


def test_args_hash_is_canonical() -> None:
    """P7-S7-A02：参数摘要只取规范化 JSON（键顺序无关），值不同摘要不同。"""

    assert confirmation_args_hash({"a": 1, "b": "x"}) == confirmation_args_hash({"b": "x", "a": 1})
    assert confirmation_args_hash({"a": 1}) != confirmation_args_hash({"a": 2})
    assert len(confirmation_args_hash({})) == 64

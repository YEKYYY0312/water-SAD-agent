"""Optional third-party Memory MCP adapter for P5 Step 46.

Only a read-only search capability is eligible. The adapter uses the existing
P4 bridge and ToolRegistry for production calls; returned bodies are ignored
and only memory IDs/scores are handed to the native repository resolver.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from water_agent.mcp.bridge import MCPServerSession, bridge_tool_name, open_bridge
from water_agent.mcp.config import MCPGatewayConfig, MCPServerConfig
from water_agent.mcp.wiring import MCPGatewayAssembly, register_mcp_tools
from water_agent.memory.providers import MemoryProviderUnavailableError
from water_agent.prompting.assembler import _estimate_tokens
from water_agent.runtime.governance.contracts import Capability, Principal
from water_agent.tools.registry import ToolRegistry

READ_ONLY_MEMORY_TOOL_NAMES = frozenset(
    {"memory_search", "search_memory", "memory_query"}
)
_ACTOR_FIELDS = frozenset({"user_id", "owner_user_id"})
MAX_MEMORY_TOOL_SCHEMA_TOKENS = 4500


def filter_memory_mcp_allowlist(allow: tuple[str, ...] | None) -> tuple[str, ...]:
    """Return only explicitly allowed read-only search names.

    ``None`` remains an empty allowlist, matching the P4 fail-closed contract.
    Write, forget, import, and admin operations can never enter the adapter.
    """

    if allow is None:
        return ()
    return tuple(name for name in allow if name in READ_ONLY_MEMORY_TOOL_NAMES)


@dataclass(slots=True)
class MemoryMCPAdapter:
    """Invoke one governed remote search tool and return ID-only hints."""

    _invoker: Callable[[dict[str, Any]], dict[str, Any]]
    search_tool_name: str = "memory_search"
    accepted_fields: frozenset[str] | None = None
    _session: MCPServerSession | None = None
    schema_token_cost: int = 0

    @classmethod
    def from_invoker(
        cls,
        invoker: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        search_tool_name: str = "memory_search",
    ) -> MemoryMCPAdapter:
        """Create a deterministic seam for tests and host adapters."""

        return cls(invoker, search_tool_name=search_tool_name)

    @classmethod
    def open(
        cls,
        config: MCPServerConfig,
        gateway: MCPGatewayConfig,
        *,
        principal_thread_id: str = "memory-mcp",
    ) -> MemoryMCPAdapter:
        """Open a governed P4 session with only the read-search allowlist."""

        safe_allow = filter_memory_mcp_allowlist(config.allow)
        effective_config = config.model_copy(update={"allow": safe_allow})
        session = open_bridge(effective_config, gateway)
        local_to_remote = {
            bridge_tool_name(config.server_id, name): name for name in safe_allow
        }
        tool = next(
            (
                candidate
                for candidate in session.result.tools
                if candidate.name in local_to_remote
            ),
            None,
        )
        remote_name = local_to_remote.get(tool.name) if tool is not None else None
        if remote_name is None:
            detail = "; ".join(skip.detail for skip in session.result.skipped)[:300]
            session.close()
            raise MemoryProviderUnavailableError(
                f"Memory MCP search tool is not available{': ' + detail if detail else ''}"
            )

        assert tool is not None
        accepted_fields = frozenset(tool.args_model.model_fields)
        if (
            "query" not in accepted_fields
            or "tenant_id" not in accepted_fields
            or not (_ACTOR_FIELDS & accepted_fields)
        ):
            session.close()
            raise MemoryProviderUnavailableError(
                "Memory MCP search schema must carry query, tenant_id, and an authenticated user field"
            )

        assembly = MCPGatewayAssembly(
            sessions=(session,), skipped_servers=(), raw_config=gateway
        )
        registry = ToolRegistry(
            principal=Principal.local(
                principal_thread_id,
                capabilities=frozenset({Capability.MCP_INVOKE}),
            )
        )
        registered = register_mcp_tools(assembly, registry)
        if registered != [tool.name]:
            session.close()
            raise MemoryProviderUnavailableError(
                "Memory MCP assembly did not register exactly one search tool"
            )
        local_name = registered[0]
        schema_token_cost = _estimate_tokens(
            json.dumps(registry.list_tools(), ensure_ascii=False)
        )
        if schema_token_cost >= MAX_MEMORY_TOOL_SCHEMA_TOKENS:
            session.close()
            raise MemoryProviderUnavailableError(
                "Memory MCP tool schema reaches the existing 4500-token ceiling"
            )

        def invoke(payload: dict[str, Any]) -> dict[str, Any]:
            return registry.invoke(local_name, **payload)

        return cls(
            invoke,
            search_tool_name=remote_name,
            accepted_fields=accepted_fields,
            _session=session,
            schema_token_cost=schema_token_cost,
        )

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def _request_payload(
        self,
        *,
        query: str,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        project_id: UUID | str | None,
        limit: int,
    ) -> dict[str, Any]:
        fields = self.accepted_fields
        if fields is not None and (
            "tenant_id" not in fields or not (_ACTOR_FIELDS & fields)
        ):
            raise MemoryProviderUnavailableError(
            "Memory MCP search schema does not expose query and authenticated scope fields"
            )
        values: dict[str, Any] = {
            "query": query,
            "tenant_id": str(tenant_id),
            "user_id": str(actor_user_id),
            "owner_user_id": str(actor_user_id),
            "project_id": None if project_id is None else str(project_id),
            "limit": limit,
            "top_k": limit,
        }
        if fields is None:
            return values
        return {
            name: value
            for name, value in values.items()
            if name in fields and value is not None
        }

    def search_ids(
        self,
        *,
        query: str,
        tenant_id: UUID | str,
        actor_user_id: UUID | str,
        project_id: UUID | str | None,
        limit: int,
    ) -> tuple[tuple[UUID, float], ...]:
        """Call remote search and retain only validated ID/score pairs."""

        payload = self._request_payload(
            query=query,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            project_id=project_id,
            limit=limit,
        )
        try:
            result = self._invoker(payload)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            raise MemoryProviderUnavailableError("Memory MCP call failed") from exc
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            code = result.get("code", "remote_error") if isinstance(result, Mapping) else "remote_error"
            raise MemoryProviderUnavailableError(f"Memory MCP search failed: {code}")

        structured = result.get("structured_content") or result.get("structuredContent") or result
        if not isinstance(structured, Mapping) or not isinstance(structured.get("hits"), list):
            raise MemoryProviderUnavailableError("Memory MCP search returned no ID hits")
        hits: list[tuple[UUID, float]] = []
        for item in structured["hits"]:
            if not isinstance(item, Mapping):
                raise MemoryProviderUnavailableError("Memory MCP returned an invalid hit")
            try:
                memory_id = UUID(str(item["memory_id"]))
                score = float(item.get("score", 0.0))
            except (KeyError, TypeError, ValueError) as exc:
                raise MemoryProviderUnavailableError("Memory MCP returned an invalid hit") from exc
            if not math.isfinite(score):
                raise MemoryProviderUnavailableError("Memory MCP returned a non-finite score")
            hits.append((memory_id, score))
        return tuple(hits[:limit])


__all__ = [
    "MAX_MEMORY_TOOL_SCHEMA_TOKENS",
    "READ_ONLY_MEMORY_TOOL_NAMES",
    "MemoryMCPAdapter",
    "filter_memory_mcp_allowlist",
]

"""MCP OAuth 2.1 服务级授权（P7-S8 Step 7 完整实现；Step 4 先给出认证提供者的最小形态）。"""
from __future__ import annotations

from water_agent.mcp.config import MCPServerConfig


class OAuthAuth:
    """还没有令牌存储时的占位：不带 ``Authorization`` 头，server 回 401 即 ``MCP_OAUTH_REQUIRED``。"""

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config

    def authorization(self) -> str | None:
        return None

    def refresh_after_unauthorized(self) -> bool:
        return False

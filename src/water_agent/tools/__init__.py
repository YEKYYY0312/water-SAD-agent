"""工具抽象层：ToolSpec + ToolRegistry + 首批工具。"""
from __future__ import annotations

from water_agent.tools.base import ToolSpec
from water_agent.tools.registry import ToolRegistry

__all__ = ["ToolRegistry", "ToolSpec"]
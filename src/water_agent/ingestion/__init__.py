"""Ingestion：结构化父子级切块与入库 + 规范版本注册表（Step 8）。"""

from water_agent.ingestion.chunker import ChunkConfig, Chunker, chunk_clauses
from water_agent.ingestion.version_registry import VersionRegistry, get_version_registry

__all__ = ["ChunkConfig", "Chunker", "VersionRegistry", "chunk_clauses", "get_version_registry"]

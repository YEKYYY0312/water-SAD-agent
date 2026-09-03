"""规范版本注册表（Step 8）。

提供规范条款的版本状态查询：current / superseded / deprecated / unknown。

设计原则（任务书 Step 8 硬约束）：
- 只录入有来源或经人工确认的事实，禁止根据文件名/年份/索引年份推断；
- 未录入的条目必须显式返回 ``status="unknown"``，绝不静默猜测；
- 每条数据必须带 ``source``（来源）与 ``verified_at``（录入时间）——
  **加载时强制**：非 unknown 状态若缺任一字段，一律降级为 unknown（铁律做进代码，不靠约定）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 合法状态枚举（其余状态在加载时归一为 unknown）
STATUS_CURRENT = "current"
STATUS_SUPERSEDED = "superseded"
STATUS_DEPRECATED = "deprecated"
STATUS_UNKNOWN = "unknown"
VALID_STATUSES = {STATUS_CURRENT, STATUS_SUPERSEDED, STATUS_DEPRECATED, STATUS_UNKNOWN}

# 覆盖默认数据文件位置（打包安装后 parents[3] 不再指向仓库根时使用）
ENV_VERSIONS_PATH = "WATER_AGENT_VERSIONS_JSON"


@dataclass(frozen=True)
class SpecVersionRecord:
    """单条规范版本记录（只录事实：非 unknown 状态必须有 source + verified_at 佐证）。"""

    spec_id: str
    version: str
    status: str
    effective_date: str | None = None
    successor: str | None = None
    source: str = ""
    verified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "version": self.version,
            "status": self.status,
            "effective_date": self.effective_date,
            "successor": self.successor,
            "source": self.source,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpecVersionRecord:
        status = data.get("status", STATUS_UNKNOWN)
        if status not in VALID_STATUSES:
            status = STATUS_UNKNOWN
        source = str(data.get("source", "") or "").strip()
        verified_at = str(data.get("verified_at", "") or "").strip()
        # 铁律：无来源/无录入时间的"事实"不算事实，降级为 unknown
        if status != STATUS_UNKNOWN and (not source or not verified_at):
            status = STATUS_UNKNOWN
        return cls(
            spec_id=str(data.get("spec_id", "")).strip(),
            version=str(data.get("version", "")).strip(),
            status=status,
            effective_date=data.get("effective_date"),
            successor=data.get("successor"),
            source=source,
            verified_at=verified_at,
        )


def split_spec_id(spec_id: str) -> tuple[str, str | None]:
    """拆分规范 ID 与版本号。

    示例：
        "GB50015" -> ("GB50015", None)
        "GB50015-2019" -> ("GB50015", "2019")
    """
    if "-" in spec_id:
        base, _, tail = spec_id.rpartition("-")
        if base and tail.isdigit():
            return base, tail
    return spec_id, None


def unknown_status(spec_id: str, version: str | None = None) -> dict[str, Any]:
    """未录入条目的统一返回形态：显式 ``status="unknown"``，与已录入记录同构。"""
    return {
        "spec_id": spec_id,
        "version": version or "",
        "status": STATUS_UNKNOWN,
        "effective_date": None,
        "successor": None,
        "source": "",
        "verified_at": "",
    }


def _default_data_path() -> Path:
    override = os.environ.get(ENV_VERSIONS_PATH, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / "data" / "versions.json"


class VersionRegistry:
    """规范版本注册表：从 ``data/versions.json`` 加载，按 spec_id（可带版本）查询。"""

    def __init__(self, data_path: Path | None = None):
        self._data_path = data_path if data_path is not None else _default_data_path()
        self._records: list[SpecVersionRecord] = []
        self._loaded = False

    def load(self) -> None:
        """从 JSON 文件加载版本记录；文件缺失/损坏时保持空表（全部查询按 unknown 走）。"""
        if self._loaded:
            return
        self._loaded = True
        if not self._data_path.exists():
            return
        try:
            with open(self._data_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, list):
            return
        self._records = [SpecVersionRecord.from_dict(item) for item in data if isinstance(item, dict)]

    def get_spec_status(self, spec_id: str) -> dict[str, Any]:
        """查询规范版本状态，**总是**返回带 ``status`` 键的字典。

        兼容两种入参：
        - "GB50015"：返回该规范的记录；多版本并存时优先 current，其次按版本号降序；
        - "GB50015-2019"：只返回版本匹配的记录。

        未录入（或版本不匹配）返回 ``status="unknown"``（调用方不得推断）。
        """
        self.load()
        base_id, version = split_spec_id((spec_id or "").strip())
        if not base_id:
            return unknown_status(base_id, version)
        matches = [
            r for r in self._records
            if r.spec_id == base_id and (version is None or r.version == version)
        ]
        if not matches:
            return unknown_status(base_id, version)
        # current 优先；同状态下取版本号最大（最新）
        current = [r for r in matches if r.status == STATUS_CURRENT]
        chosen = max(current or matches, key=lambda r: r.version)
        return chosen.to_dict()

    def get_status_for_source_id(self, source_id: str) -> dict[str, Any]:
        """从 source_id（如 "GB50015-2019 5.4.2"）提取规范主体并查询状态。"""
        return self.get_spec_status((source_id or "").split(" ", 1)[0])

    def list_all(self) -> list[dict[str, Any]]:
        """返回所有版本记录。"""
        self.load()
        return [r.to_dict() for r in self._records]

    def snapshot_lines(self) -> list[str]:
        """渲染给人/模型看的版本状态快照行（按 spec_id 排序，稳定输出利于缓存）。"""
        self.load()
        lines = []
        for record in sorted(self._records, key=lambda r: (r.spec_id, r.version)):
            marker = {
                STATUS_CURRENT: "现行",
                STATUS_SUPERSEDED: "被取代",
                STATUS_DEPRECATED: "废止",
                STATUS_UNKNOWN: "状态未知（待人工核实）",
            }.get(record.status, "状态未知（待人工核实）")
            line = f"- {record.spec_id}-{record.version}: {marker}"
            if record.effective_date:
                line += f"（生效 {record.effective_date}）"
            if record.successor:
                line += f"，被 {record.successor} 取代"
            if record.source and record.status != STATUS_UNKNOWN:
                line += f"[来源: {record.source}]"
            lines.append(line)
        return lines


# 全局单例
_registry: VersionRegistry | None = None


def get_version_registry() -> VersionRegistry:
    """获取全局版本注册表单例（数据源 data/versions.json，可用 WATER_AGENT_VERSIONS_JSON 覆盖）。"""
    global _registry
    if _registry is None:
        _registry = VersionRegistry()
        _registry.load()
    return _registry

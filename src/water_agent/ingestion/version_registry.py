"""规范版本注册表（Step 8）。

提供规范条款的版本状态查询：current / superseded / deprecated / unknown。

设计原则（任务书 Step 8 硬约束）：
- 只录入有来源或经人工确认的事实，禁止根据文件名/年份/索引年份推断；
- 未录入的条目必须显式返回 ``status="unknown"``，绝不静默猜测；
- 每条数据必须带 ``source``（来源）与 ``verified_at``（录入时间）——
  **加载时强制**：非 unknown 状态若缺任一字段，一律降级为 unknown（铁律做进代码，不靠约定）。

2026-09-08 实测缺陷与修正
--------------------------
原守卫只检查字段**是否为空**，拦不住"字段填了但内容是占位符"。实测：把
``data/versions.json`` 四条的 ``status`` 改为 ``current``、``source`` 保留模板里的
"未核实：…人工核实后改为 current 并填 source"，四条**全部原样通过守卫**返回
``current`` —— 系统于是对外声称这四本规范现行有效，而依据是一句"未核实"。

这不是理论风险：模板预置占位文本，改 ``status`` 时漏改 ``source`` 是最自然的操作顺序。

故守卫加两条，且**降级原因写进记录本身**（``demotion_reason``）而非静默丢弃：
- ``source`` 含未核实标记（见 ``UNVERIFIED_MARKERS``）视为无来源；
- ``status="current"`` 必须有 ``effective_date`` —— 一本"现行有效"却没有生效日期的
  规范在工程上说不通（``superseded``/``deprecated`` 不强制，那两者的关键事实是 successor）。

降级原因会出现在 ``snapshot_lines()``（该快照进提示词），所以"改了 status 但没填依据"
立刻可见，而不是悄悄不生效 —— 静默降级会让人以为自己的核实已经起作用了。
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

#: ``source`` 里出现这些标记即视为**没有来源**（内容自述未核实）。
#: 2026-09-08 实测：模板占位文本"未核实：…"是非空字符串，旧守卫放行了它。
#: 保持小写比较；新增标记时同步 `tests/test_version_registry.py` 的逐条断言。
UNVERIFIED_MARKERS: tuple[str, ...] = (
    "未核实",
    "待核实",
    "待确认",
    "待补",
    "todo",
    "tbd",
    "fixme",
    "placeholder",
)


def _is_unverified_source(source: str) -> bool:
    """``source`` 是否实质上等于"没有来源"（空或自述未核实）。

    **不做**更聪明的判断（如"是否含公告号"）：那需要知道各国标公告号格式，
    猜错会把真实来源误判为占位符，反而挡住已完成的核实。
    """
    if not source:
        return True
    lowered = source.lower()
    return any(marker in lowered for marker in UNVERIFIED_MARKERS)


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
    #: 加载时被降级的原因（未降级为空串）。**写进记录而非静默丢弃** ——
    #: 它会出现在 `snapshot_lines()`，让"改了 status 但依据不足"立刻可见。
    demotion_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "version": self.version,
            "status": self.status,
            "effective_date": self.effective_date,
            "successor": self.successor,
            "source": self.source,
            "verified_at": self.verified_at,
            "demotion_reason": self.demotion_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpecVersionRecord:
        declared = data.get("status", STATUS_UNKNOWN)
        status = declared if declared in VALID_STATUSES else STATUS_UNKNOWN
        source = str(data.get("source", "") or "").strip()
        verified_at = str(data.get("verified_at", "") or "").strip()
        effective_date = data.get("effective_date")
        effective_str = str(effective_date or "").strip()

        # 铁律：无来源/无录入时间的"事实"不算事实，降级为 unknown。
        # 关键是 `_is_unverified_source` —— 只判"字段非空"会放行占位文本（实测已发生）。
        reason = ""
        if status != STATUS_UNKNOWN:
            if _is_unverified_source(source):
                # 不把整个标记元组写进消息 —— 这段文本会进提示词快照，
                # 列出全部标记既啰嗦又浪费 token；要查规则去看 UNVERIFIED_MARKERS。
                reason = (
                    f"source 缺失或自述未核实，声明的 status={status!r} 不予采信"
                    "（请填入可复核来源：公告号/生效日期/查询出处）"
                )
            elif not verified_at:
                reason = f"缺 verified_at，声明的 status={status!r} 不予采信"
            elif status == STATUS_CURRENT and not effective_str:
                # 只对 current 强制：superseded/deprecated 的关键事实是 successor，
                # 生效日期对它们不是必需的。
                reason = "status=current 但缺 effective_date（现行有效必须有生效日期）"
            if reason:
                status = STATUS_UNKNOWN

        return cls(
            spec_id=str(data.get("spec_id", "")).strip(),
            version=str(data.get("version", "")).strip(),
            status=status,
            effective_date=effective_date,
            successor=data.get("successor"),
            source=source,
            verified_at=verified_at,
            demotion_reason=reason,
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
    """未录入条目的统一返回形态：显式 ``status="unknown"``，与已录入记录同构。

    同构包含 ``demotion_reason``（未录入不是降级，故为空串）—— 调用方按固定键读取，
    少一个键会让"未录入"与"被降级"在下游需要两套取值逻辑。
    """
    return {
        "spec_id": spec_id,
        "version": version or "",
        "status": STATUS_UNKNOWN,
        "effective_date": None,
        "successor": None,
        "source": "",
        "verified_at": "",
        "demotion_reason": "",
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
        """渲染给人/模型看的版本状态快照行（按 spec_id 排序，稳定输出利于缓存）。

        被降级的条目**显式标出降级原因** —— 这个快照会进提示词，所以
        "改了 status 但依据不足"在这里就能看见，不必去读加载代码才发现没生效。
        """
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
            if record.demotion_reason:
                line += f"[已降级: {record.demotion_reason}]"
            lines.append(line)
        return lines

    def demoted_records(self) -> list[dict[str, Any]]:
        """被降级的记录清单（供 CLI/质检脚本提示"你的核实没生效"）。"""
        self.load()
        return [r.to_dict() for r in self._records if r.demotion_reason]


# 全局单例
_registry: VersionRegistry | None = None


def get_version_registry() -> VersionRegistry:
    """获取全局版本注册表单例（数据源 data/versions.json，可用 WATER_AGENT_VERSIONS_JSON 覆盖）。"""
    global _registry
    if _registry is None:
        _registry = VersionRegistry()
        _registry.load()
    return _registry

"""Step 8 测试：规范版本注册表。

覆盖：基本查询 / 版本号拆分 / 未知条目显式返回 status=unknown / 录入条目可判定状态 /
只录事实（无 source 或 verified_at 的非 unknown 条目加载时降级 unknown）/ 多版本优先 current。
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.ingestion.version_registry import (
    STATUS_CURRENT,
    STATUS_UNKNOWN,
    VersionRegistry,
    get_version_registry,
    split_spec_id,
)


class TestSplitSpecId:
    def test_no_version(self) -> None:
        assert split_spec_id("GB50015") == ("GB50015", None)

    def test_with_version(self) -> None:
        assert split_spec_id("GB50015-2019") == ("GB50015", "2019")

    def test_with_version_and_trailing(self) -> None:
        assert split_spec_id("GB50015-2019") == ("GB50015", "2019")

    def test_hyphen_in_name_no_digits(self) -> None:
        assert split_spec_id("GB-T50015") == ("GB-T50015", None)

    def test_empty(self) -> None:
        assert split_spec_id("") == ("", None)


def _write(path: Path, data: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class TestVersionRegistry:
    def test_empty_registry_returns_unknown(self) -> None:
        """文件缺失 → 任何查询都显式 status=unknown（契约：总是返回带 status 的 dict）。"""
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nonexistent.json"
            reg = VersionRegistry(path)
            status = reg.get_spec_status("GB50015")
            assert status["status"] == STATUS_UNKNOWN
            assert status["spec_id"] == "GB50015"

    def test_missing_source_demoted_to_unknown(self) -> None:
        """铁律：非 unknown 状态缺 source 或 verified_at → 加载时降级 unknown，不算事实。"""
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            _write(
                path,
                [
                    {"spec_id": "GB1", "version": "2019", "status": "current", "source": "", "verified_at": "2026-09-03"},
                    {"spec_id": "GB2", "version": "2019", "status": "current", "source": "人工确认", "verified_at": ""},
                    {"spec_id": "GB3", "version": "2019", "status": "deprecated", "source": "人工确认", "verified_at": "2026-09-03"},
                ],
            )
            reg = VersionRegistry(path)
            assert reg.get_spec_status("GB1")["status"] == STATUS_UNKNOWN
            assert reg.get_spec_status("GB2")["status"] == STATUS_UNKNOWN
            assert reg.get_spec_status("GB3")["status"] == "deprecated"

    def test_multi_version_prefers_current(self) -> None:
        """同一规范多版本并存：无版本查询优先 current；带版本查询精确匹配；版本不存在 → unknown。"""
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            _write(
                path,
                [
                    {"spec_id": "GB50015", "version": "2019", "status": "current", "source": "t", "verified_at": "2026-09-03"},
                    {"spec_id": "GB50015", "version": "2003", "status": "superseded", "successor": "GB50015-2019", "source": "t", "verified_at": "2026-09-03"},
                ],
            )
            reg = VersionRegistry(path)
            assert reg.get_spec_status("GB50015")["version"] == "2019"
            assert reg.get_spec_status("GB50015-2003")["status"] == "superseded"
            assert reg.get_spec_status("GB50015-2088")["status"] == STATUS_UNKNOWN

    def test_loads_from_json(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            data = [
                {"spec_id": "GB50015", "version": "2019", "status": "current", "source": "test", "verified_at": "2026-09-03"},
                {"spec_id": "GB50014", "version": "2021", "status": "current", "source": "test", "verified_at": "2026-09-03"},
            ]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            reg = VersionRegistry(path)
            assert reg.get_spec_status("GB50015")["status"] == "current"
            assert reg.get_spec_status("GB50014")["status"] == "current"

    def test_unknown_spec_returns_unknown_status(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            data = [{"spec_id": "GB50015", "version": "2019", "status": "current", "source": "test", "verified_at": "2026-09-03"}]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            reg = VersionRegistry(path)
            assert reg.get_spec_status("GB99999")["status"] == STATUS_UNKNOWN
            assert reg.get_spec_status("")["status"] == STATUS_UNKNOWN

    def test_source_id_query(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            data = [{"spec_id": "GB50015", "version": "2019", "status": "current", "source": "test", "verified_at": "2026-09-03"}]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            reg = VersionRegistry(path)
            result = reg.get_status_for_source_id("GB50015-2019 5.4.2")
            assert result["status"] == STATUS_CURRENT
            assert result["spec_id"] == "GB50015"

    def test_list_all(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            data = [
                {"spec_id": "GB50015", "version": "2019", "status": "current", "source": "test", "verified_at": "2026-09-03"},
                {"spec_id": "GB50014", "version": "2021", "status": "current", "source": "test", "verified_at": "2026-09-03"},
            ]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            reg = VersionRegistry(path)
            all_records = reg.list_all()
            assert len(all_records) == 2
            assert all(r["spec_id"] in ("GB50015", "GB50014") for r in all_records)

    def test_snapshot_lines(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            data = [{"spec_id": "GB50015", "version": "2019", "status": "current", "source": "test", "verified_at": "2026-09-03"}]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            reg = VersionRegistry(path)
            lines = reg.snapshot_lines()
            assert len(lines) == 1
            assert "GB50015-2019" in lines[0]
            assert "现行" in lines[0]

    def test_status_from_prod_json(self) -> None:
        """data/versions.json 只录事实：四本已入库规范有记录，但现行状态尚未人工核实 → unknown。

        人工核实（公告号/生效日期）并填入 source 后，把这里的断言改为 STATUS_CURRENT。
        """
        reg = get_version_registry()
        assert len(reg.list_all()) >= 4  # 四本已入库规范
        for spec_id in ("GB50015", "GB50013", "GB50014", "GB50318"):
            status = reg.get_spec_status(spec_id)
            assert status["version"], f"{spec_id} 应在 data/versions.json 中有记录（含版本号）"
            assert status["status"] == STATUS_UNKNOWN, f"{spec_id} 未人工核实前必须为 unknown"
            assert status["source"], "即使 unknown 也要写明为什么未知/如何核实"
            assert status["status"] != STATUS_CURRENT
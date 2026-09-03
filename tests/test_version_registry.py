"""Step 8 测试：规范版本注册表。

覆盖：基本查询 / 版本号拆分 / 未知条目返回 None / 录入条目可判定状态：
- "GB50015" 可查到现行状态
- "GB50015-2019" 可查到版本匹配记录
- "GB99999" 返回 None（unknown）
- 空 spec_id 返回 None
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.ingestion.version_registry import (
    STATUS_CURRENT,
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


class TestVersionRegistry:
    def test_empty_registry_returns_none(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nonexistent.json"
            reg = VersionRegistry(path)
            assert reg.get_spec_status("GB50015") is None

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
            assert reg.get_spec_status("GB50015") is not None
            assert reg.get_spec_status("GB50015")["status"] == "current"
            assert reg.get_spec_status("GB50014") is not None

    def test_unknown_spec_returns_none(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            data = [{"spec_id": "GB50015", "version": "2019", "status": "current", "source": "test", "verified_at": "2026-09-03"}]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            reg = VersionRegistry(path)
            assert reg.get_spec_status("GB99999") is None

    def test_source_id_query(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            data = [{"spec_id": "GB50015", "version": "2019", "status": "current", "source": "test", "verified_at": "2026-09-03"}]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            reg = VersionRegistry(path)
            result = reg.get_status_for_source_id("GB50015-2019 5.4.2")
            assert result is not None
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
        """从 data/versions.json 验证生产数据的条目可读。"""
        reg = get_version_registry()
        assert len(reg.list_all()) >= 4  # 四本已入库规范
        # 每本都在实际语料库中（GB50015/GB50013/GB50014/GB50318）
        for spec_id in ("GB50015", "GB50013", "GB50014", "GB50318"):
            status = reg.get_spec_status(spec_id)
            assert status is not None, f"{spec_id} 应在 data/versions.json 中有记录"
            assert status["status"] == STATUS_CURRENT, f"{spec_id} 应为现行"
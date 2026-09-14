"""Step 8 测试：规范版本注册表。

覆盖：基本查询 / 版本号拆分 / 未知条目显式返回 status=unknown / 录入条目可判定状态 /
只录事实（无 source 或 verified_at 的非 unknown 条目加载时降级 unknown）/ 多版本优先 current。

2026-09-08 加固（`TestPlaceholderSourceGuard`）：旧守卫只查"字段是否为空"，
放行了自述"未核实"的占位文本 —— 实测把生产 json 四条改成 current 而 source 仍是模板
占位符时，四条全部返回 current。那等于用一句"未核实"支撑了铁律②。本组锁死修正后的行为。
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from water_agent.ingestion.version_registry import (
    STATUS_CURRENT,
    STATUS_UNKNOWN,
    UNVERIFIED_MARKERS,
    VersionRegistry,
    get_version_registry,
    split_spec_id,
    unknown_status,
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
            # 夹具是一条**合法的已核实记录**：current 必须带 effective_date
            # （2026-09-08 加固；缺它会被降级 unknown）。
            data = [
                {"spec_id": "GB50015", "version": "2019", "status": "current",
                 "effective_date": "2020-03-01", "source": "test", "verified_at": "2026-09-03"},
                {"spec_id": "GB50014", "version": "2021", "status": "current",
                 "effective_date": "2021-10-01", "source": "test", "verified_at": "2026-09-03"},
            ]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            reg = VersionRegistry(path)
            assert reg.get_spec_status("GB50015")["status"] == "current"
            assert reg.get_spec_status("GB50014")["status"] == "current"

    def test_unknown_spec_returns_unknown_status(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            data = [{"spec_id": "GB50015", "version": "2019", "status": "current",
                     "effective_date": "2020-03-01", "source": "test", "verified_at": "2026-09-03"}]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            reg = VersionRegistry(path)
            assert reg.get_spec_status("GB99999")["status"] == STATUS_UNKNOWN
            assert reg.get_spec_status("")["status"] == STATUS_UNKNOWN

    def test_source_id_query(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "versions.json"
            data = [{"spec_id": "GB50015", "version": "2019", "status": "current",
                     "effective_date": "2020-03-01", "source": "test", "verified_at": "2026-09-03"}]
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
        """data/versions.json 只录事实：四本已入库规范有记录。

        2026-09-14 更新：四本规范均已核实为 current（住建部公告核实完成）。

        2026-09-08 补充：这条断言**不是**只看 status 字段。曾出现过把 status 改成
        current 但 source 仍是"未核实…"占位符的半成品状态（当时守卫放行了它），
        所以这里同时断言"若声明非 unknown，则必须真的有可复核来源"。
        """
        reg = get_version_registry()
        assert len(reg.list_all()) >= 4  # 四本已入库规范
        for spec_id in ("GB50015", "GB50013", "GB50014", "GB50318"):
            status = reg.get_spec_status(spec_id)
            assert status["version"], f"{spec_id} 应在 data/versions.json 中有记录（含版本号）"
            # 2026-09-14：规范版本已核实，期望为 current
            assert status["status"] == STATUS_CURRENT, (
                f"{spec_id} 已核实为 current（见 data/versions.json 的 source 与 verified_at）"
            )
            assert status["source"], "current 状态必须有住建部公告等来源依据"
            assert status["effective_date"], "current 状态必须有实施日期"

    def test_prod_json_has_no_half_verified_entry(self) -> None:
        """生产 json 不得存在"声明 current 但依据是占位符"的半成品条目。

        这是 2026-09-08 实际踩到的状态：改了 status、漏改 source/effective_date。
        守卫现在会降级它并记 demotion_reason —— 有 reason 就说明有人改了 status
        却没补依据，应当立刻发现而不是等到引用链出问题。
        """
        reg = get_version_registry()
        demoted = reg.demoted_records()
        assert demoted == [], (
            "data/versions.json 存在被降级的条目（改了 status 但依据不足）："
            f"{[(d['spec_id'], d['demotion_reason']) for d in demoted]}"
        )


class TestPlaceholderSourceGuard:
    """2026-09-08 加固：`source` 自述未核实 == 没有来源。

    旧守卫 `if not source or not verified_at` 只拦"字段为空"。模板里预置的
    "未核实：…人工核实后改为 current 并填 source"是**非空字符串**，于是
    `status: current` 原样通过 —— 系统对外声称规范现行有效，依据是一句"未核实"。
    模板占位 + 只改 status 是最自然的操作顺序，所以这不是理论风险。
    """

    def _record(self, path: Path, **overrides: object) -> VersionRegistry:
        entry: dict[str, object] = {
            "spec_id": "GB50015",
            "version": "2019",
            "status": "current",
            "effective_date": "2020-03-01",
            "source": "住建部公告 2019 年第 x 号",
            "verified_at": "2026-09-08",
        }
        entry.update(overrides)
        _write(path, [entry])
        return VersionRegistry(path)

    def test_fully_verified_entry_is_accepted(self) -> None:
        """先证明"齐全的记录能通过" —— 否则下面的反例可能只是被别的原因拦住。"""
        with TemporaryDirectory() as tmpdir:
            reg = self._record(Path(tmpdir) / "v.json")
            status = reg.get_spec_status("GB50015")
            assert status["status"] == STATUS_CURRENT
            assert status["demotion_reason"] == ""

    @pytest.mark.parametrize("marker", UNVERIFIED_MARKERS)
    def test_every_unverified_marker_is_rejected(self, marker: str) -> None:
        """逐个标记断言，而不是只测一个 —— 只测"未核实"会让新增标记形同虚设。"""
        with TemporaryDirectory() as tmpdir:
            reg = self._record(
                Path(tmpdir) / "v.json", source=f"{marker}：等查到公告号再补"
            )
            status = reg.get_spec_status("GB50015")
            assert status["status"] == STATUS_UNKNOWN
            assert "未核实" in status["demotion_reason"] or "自述" in status["demotion_reason"]

    def test_the_exact_production_placeholder_is_rejected(self) -> None:
        """用**生产 json 里的原文**做反例：这是实测放行过的那条字符串。"""
        placeholder = (
            "未核实：data/curated/clauses/GB50015-2019.jsonl 仅证明该版本已入库，"
            "不证明现行/废止；人工核实（公告号/生效日期）后改为 current 并填 source"
        )
        with TemporaryDirectory() as tmpdir:
            reg = self._record(Path(tmpdir) / "v.json", source=placeholder)
            assert reg.get_spec_status("GB50015")["status"] == STATUS_UNKNOWN

    def test_marker_match_is_case_insensitive(self) -> None:
        with TemporaryDirectory() as tmpdir:
            reg = self._record(Path(tmpdir) / "v.json", source="TODO: fill in later")
            assert reg.get_spec_status("GB50015")["status"] == STATUS_UNKNOWN

    def test_current_without_effective_date_is_demoted(self) -> None:
        """一本"现行有效"却没有生效日期，在工程上说不通。"""
        with TemporaryDirectory() as tmpdir:
            reg = self._record(Path(tmpdir) / "v.json", effective_date=None)
            status = reg.get_spec_status("GB50015")
            assert status["status"] == STATUS_UNKNOWN
            assert "effective_date" in status["demotion_reason"]

    def test_superseded_does_not_require_effective_date(self) -> None:
        """只对 current 强制生效日期：superseded/deprecated 的关键事实是 successor。

        对它们也强制会把"已知被取代但不知原生效日"的真实记录挡在门外。
        """
        with TemporaryDirectory() as tmpdir:
            reg = self._record(
                Path(tmpdir) / "v.json",
                status="superseded",
                effective_date=None,
                successor="GB50015-2025",
            )
            assert reg.get_spec_status("GB50015")["status"] == "superseded"

    def test_demotion_reason_surfaces_in_snapshot(self) -> None:
        """降级必须**可见**：快照进提示词，静默降级会让人以为核实已生效。"""
        with TemporaryDirectory() as tmpdir:
            reg = self._record(Path(tmpdir) / "v.json", source="未核实")
            line = reg.snapshot_lines()[0]
            assert "已降级" in line
            assert "状态未知" in line

    def test_demoted_entry_does_not_leak_claimed_status(self) -> None:
        """降级后不得在任何输出里仍显示 current —— 那会让读者误信它现行有效。"""
        with TemporaryDirectory() as tmpdir:
            reg = self._record(Path(tmpdir) / "v.json", source="待核实")
            assert "现行" not in reg.snapshot_lines()[0]
            assert reg.list_all()[0]["status"] == STATUS_UNKNOWN

    def test_unknown_status_helper_stays_isomorphic(self) -> None:
        """未录入与被降级两条路径的返回形状必须一致（下游按固定键取值）。"""
        with TemporaryDirectory() as tmpdir:
            reg = self._record(Path(tmpdir) / "v.json", source="未核实")
            demoted = reg.get_spec_status("GB50015")
            assert set(demoted) == set(unknown_status("GB50015", "2019"))

    def test_legitimate_source_containing_no_marker_passes(self) -> None:
        """误伤保护：真实来源文本不得因为"看起来像备注"被拦。"""
        with TemporaryDirectory() as tmpdir:
            reg = self._record(
                Path(tmpdir) / "v.json",
                source="住建部公告 2019 年第 108 号，2020-03-01 实施；查询于 openstd.samr.gov.cn",
            )
            assert reg.get_spec_status("GB50015")["status"] == STATUS_CURRENT
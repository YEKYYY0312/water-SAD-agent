"""P6-S1 Step 1：建筑类型规则表的契约与来源分级。

守住：
- 规则表用 pydantic ``extra="forbid"`` 校验，每条规则都带 ``basis``；
- ``formal_clause`` / ``informal_summary`` 的 source_id 真实在库、可检索，
  ``corpus_quote`` 是那条语料的逐字片段；``formal_clause`` 的原文不得自述"非正式"；
- ``engineering_practice`` 不得携带 source_id，图面说明里不得出现任何规范号；
- 语料事实：GB50015-2019 在库条目全部自述非正式（若哪天补入正式条文，本组会红，
  提醒"流速/防倒流规则可以升级了"，而不是缺陷）。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from water_agent.domain.models import make_source_id
from water_agent.drawing.building_rules import (
    RULES_PATH,
    BuildingRulesError,
    BuildingType,
    FittingKind,
    load_building_rules,
    parse_building_rules,
)
from water_agent.drawing.rules import FORBIDDEN_IN_INFORMAL, EvidenceLevel

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUSES_DIR = REPO_ROOT / "data" / "curated" / "clauses"
_WHITESPACE = re.compile(r"\s+")
_STANDARD_CODE = re.compile(r"GB\s*/?\s*T?\s*5\d{4}", re.IGNORECASE)


def _raw() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _corpus_records() -> dict[str, list[dict]]:
    """source_id → 全部记录（**不按 article 去重**：同一 article 可能有多个变体）。"""

    records: dict[str, list[dict]] = {}
    for path in sorted(CLAUSES_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            source_id = make_source_id(row["spec_id"], row["article"])
            records.setdefault(source_id, []).append(row)
    return records


def _compact(text: str) -> str:
    return _WHITESPACE.sub("", text or "")


class TestContract:
    def test_shipped_table_loads(self) -> None:
        rules = load_building_rules()
        assert rules.rules_version
        assert set(rules.building_types) == set(BuildingType)

    def test_every_rule_declares_its_basis(self) -> None:
        for rule in load_building_rules().all_rules():
            assert isinstance(rule.basis, EvidenceLevel), rule.rule_id
            assert rule.basis_note.strip(), f"{rule.rule_id} 没写依据说明"

    def test_unknown_keys_are_rejected(self) -> None:
        payload = _raw()
        payload["building_types"]["住宅"]["grid"]["bay_z_mm"] = 3000
        with pytest.raises(BuildingRulesError):
            parse_building_rules(payload)

    def test_missing_basis_is_rejected(self) -> None:
        payload = _raw()
        del payload["building_types"]["办公"]["routing"]["basis"]
        with pytest.raises(BuildingRulesError):
            parse_building_rules(payload)

    def test_engineering_practice_cannot_carry_a_source_id(self) -> None:
        payload = _raw()
        payload["building_types"]["住宅"]["grid"]["source_id"] = "GB50015-2019 3.7.1"
        payload["building_types"]["住宅"]["grid"]["corpus_quote"] = "生活给水设计秒流量"
        with pytest.raises(BuildingRulesError, match="engineering_practice"):
            parse_building_rules(payload)

    def test_formal_clause_must_carry_a_source_id_and_quote(self) -> None:
        payload = _raw()
        payload["building_types"]["商业"]["fittings"]["basis"] = "formal_clause"
        with pytest.raises(BuildingRulesError, match="source_id"):
            parse_building_rules(payload)

    def test_all_three_building_types_are_required(self) -> None:
        payload = _raw()
        del payload["building_types"]["商业"]
        with pytest.raises(BuildingRulesError, match="商业"):
            parse_building_rules(payload)

    def test_related_rules_must_exist(self) -> None:
        payload = _raw()
        payload["building_types"]["住宅"]["detail"]["related_rules"] = ["ws.no_such_rule"]
        with pytest.raises(BuildingRulesError):
            parse_building_rules(payload)

    def test_default_riser_position_must_be_listed(self) -> None:
        payload = _raw()
        payload["building_types"]["办公"]["riser"]["default_position"] = "楼梯间"
        with pytest.raises(BuildingRulesError):
            parse_building_rules(payload)

    def test_only_pump_room_detail_may_contain_a_pump(self) -> None:
        payload = copy.deepcopy(_raw())
        payload["building_types"]["住宅"]["detail"]["components"].append({"kind": "水泵", "label": "水泵"})
        with pytest.raises(BuildingRulesError):
            parse_building_rules(payload)


class TestSpecD2:
    """spec D2 拍板的三种建筑类型差异，必须在规则表里如实存在。"""

    def test_riser_positions_follow_spec(self) -> None:
        rules = load_building_rules()
        assert set(rules.for_type(BuildingType.RESIDENTIAL).riser.positions) == {"楼梯间", "管道井"}
        assert set(rules.for_type(BuildingType.OFFICE).riser.positions) == {"核心筒"}
        assert set(rules.for_type(BuildingType.COMMERCIAL).riser.positions) == {"边角"}

    def test_routing_methods_follow_spec(self) -> None:
        rules = load_building_rules()
        assert rules.for_type(BuildingType.RESIDENTIAL).routing.method == "沿墙"
        assert rules.for_type(BuildingType.OFFICE).routing.method == "沿走廊吊顶"
        assert rules.for_type(BuildingType.COMMERCIAL).routing.method == "沿柱网"

    def test_detail_types_follow_spec(self) -> None:
        rules = load_building_rules()
        assert rules.for_type(BuildingType.RESIDENTIAL).detail.detail_type == "水表井"
        assert rules.for_type(BuildingType.OFFICE).detail.detail_type == "吊顶内阀门组"
        assert rules.for_type(BuildingType.COMMERCIAL).detail.detail_type == "泵房接管"

    def test_residential_default_grid_is_the_spec_default(self) -> None:
        grid = load_building_rules().for_type(BuildingType.RESIDENTIAL).grid
        assert (grid.bay_x_mm, grid.bay_y_mm) == (3600, 3600)

    def test_every_unit_branch_has_a_valve(self) -> None:
        for building_type in BuildingType:
            fittings = load_building_rules().for_type(building_type).fittings
            assert fittings.unit_valve in {FittingKind.GATE_VALVE, FittingKind.GLOBE_VALVE}


class TestTraceability:
    """铁律②：能指回语料的规则必须真能指回；指不回的不得假装能。"""

    def test_cited_source_ids_exist_in_the_curated_corpus(self) -> None:
        corpus = _corpus_records()
        cited = [r for r in load_building_rules().all_rules() if r.source_id is not None]
        assert cited, "规则表里至少有一条可指回语料的规则"
        for rule in cited:
            assert rule.source_id in corpus, f"{rule.rule_id} 的 {rule.source_id} 不在 data/curated/clauses/"
            assert any(
                row.get("ingestion_decision") == "include" for row in corpus[rule.source_id]
            ), f"{rule.source_id} 在库但不可检索（ingestion_decision != include）"

    def test_formal_clause_source_ids_are_in_the_corpus(self) -> None:
        """执行 prompt Step 1 的验收断言：所有 formal_clause 的 source_id 都在库。"""

        corpus = _corpus_records()
        formal = [r for r in load_building_rules().all_rules() if r.basis is EvidenceLevel.FORMAL_CLAUSE]
        assert formal
        for rule in formal:
            assert rule.source_id in corpus, rule.rule_id

    def test_corpus_quote_is_verbatim(self) -> None:
        corpus = _corpus_records()
        for rule in load_building_rules().all_rules():
            if rule.source_id is None:
                continue
            texts = [_compact(row.get("text", "")) for row in corpus[rule.source_id]]
            assert any(_compact(rule.corpus_quote or "") in text for text in texts), (
                f"{rule.rule_id} 的 corpus_quote 不是 {rule.source_id} 的原文片段"
            )

    def test_formal_clauses_are_not_self_declared_informal(self) -> None:
        corpus = _corpus_records()
        for rule in load_building_rules().all_rules():
            if rule.basis is not EvidenceLevel.FORMAL_CLAUSE:
                continue
            quoted = [
                row for row in corpus[rule.source_id or ""]
                if _compact(rule.corpus_quote or "") in _compact(row.get("text", ""))
            ]
            assert quoted
            assert all("非正式" not in row["text"] for row in quoted), (
                f"{rule.rule_id} 引的是自述非正式的综述，不得标 formal_clause"
            )

    def test_informal_summaries_really_are_informal(self) -> None:
        """反方向：标成 informal_summary 的，原文确实自述非正式——否则是降级漏报。"""

        corpus = _corpus_records()
        for rule in load_building_rules().all_rules():
            if rule.basis is EvidenceLevel.INFORMAL_SUMMARY:
                assert any("非正式" in row["text"] for row in corpus[rule.source_id or ""]), rule.rule_id

    def test_gb50015_in_corpus_is_entirely_informal(self) -> None:
        """语料事实（2026-09-29 实测）：GB50015-2019 在库 9 条全部自述非正式。

        执行 prompt 以为只有 4.7.1 如此。若本断言失败，说明补入了正式条文——
        流速与防倒流两条规则可以重新定级。
        """

        rows = [
            json.loads(line)
            for line in (CLAUSES_DIR / "GB50015-2019.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 9
        assert all("非正式规范条文" in row["text"] for row in rows)


class TestRenderedNotes:
    """图面说明的唯一出口：`RuleText.rendered_note()`。"""

    @pytest.mark.parametrize(
        "rule",
        [r for r in load_building_rules().all_rules() if r.basis is not EvidenceLevel.FORMAL_CLAUSE],
        ids=lambda rule: rule.rule_id,
    )
    def test_non_formal_notes_never_claim_compliance(self, rule) -> None:
        note = rule.rendered_note()
        for phrase in FORBIDDEN_IN_INFORMAL:
            assert phrase not in note, f"{rule.rule_id}：{phrase!r}"
        assert "需人工确认" in note

    @pytest.mark.parametrize(
        "rule",
        [r for r in load_building_rules().all_rules() if r.basis is EvidenceLevel.ENGINEERING_PRACTICE],
        ids=lambda rule: rule.rule_id,
    )
    def test_engineering_practice_notes_cite_no_standard(self, rule) -> None:
        """工程惯例的图面说明不得出现任何规范号（不得写"按 GB50015 第 x.x.x 条"）。"""

        assert not _STANDARD_CODE.search(rule.rendered_note()), rule.rule_id
        assert "无条文支撑" in rule.rendered_note()

    def test_informal_note_cites_only_its_own_source(self) -> None:
        rules = load_building_rules()
        note = rules.velocity.rendered_note()
        assert "（GB50015-2019 3.7.3）" in note
        assert "非正式条文" in note
        assert _STANDARD_CODE.findall(note) == ["GB50015"]

    def test_formal_note_carries_its_scope_caveat(self) -> None:
        note = load_building_rules().pump_discharge_valves.rendered_note()
        assert note.startswith("[依据正式条文（GB50013-2018 6.3.5）]")
        assert "须经设计人确认适用性" in note

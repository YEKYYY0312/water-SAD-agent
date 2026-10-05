"""设计依据的引用校验不再自证（review 2026-09-30 缺陷"引用校验是自证"的回归）。

此前 Design Agent 的 ``_citation_candidates`` 调用 ``validate_citations(candidates, candidates,
version_registry=None)``：被引集合就是"检索"集合本身，invalid 恒为空；编排器也从不传版本注册表，
版本状态恒为 None。于是：
- 检索集合里塞进 ``TOTALLY-FAKE-9999 9.9.9`` 这样的条款，它照样进设计依据，版本状态是 None，
  审查与审批都看不到任何提示，run succeeded；
- CLI 固定传 ``retrieved_source_ids=[]``，设计零依据也一路 succeeded，没有任何地方说"没有依据"。

口径：
- 被引候选（用户在 user_constraints 里请求的 source_id；没有请求时取检索集合全体）对照
  真实检索到的集合（本轮 ``retrieved_source_ids`` ∪ 引用校验器已交入的 claims）用
  ``validate_citations`` 校验，并接入 ``VersionRegistry``（编排器缺省用
  ``get_version_registry()``）；不在检索集合里的候选进 ``invalid_citation_ids``，不进任何管段。
- 版本状态按既有语义只标记、不判失败（任务书 §7.3"规范版本 unknown → 改写为未知状态并披露"）：
  非 current 的依据、未经注册表校验的依据、以及设计零引用，都写进审查包的
  ``disclosed_limitations``，随审批请求与审批摘要交给批准人。
- 编排器内没有检索步骤（范围外）：``retrieved_source_ids`` 仍由调用方提供，条款是否真实存在
  只能由真实检索保证；这里能做的是把注册表查不到的规范如实标成 unknown 并披露。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from water_agent.domain.citation import CitationClaim
from water_agent.multi_agent.agents.design import run_design_agent
from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
from water_agent.multi_agent.agents.review import collect_disclosed_limitations
from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    DesignPackage,
    ProjectBrief,
    ReviewPackage,
)
from water_agent.multi_agent.orchestrator import build_orchestrator_graph, default_multi_agent_state
from water_agent.session import default_checkpoint_serde

_FAKE = "TOTALLY-FAKE-9999 9.9.9"
_REAL = "GB50015-2019 3.2.1"  # data/versions.json：GB50015-2019 已核实为 current
_CLI_QUERY = "给水系统，Q=12 L/s，DN200，L=25m，材质=steel，起点=n10，终点=n11，输出=图纸，求流速"
_PROMPT = "批准发布最终草图？[y/n]"


def _brief(**overrides: Any) -> dict[str, Any]:
    brief = {
        "project_id": "proj-citation",
        "request": "给水管段设计，DN150 PE 管 10m，流量 10L/s",
        "systems": ["water_supply"],
        "unit_system": "SI",
        "hydraulic_cases": [
            {"case_id": "case-vel-1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}
        ],
        "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        "spatial_constraints": [{"kind": "material", "value": "PE"}],
        "required_outputs": ["drawing"],
        "user_constraints": ["material: PE", "DN150"],
    }
    brief.update(overrides)
    return brief


def _pause(tmp_path: Path, name: str, retrieved: list[str], **overrides: Any) -> dict[str, Any]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(connection, serde=default_checkpoint_serde()))
        return dict(
            app.invoke(
                default_multi_agent_state(
                    project_brief=_brief(**overrides),
                    retrieved_source_ids=retrieved,
                    staging_dir=str(tmp_path / name / "staging"),
                    final_dir=str(tmp_path / name / "final"),
                    output_file_id=name,
                ),
                config={"configurable": {"thread_id": name}},
            )
        )
    finally:
        connection.close()


def _active(state: dict[str, Any], role: str) -> ArtifactEnvelope:
    return ArtifactEnvelope.model_validate(state["artifacts"][state["active_artifact_ids"][role]])


def _design(state: dict[str, Any]) -> DesignPackage:
    return DesignPackage.model_validate(_active(state, "design").payload)


def _review(state: dict[str, Any]) -> ReviewPackage:
    return ReviewPackage.model_validate(_active(state, "review").payload)


def test_orchestrated_design_citations_carry_the_registry_version_status(tmp_path: Path) -> None:
    paused = _pause(tmp_path, "cit-real", [_REAL])
    assert paused.get("__interrupt__")
    design = _design(paused)
    assert [c.source_id for c in design.citations] == [_REAL]
    assert design.citations[0].version_status == "current", "编排器没把版本注册表接进设计 Agent"
    summary = paused["__interrupt__"][0].value["summary"]
    assert summary["citations"] == [{"source_id": _REAL, "version_status": "current"}]
    assert not any(_REAL in item for item in _review(paused).disclosed_limitations)


def test_unregistered_spec_from_retrieval_is_marked_unknown_and_disclosed_before_approval(tmp_path: Path) -> None:
    paused = _pause(tmp_path, "cit-fake", [_FAKE])
    assert paused.get("__interrupt__")
    design_env = _active(paused, "design")
    design = DesignPackage.model_validate(design_env.payload)
    assert design.citations and design.citations[0].source_id == _FAKE
    assert design.citations[0].version_status == "unknown"
    assert any(_FAKE in warning for warning in design_env.warnings)

    review_disclosures = [item for item in _review(paused).disclosed_limitations if _FAKE in item]
    assert review_disclosures and "无法确认现行有效" in review_disclosures[0]
    request = paused["__interrupt__"][0].value
    assert any(_FAKE in item for item in request["disclosed_limitations"])
    assert {"source_id": _FAKE, "version_status": "unknown"} in request["summary"]["citations"]


def test_zero_citation_design_is_disclosed_in_review_and_in_the_approval_request(tmp_path: Path) -> None:
    paused = _pause(tmp_path, "cit-zero", [])
    assert paused.get("__interrupt__"), "零依据仍可带条件进入人审，但必须披露"
    assert _design(paused).citations == []
    assert any("设计依据零引用" in item for item in _review(paused).disclosed_limitations)
    request = paused["__interrupt__"][0].value
    assert any("设计依据零引用" in item for item in request["disclosed_limitations"])
    assert any("设计依据零引用" in item for item in request["summary"]["review"]["disclosed_limitations"])


def test_cli_zero_citation_disclosure_is_visible_before_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from water_agent import cli

    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    code = cli.main(["--flow", "multi-agent", "--json", "--session-dir", str(tmp_path / "sessions"), _CLI_QUERY])
    captured = capsys.readouterr()
    assert code == 0, f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
    before_prompt = captured.err[: captured.err.index(_PROMPT)]
    assert "设计依据零引用" in before_prompt


def _calc_env():
    brief = ProjectBrief.model_validate(_brief())
    return brief, run_hydraulic_agent(brief)


class _Registry:
    """Stub VersionRegistry recording which specs were looked up."""

    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses
        self.calls: list[str] = []

    def get_spec_status(self, spec_id: str) -> dict[str, str]:
        self.calls.append(spec_id)
        return {"status": self.statuses.get(spec_id, "unknown")}


def test_design_validates_candidates_against_retrieval_with_the_given_registry() -> None:
    brief, calc_env = _calc_env()
    registry = _Registry({"GB50015-2019": "superseded", "ODD-1": "not-a-status"})
    env = run_design_agent(
        brief,
        calc_env,
        retrieved_source_ids=[_REAL, "ODD-1 1.1"],
        version_registry=registry,
    )
    design = DesignPackage.model_validate(env.payload)
    statuses = {c.source_id: c.version_status for c in design.citations}
    assert statuses == {_REAL: "superseded", "ODD-1 1.1": "unknown"}  # 注册表外的取值归一为 unknown
    assert {"GB50015-2019", "ODD-1"} <= set(registry.calls)
    assert env.status == "ready"  # 版本状态只标记、不判失败
    disclosed = collect_disclosed_limitations({calc_env.artifact_id: calc_env, env.artifact_id: env})
    assert any(_REAL in item and "superseded" in item for item in disclosed)


def test_review_flags_citations_whose_version_was_never_checked() -> None:
    brief, calc_env = _calc_env()
    env = run_design_agent(brief, calc_env, retrieved_source_ids=[_REAL])  # 直接调用、未给注册表
    assert DesignPackage.model_validate(env.payload).citations[0].version_status is None
    disclosed = collect_disclosed_limitations({calc_env.artifact_id: calc_env, env.artifact_id: env})
    assert any(_REAL in item and "未经版本注册表校验" in item for item in disclosed)


def test_requested_citation_outside_the_retrieval_set_never_enters_the_design_basis() -> None:
    """守护（修复前后都成立）：请求的条款不在检索集合 ∪ 校验器已确认引用里，就不进任何管段。"""
    brief, calc_env = _calc_env()
    requested = ProjectBrief.model_validate(
        _brief(user_constraints=["material: PE", "DN150", f"source_ids: {_REAL}, GB50013-2018 6.3.3, NOT-RETRIEVED 1.1"])
    )
    env = run_design_agent(
        requested,
        calc_env,
        retrieved_source_ids=[_REAL],
        validated_citations=[CitationClaim(source_id="GB50013-2018 6.3.3", declared_by="model")],
        version_registry=_Registry({"GB50015-2019": "current", "GB50013-2018": "current"}),
    )
    design = DesignPackage.model_validate(env.payload)
    assert design.invalid_citation_ids == ["NOT-RETRIEVED 1.1"]
    assert design.segments[0].source_ids == [_REAL, "GB50013-2018 6.3.3"]
    assert all("NOT-RETRIEVED" not in c.source_id for c in design.citations)
    assert env.status == "blocked"

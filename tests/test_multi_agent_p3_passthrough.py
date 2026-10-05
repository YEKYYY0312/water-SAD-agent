"""多 Agent 审查透传 P3 结论与水力越界提示（review 2026-09-30）。

此前 ``drawing_executor.verify_roundtrip`` 只保存 P3 ``review.summary()`` 一个字符串，
findings / not_checked / pipes_reviewed 全部丢掉；水力 Agent 的 envelope ``assumptions=[]``，
审查 Agent 也不看 assumptions。于是：
- 排水 DN200（< 工程惯例最小 DN300）P3 报"1 条待核问题 + 3 项未检查"，多 Agent 审查
  却是 ``pass / issues=[] / not_checked=[]``，运行 succeeded 并导出；
- DN50 走 50 L/s 算出 25.46 m/s，审查照样 pass、导出；
- 纯给水图 P3 明说"未对任何管段执行规则判定"，审查仍 pass；
- 人工批准时只看到一个哈希（盲批）。

口径（用户 2026-09-30 选定"带条件进人审"，任务书 §5.6 的"明确规则"）：
- P3 **缺陷** → ReviewIssue（owner=human；正式条文 error，其余 warning）→ 运行停在 waiting；
- P3 **覆盖缺口**（未检查项）与**水力越界提示** → ``ReviewPackage.disclosed_limitations``，
  写进审批请求，批准前逐条展示；不影响 decision，但与批准绑定在同一个审查包哈希上。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from water_agent.multi_agent.contracts import ReviewPackage
from water_agent.multi_agent.orchestrator import build_orchestrator_graph, default_multi_agent_state
from water_agent.session import default_checkpoint_serde


def _brief(pid: str, **overrides) -> dict:
    brief = {
        "project_id": pid,
        "request": "p3 passthrough",
        "systems": ["water_supply"],
        "unit_system": "SI",
        "hydraulic_cases": [{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 10, "diameter_mm": 150}}],
        "topology": [{"start_node_id": "n1", "end_node_id": "n2", "length_m": 10.0}],
        "spatial_constraints": [{"kind": "material", "value": "PE"}],
        "required_outputs": ["drawing"],
        "user_constraints": [],
    }
    brief.update(overrides)
    return brief


def _run(tmp_path: Path, name: str, brief: dict, *, approve: bool | None = True):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    app = build_orchestrator_graph(checkpointer=SqliteSaver(connection, serde=default_checkpoint_serde()))
    config = {"configurable": {"thread_id": name}}
    staging, final = tmp_path / name / "staging", tmp_path / name / "final"
    staging.mkdir(parents=True)
    final.mkdir(parents=True)
    paused = dict(
        app.invoke(
            default_multi_agent_state(
                project_brief=brief,
                retrieved_source_ids=[],
                staging_dir=str(staging),
                final_dir=str(final),
                output_file_id=name,
            ),
            config=config,
        )
    )
    interrupt = paused["__interrupt__"][0].value if paused.get("__interrupt__") else None
    result = paused
    if interrupt is not None and approve is not None:
        result = dict(app.invoke(Command(resume={"approved": approve, "reason": "test"}), config=config))
    return paused, interrupt, result


def _active(result: dict, role: str) -> dict:
    return result["artifacts"][result["active_artifact_ids"][role]]


def _review(result: dict) -> ReviewPackage:
    return ReviewPackage.model_validate(_active(result, "review")["payload"])


def test_p3_defect_stops_the_run_for_a_human(tmp_path: Path) -> None:
    """排水 DN200：P3 的最小管径发现必须成为人工问题，运行停在 waiting，不导出。"""
    brief = _brief(
        "p3-dn200",
        systems=["drainage"],
        hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 5, "diameter_mm": 200, "slope": 0.003}}],
    )
    _, interrupt, result = _run(tmp_path, "p3-dn200", brief)
    assert interrupt is None, "有 P3 缺陷时不得进入批准"
    assert result["status"] == "waiting"
    assert result.get("exported_file_path") is None
    review = _review(result)
    p3_issues = [issue for issue in review.issues if issue.rule_id.startswith("p3_")]
    assert p3_issues, review.issues
    assert all(issue.owner == "human" for issue in p3_issues)
    assert any("DN200" in issue.message for issue in p3_issues)
    # 坡度/流速等未检查项同样如实披露，不再是空列表
    assert any("未检查" in item or "缺" in item for item in review.disclosed_limitations)


def test_hydraulic_out_of_range_note_reaches_the_approval_request(tmp_path: Path) -> None:
    """DN50 走 50 L/s（25.46 m/s）：越界提示必须写进审批请求与审查包，人看过才能批准。"""
    brief = _brief(
        "p3-v25",
        hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 50, "diameter_mm": 50}}],
    )
    _, interrupt, result = _run(tmp_path, "p3-v25", brief)
    assert interrupt is not None
    disclosed = interrupt["disclosed_limitations"]
    assert any("25.465 m/s 高于给水常用区间" in item for item in disclosed), disclosed
    assert result["status"] == "succeeded"
    assert any("25.465" in item for item in _review(result).disclosed_limitations)
    calc = _active(result, "calc")
    assert any("25.465" in note for note in calc["assumptions"]), "越界提示必须随 CalcPackage envelope 下传"


def test_water_supply_drawing_rule_gap_is_disclosed(tmp_path: Path) -> None:
    """纯给水图：P3 规则一条都不适用——批准前必须披露"给水管段未检查"，而不是静默 pass。"""
    _, interrupt, result = _run(tmp_path, "p3-ws", _brief("p3-ws"))
    assert interrupt is not None
    assert any("给水管段" in item and "未检查" in item for item in interrupt["disclosed_limitations"])
    assert result["status"] == "succeeded"


def test_roundtrip_review_keeps_the_structured_p3_result(tmp_path: Path) -> None:
    brief = _brief(
        "p3-rt",
        systems=["drainage"],
        hydraulic_cases=[{"case_id": "c1", "formula": "velocity", "inputs": {"flow_lps": 5, "diameter_mm": 200, "slope": 0.003}}],
    )
    _, _, result = _run(tmp_path, "p3-rt", brief)
    roundtrip = _active(result, "drawing")["payload"]["roundtrip_review"]
    assert roundtrip["pipes_reviewed"] >= 1
    assert roundtrip["p3_findings"], "P3 缺陷必须结构化保留，不能只剩一句摘要"
    assert roundtrip["p3_not_checked"]
    assert roundtrip["review_summary"]


def test_cli_lists_disclosures_before_asking_for_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """CLI 审批不再只给一个哈希：批准前逐条列出披露项，最终 JSON 也带上。"""
    import builtins

    from water_agent.cli import main

    answers = iter(["y"])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(answers))
    code = main(
        [
            "--flow", "multi-agent", "--json", "--session-dir", str(tmp_path / "sessions"),
            "给水系统，Q=12 L/s，DN200，L=25m，材质=steel，起点=n10，终点=n11，输出=图纸，求流速",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
    assert "低于给水常用区间" in captured.err, captured.err
    assert "给水管段" in captured.err
    payload = json.loads(captured.out)
    assert any("低于给水常用区间" in item for item in payload["disclosed_limitations"])

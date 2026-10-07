"""CLI 流程测试（全程离线 --mock-generator）：交互式真正作答、agentic 多轮同 thread、--flow docgen 人审闭环。"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from water_agent.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]


def _feed(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    it = iter(lines)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))


class TestInteractive:
    def test_interactive_agentic_answers_and_keeps_thread(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """--interactive --mode agentic：每行真正走 agentic 回答（不是"已收到第 N 条消息"），同一会话 ID。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        _feed(monkeypatch, ["给水管道的设计秒流量如何确定？", "污水管道最大充满度", "exit"])
        code = main(["--interactive", "--mode", "agentic", "--mock-generator", "--session-dir", str(tmp_path / "s"), "--json"])
        out = capsys.readouterr().out
        assert code == 0
        assert "已收到第" not in out
        rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        assert len(rows) == 2
        assert all(r["status"] == "succeeded" and r["answer"].startswith("根据规范检索结果") for r in rows)
        assert rows[0]["session_id"] == rows[1]["session_id"]
        # 占位模型会 cite 首条命中：引用带版本状态字段（FIX 未录入 → unknown）
        assert rows[0]["citations"] and rows[0]["citations"][0]["version_status"] == "unknown"

    def test_interactive_default_mode_routes_calc_and_rag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        _feed(monkeypatch, ["DN150、Q=20L/s，求流速", "给水管道的设计秒流量", "quit"])
        code = main(["--interactive", "--mock-generator", "--session-dir", str(tmp_path / "s"), "--trace-dir", str(tmp_path / "t")])
        out = capsys.readouterr().out
        assert code == 0
        assert "velocity_mps=1.132" in out
        assert "引用：" in out and "FIX 1.1.1" in out
        assert "已收到第" not in out


class TestDocgenCalcBookEndToEnd:
    """B3 收口 e2e：calc 意图 docgen → 计算书含入参+结果+设计依据（不再无条件空引用）。"""

    def test_calc_book_contains_inputs_results_and_design_basis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _feed(monkeypatch, ["y"])
        code = main([
            "--flow", "docgen", "--mock-generator", "--json",
            "--session-dir", str(tmp_path / "s"), "--trace-dir", str(tmp_path / "t"),
            "DN150、Q=20L/s，求流速，生成给水管道计算书",
        ])
        captured = capsys.readouterr()
        assert code == 0, captured.out
        payload = json.loads(captured.out)  # B4：stdout 只含最终 JSON，可直接 json.loads
        assert payload["status"] == "succeeded"
        final = payload["final_doc"]
        assert final.startswith("# 计算书\n")
        # 数值入参（输入口径）与结果同表
        assert "diameter_mm = 150 mm" in final
        assert "flow_lps = 20 L/s" in final
        assert "velocity_mps = 1.13177 m/s" in final
        # 设计依据反映 gather 实际检到的引用（mock RAG 命中 fixture），非"未声明"
        assert "FIX " in final, "calc 计算书设计依据必须来自检索（design basis）"
        assert "未声明规范依据" not in final
        # 公式口径备注可追溯
        assert "公式口径" in final
    def test_docgen_reject_then_approve_writes_doc_and_trace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """n（带原因）→ 重拟第 2 稿体现反馈 → y → 正式文档；审批记录进 trace。

        P2 收口：calc 意图的 docgen 会额外跑一次设计依据检索（另一个 trace 文件），
        本测试只挑 docgen 人审 trace（含 approval_decision 事件）断言。
        """
        _feed(monkeypatch, ["n", "补充管径表格", "y"])
        code = main([
            "--flow", "docgen", "--mock-generator",
            "--session-dir", str(tmp_path / "s"), "--trace-dir", str(tmp_path / "t"),
            "生成 DN150 给水管道计算书",
        ])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "第 1 稿" in out and "第 2 稿" in out
        assert "用户反馈（已吸收）" in out and "补充管径表格" in out
        assert "===== 正式文档 =====" in out and "状态: succeeded" in out
        traces = list((tmp_path / "t").glob("*.json"))
        assert traces
        flow_traces = [
            json.loads(t.read_text(encoding="utf-8"))
            for t in traces
            if any(e.get("event_type") == "approval_decision" for e in json.loads(t.read_text(encoding="utf-8"))["events"])
        ]
        assert len(flow_traces) == 1, "docgen 人审 trace 必须恰好一条"
        doc = flow_traces[0]
        decisions = [e for e in doc["events"] if e["event_type"] == "approval_decision"]
        assert [d["data"]["approved"] for d in decisions] == [False, True]
        assert decisions[0]["data"]["reason"] == "补充管径表格"
        assert doc["status"] == "succeeded"
        run_end = next(e for e in doc["events"] if e["event_type"] == "run_end")
        assert len(run_end["data"]["approval_log"]) == 2

    def test_docgen_gathers_citations_from_rag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        _feed(monkeypatch, ["y"])
        code = main([
            "--flow", "docgen", "--mock-generator", "--json",
            "--session-dir", str(tmp_path / "s"), "--trace-dir", str(tmp_path / "t"),
            "给水管道的设计秒流量",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["status"] == "succeeded"
        assert "FIX 1.1.1" in payload["final_doc"], "gather 应把 RAG 引用汇进草稿"

    def test_docgen_interrupted_session_resumes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """第一次在审批处 EOF 挂起（exit 0）；--resume 同一会话后 y 走完。"""
        def _eof(_prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        args = ["--flow", "docgen", "--mock-generator", "--session-dir", str(tmp_path / "s"), "--trace-dir", str(tmp_path / "t")]
        code = main([*args, "生成 DN150 给水管道计算书"])
        out = capsys.readouterr().out
        assert code == 0 and "审批挂起" in out
        session_id = out.split("--resume ")[1].split()[0]

        _feed(monkeypatch, ["y"])
        code = main([*args, "--resume", session_id, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["status"] == "succeeded" and payload["session_id"] == session_id

    def test_docgen_gather_calc_reports_missing_inputs(self, tmp_path: Path) -> None:
        """计算意图缺参：缺参信号必须透出到人审草稿（S9-03），审批人才能判断为何无数值。"""
        from water_agent.cli import _gather_docgen_inputs, build_parser

        args = build_parser().parse_args(
            [
                "--flow", "docgen", "--mock-generator",
                "--session-dir", str(tmp_path / "s"), "--trace-dir", str(tmp_path / "t"),
                "DN150 求流速",
            ]
        )
        gathered = _gather_docgen_inputs("DN150 求流速", args)
        assert gathered["citations"], "设计依据不再空跑：calc 意图也会检索设计依据引用"
        assert gathered["calc_result"]["missing_inputs"] == "flow_lps"
        # P2 收口：数值入参与结果同表（计算书需要展示输入口径 DN150）
        assert gathered["calc_result"]["diameter_mm"] == 150

    def test_docgen_json_output_remains_pure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """--json 模式下不应把交互提示污染到 stdout。"""
        _feed(monkeypatch, ["n", "补充设计依据", "y"])
        code = main([
            "--flow", "docgen", "--mock-generator", "--json",
            "--session-dir", str(tmp_path / "s"), "--trace-dir", str(tmp_path / "t"),
            "生成 DN150 给水管道计算书",
        ])
        captured = capsys.readouterr()
        assert code == 0
        assert "批准生成正式文档？" not in captured.out
        assert "拒绝原因" not in captured.out
        payload = json.loads(captured.out)
        assert payload["ok"] is True or payload["status"] == "succeeded"


class TestMultiAgentCli:
    def test_plain_language_returns_field_level_missing_inputs_without_staging(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """普通需求描述不能被替换成固定 DN150/PE/10m 工况。"""
        import tempfile

        def _unexpected_staging(*_args, **_kwargs):
            raise AssertionError("missing ProjectBrief must not create staging directories")

        def _unexpected_approval(_prompt: str = "") -> str:
            raise AssertionError("missing ProjectBrief must not request approval")

        monkeypatch.setattr(tempfile, "mkdtemp", _unexpected_staging)
        monkeypatch.setattr("builtins.input", _unexpected_approval)

        code = main(
            [
                "--flow",
                "multi-agent",
                "--json",
                "--session-dir",
                str(tmp_path / "sessions"),
                "请帮我做一个给排水项目方案",
            ]
        )

        captured = capsys.readouterr()
        assert code == 1, f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
        payload = json.loads(captured.out)
        assert payload["status"] == "missing_inputs"
        assert payload["waiting"] is True
        assert payload["error_code"] == "MULTI_MISSING_INPUTS"
        assert {
            "systems",
            "unit_system",
            "hydraulic_cases[0].formula",
            "topology[0].start_node_id",
            "topology[0].end_node_id",
            "topology[0].length_m",
            "spatial_constraints.material",
            "required_outputs",
        } <= set(payload["missing_inputs"])
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "DN150" not in serialized
        assert '"PE"' not in serialized
        assert '"flow_lps": 10' not in serialized

    def test_explicit_natural_language_brief_uses_observed_values_in_real_release(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """完整显式输入走真实 interrupt/resume/export，发布值必须来自输入。"""
        import ezdxf

        _feed(monkeypatch, ["y"])
        code = main(
            [
                "--flow",
                "multi-agent",
                "--json",
                "--session-dir",
                str(tmp_path / "sessions"),
                "给水系统，Q=12 L/s，DN200，L=25m，材质=steel，起点=n10，终点=n11，输出=图纸，求流速",
            ]
        )

        captured = capsys.readouterr()
        assert code == 0, f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
        payload = json.loads(captured.out)
        assert payload["status"] == "succeeded"
        assert payload["waiting"] is False
        brief = payload["project_brief"]
        assert brief["systems"] == ["water_supply"]
        assert brief["unit_system"] == "SI"
        assert brief["hydraulic_cases"][0]["formula"] == "velocity"
        assert brief["hydraulic_cases"][0]["inputs"]["flow_lps"] == 12
        assert brief["hydraulic_cases"][0]["inputs"]["diameter_mm"] == 200
        # 单系统、单工况：边不声明 system / calc_case_id，由设计 Agent 无歧义自动绑定
        assert brief["topology"] == [
            {
                "start_node_id": "n10",
                "end_node_id": "n11",
                "length_m": 25.0,
                "system": None,
                "calc_case_id": None,
            }
        ]
        assert brief["spatial_constraints"] == [{"kind": "material", "value": "steel"}]
        assert brief["required_outputs"] == ["drawing"]
        assert payload["retrieved_source_ids"] == []

        drawing_path = Path(payload["exported_file_path"])
        assert drawing_path.is_file()
        drawing = ezdxf.readfile(drawing_path)
        lines = [entity for entity in drawing.modelspace() if entity.dxftype() == "LINE"]
        assert len(lines) == 1
        start, end = lines[0].dxf.start, lines[0].dxf.end
        assert math.dist((start.x, start.y), (end.x, end.y)) == pytest.approx(25_000.0)
        labels = [
            entity.dxf.text
            for entity in drawing.modelspace()
            if entity.dxftype() == "TEXT"
        ]
        assert any("DN200 steel water_supply" in label for label in labels)

    def test_segment_id_whose_hex_reads_as_an_overflowing_number_still_reaches_release(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """CLI 偶发失败的确定性复现：project_id 含 uuid4 session_id，管段 ID 的 hex 偶尔形如 21634e590093。

        此前审查把材料统计"来源：seg-21634e590093(25m)"里的编号尾巴读成溢出数值，每轮都报同一条
        doc_numeric_preservation，三轮后 MULTI_RETRY_EXHAUSTED，到不了审批（exit 1、stderr 为空）。
        """
        monkeypatch.setattr("water_agent.cli.new_session_id", lambda: "000000000000400080000000000000a8")
        _feed(monkeypatch, ["y"])
        code = main(
            [
                "--flow",
                "multi-agent",
                "--json",
                "--session-dir",
                str(tmp_path / "sessions"),
                "给水系统，Q=12 L/s，DN200，L=25m，材质=steel，起点=n10，终点=n11，输出=图纸，求流速",
            ]
        )

        captured = capsys.readouterr()
        diagnostics = f"stdout:\n{captured.out}\nstderr:\n{captured.err}"
        payload = json.loads(captured.out)
        segments = (payload.get("approval_summary") or {}).get("segments") or []
        # 前提：这个 session 确实派生出会溢出的管段 ID；派生规则一改这里先红，换一个 session 即可
        assert [segment["segment_id"] for segment in segments] == ["seg-21634e590093"], diagnostics
        assert code == 0, diagnostics
        assert payload["status"] == "succeeded", diagnostics

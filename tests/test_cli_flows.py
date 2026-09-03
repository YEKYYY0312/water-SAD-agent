"""CLI 流程测试（全程离线 --mock-generator）：交互式真正作答、agentic 多轮同 thread、--flow docgen 人审闭环。"""
from __future__ import annotations

import json
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


class TestDocgenFlow:
    def test_docgen_reject_then_approve_writes_doc_and_trace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """n（带原因）→ 重拟第 2 稿体现反馈 → y → 正式文档；审批记录进 trace。"""
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
        assert len(traces) == 1
        doc = json.loads(traces[0].read_text(encoding="utf-8"))
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

    def test_docgen_gather_calc_reports_missing_inputs(self) -> None:
        """计算意图缺参：缺参信号必须透出到人审草稿（S9-03），审批人才能判断为何无数值。"""
        import argparse

        from water_agent.cli import _gather_docgen_inputs

        args = argparse.Namespace(mode="auto")
        gathered = _gather_docgen_inputs("DN150 求流速", args)
        assert gathered["citations"] == []
        assert gathered["calc_result"]["missing_inputs"] == "flow_lps"

"""DATA_ROOT 验证 CLI：退出码、JSON 输出、报告文件。"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.ingestion.quality_cli import main

VALID = {
    "spec_id": "GB50014-2021",
    "chapter": "4",
    "section": "4.4",
    "article": "4.4.2",
    "title": "污水管道水力计算",
    "text": "污水管道的最大设计充满度，应按下列规定取值。",
}


def _write(root: Path, lines: list[object]) -> None:
    clauses = root / "clauses"
    clauses.mkdir(parents=True, exist_ok=True)
    path = clauses / "a.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


class TestValidateCliExitCodes:
    def test_invokes_and_exit_0_for_valid_report_only(self, capsys):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), [VALID])
            ec = main(["--data-root", str(tmp)])
            assert ec == 0
            captured = capsys.readouterr()
            payload = json.loads(captured.out)
            assert payload["ok"] is True
            assert payload["status"] == "ok"

    def test_exit_1_for_strict_with_invalid_json(self, capsys):
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "clauses" / "a.jsonl"
            f.parent.mkdir()
            f.write_text("not-json\n", encoding="utf-8")
            ec = main(["--data-root", str(tmp), "--strict"])
            assert ec == 1
            captured = capsys.readouterr()
            payload = json.loads(captured.out)
            assert payload["ok"] is False
            assert payload["status"] == "strict_failed"

    def test_exit_1_for_strict_with_missing_required(self, capsys):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), [{k: v for k, v in VALID.items() if k != "article"}])
            ec = main(["--data-root", str(tmp), "--strict"])
            assert ec == 1

    def test_exit_1_for_strict_with_duplicate(self, capsys):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), [VALID, VALID])
            ec = main(["--data-root", str(tmp), "--strict"])
            assert ec == 1

    def test_exit_2_when_data_root_not_configured(self, capsys, monkeypatch):
        monkeypatch.delenv("DATA_ROOT", raising=False)
        ec = main([])
        assert ec == 2
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["status"] == "NOT_CONFIGURED"

    def test_exit_2_when_data_root_not_found(self, capsys):
        with TemporaryDirectory() as tmp:
            ec = main(["--data-root", str(Path(tmp) / "nonexistent")])
            assert ec == 2

    def test_report_only_returns_0_even_with_issues(self, capsys):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), [VALID, VALID])
            ec = main(["--data-root", str(tmp)])  # default = report-only
            assert ec == 0

    def test_report_file_written_and_valid_json(self, capsys):
        with TemporaryDirectory() as tmp, TemporaryDirectory() as out_dir:
            _write(Path(tmp), [VALID])
            report_path = Path(out_dir) / "report.json"
            ec = main(["--data-root", str(tmp), "--output", str(report_path)])
            assert ec == 0
            assert report_path.exists()
            parsed = json.loads(report_path.read_text(encoding="utf-8"))
            assert isinstance(parsed, dict)
            assert "summary" in parsed

    def test_prints_next_step_when_ok(self, capsys):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), [VALID])
            ec = main(["--data-root", str(tmp)])
            assert ec == 0
            captured = capsys.readouterr()
            payload = json.loads(captured.out)
            assert "next_step" in payload
            assert "scripts/ingest.py" in payload["next_step"]

    def test_no_next_step_when_strict_fails(self, capsys):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), [VALID, VALID])
            ec = main(["--data-root", str(tmp), "--strict"])
            assert ec == 1
            captured = capsys.readouterr()
            payload = json.loads(captured.out)
            assert payload.get("next_step") is None

    def test_not_configured_prints_next_step_null(self, capsys, monkeypatch):
        monkeypatch.delenv("DATA_ROOT", raising=False)
        ec = main([])
        assert ec == 2
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload.get("next_step") is None
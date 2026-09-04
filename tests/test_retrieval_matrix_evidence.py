"""真实检索矩阵证据报告必须保留失败组合与泄漏警告。"""
from __future__ import annotations

from pathlib import Path

from scripts import evaluate_retrieval_matrix as matrix


def test_missing_key_is_structured_skip(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    exit_code = matrix.main([])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"kind": "skip"' in output
    assert '"skipped": true' in output
    assert "SKIP" in output


def test_report_lists_failed_combinations_and_partial_leak_warning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    index = tmp_path / "index.db"
    index.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(matrix, "_db_for", lambda _dim: index)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    def fake_run_case(*, dim, rerank, **_kwargs):
        if rerank == "bailian":
            raise PermissionError("HTTP 403 AccessDenied")
        return {
            "queries": 70,
            "recall_at_1": 1.0,
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "mrr": 1.0,
            "dimensions": dim,
            "rerank": rerank,
            "top_k": 5,
            "recall_top_k": 20,
            "hybrid_alpha": 0.7,
        }

    monkeypatch.setattr(matrix, "run_case", fake_run_case)
    report_path = tmp_path / "matrix.md"
    exit_code = matrix.main(
        [
            "--dimensions",
            "256",
            "--rerank",
            "none",
            "bailian",
            "--out",
            str(report_path),
        ]
    )

    report = report_path.read_text(encoding="utf-8")
    assert exit_code == 1
    assert "失败组合" in report
    assert "256 | bailian" in report
    assert "HTTP 403 AccessDenied" in report
    assert "泄漏警告" in report
    assert "256" in report

"""DATA_ROOT 数据接入契约：路径解析与结构化状态。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.ingestion.data_root import resolve_data_root


class TestResolveDataRoot:
    def test_not_configured_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("DATA_ROOT", raising=False)
        result = resolve_data_root()
        assert result.status == "NOT_CONFIGURED"
        assert result.clause_files == []
        assert result.as_dict()["status"] == "NOT_CONFIGURED"

    def test_not_found_when_path_missing(self):
        with TemporaryDirectory() as tmp:
            result = resolve_data_root(Path(tmp) / "nope")
            assert result.status == "NOT_FOUND"
            assert result.clause_files == []

    def test_no_clause_files_when_clauses_dir_missing(self):
        with TemporaryDirectory() as tmp:
            result = resolve_data_root(Path(tmp))
            assert result.status == "NO_CLAUSE_FILES"
            assert result.clause_files == []

    def test_no_clause_files_when_empty(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "clauses").mkdir()
            result = resolve_data_root(Path(tmp))
            assert result.status == "NO_CLAUSE_FILES"

    def test_no_clause_files_when_only_other_extensions(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "clauses").mkdir()
            (Path(tmp) / "clauses" / "a.md").write_text("x", encoding="utf-8")
            result = resolve_data_root(Path(tmp))
            assert result.status == "NO_CLAUSE_FILES"

    def test_ok_discovers_jsonl_sorted(self):
        with TemporaryDirectory() as tmp:
            clauses = Path(tmp) / "clauses"
            clauses.mkdir()
            (clauses / "b.jsonl").write_text("", encoding="utf-8")
            (clauses / "a.jsonl").write_text("", encoding="utf-8")
            (clauses / "note.txt").write_text("", encoding="utf-8")
            result = resolve_data_root(Path(tmp))
            assert result.status == "OK"
            assert [p.name for p in result.clause_files] == ["a.jsonl", "b.jsonl"]

    def test_uses_env_var(self, monkeypatch):
        with TemporaryDirectory() as tmp:
            clauses = Path(tmp) / "clauses"
            clauses.mkdir()
            (clauses / "a.jsonl").write_text("", encoding="utf-8")
            monkeypatch.setenv("DATA_ROOT", str(tmp))
            result = resolve_data_root()
            assert result.status == "OK"
            assert len(result.clause_files) == 1

    def test_does_not_fallback_to_knowledge(self, monkeypatch):
        # 即使存在同名 knowledge/ 目录，DATA_ROOT 解析也不得回退到它
        with TemporaryDirectory() as tmp:
            knowledge = Path(tmp) / "knowledge"
            knowledge.mkdir()
            (knowledge / "c.jsonl").write_text("", encoding="utf-8")
            monkeypatch.setenv("DATA_ROOT", str(tmp))  # 根目录没有 clauses/
            result = resolve_data_root()
            assert result.status != "OK"
            assert result.clause_files == []

    def test_explicit_override_beats_env(self, monkeypatch):
        with TemporaryDirectory() as tmp_a, TemporaryDirectory() as tmp_b:
            for base in (tmp_a, tmp_b):
                (Path(base) / "clauses").mkdir()
            (Path(tmp_b) / "clauses" / "only_b.jsonl").write_text("", encoding="utf-8")
            monkeypatch.setenv("DATA_ROOT", tmp_a)  # tmp_a 的 clauses/ 为空
            result = resolve_data_root(Path(tmp_b))
            assert result.status == "OK"
            assert [p.name for p in result.clause_files] == ["only_b.jsonl"]

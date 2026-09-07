"""P3 Step 19：DRAWING_ROOT 路径契约的四态与边界。

照 `tests/` 既有口径：前置条件缺失与真实失败必须可区分，且**不自动回退**到
项目 fixture 或临时目录 —— 静默回退会让"图纸目录配错了"表现成"图纸是空的"。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from water_agent.drawing.drawing_root import (
    DRAWING_SUFFIX,
    ENV_NAME,
    resolve_drawing_root,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每条用例都从"未配置"起步，避免宿主机环境污染断言。"""
    monkeypatch.delenv(ENV_NAME, raising=False)


class TestNotConfigured:
    def test_env_absent(self) -> None:
        result = resolve_drawing_root()
        assert result.status == "NOT_CONFIGURED"
        assert result.drawing_root is None
        assert result.drawings == []
        assert ENV_NAME in result.message

    @pytest.mark.parametrize("blank", ("", "   ", "\t"))
    def test_env_blank_is_not_configured_not_cwd(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        """空串不得被当成"当前目录" —— 那是静默回退。"""
        monkeypatch.setenv(ENV_NAME, blank)
        assert resolve_drawing_root().status == "NOT_CONFIGURED"

    def test_explicit_blank_is_not_configured(self) -> None:
        assert resolve_drawing_root("  ").status == "NOT_CONFIGURED"


class TestNotFound:
    def test_missing_dir(self, tmp_path: Path) -> None:
        result = resolve_drawing_root(tmp_path / "nope")
        assert result.status == "NOT_FOUND"
        assert result.drawings == []

    def test_file_instead_of_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "a.dxf"
        target.write_text("x", encoding="utf-8")
        assert resolve_drawing_root(target).status == "NOT_FOUND"


class TestNoDrawingFiles:
    def test_empty_dir(self, tmp_path: Path) -> None:
        result = resolve_drawing_root(tmp_path)
        assert result.status == "NO_DRAWING_FILES"
        assert result.drawing_root == str(tmp_path)

    def test_dwg_only_is_not_usable(self, tmp_path: Path) -> None:
        """**反例**：只有 .dwg 时不算有可用图纸 —— P3 读不了它。

        但 message 必须提示需人工预转，否则用户会以为目录放错了。
        """
        (tmp_path / "plan.dwg").write_bytes(b"AC1032")
        result = resolve_drawing_root(tmp_path)
        assert result.status == "NO_DRAWING_FILES"
        assert result.drawings == []
        assert "DWG" in result.message and "人工" in result.message

    def test_unrelated_files_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "readme.txt").write_text("x", encoding="utf-8")
        (tmp_path / "notes.md").write_text("x", encoding="utf-8")
        assert resolve_drawing_root(tmp_path).status == "NO_DRAWING_FILES"


class TestOk:
    def test_finds_dxf_sorted(self, tmp_path: Path) -> None:
        for name in ("b.dxf", "a.dxf", "c.dxf"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        result = resolve_drawing_root(tmp_path)
        assert result.status == "OK"
        assert [p.name for p in result.drawings] == ["a.dxf", "b.dxf", "c.dxf"]

    def test_suffix_case_insensitive(self, tmp_path: Path) -> None:
        (tmp_path / "PLAN.DXF").write_text("x", encoding="utf-8")
        assert resolve_drawing_root(tmp_path).status == "OK"

    def test_env_is_used_when_no_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "a.dxf").write_text("x", encoding="utf-8")
        monkeypatch.setenv(ENV_NAME, str(tmp_path))
        assert resolve_drawing_root().status == "OK"

    def test_explicit_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = tmp_path / "good"
        good.mkdir()
        (good / "a.dxf").write_text("x", encoding="utf-8")
        monkeypatch.setenv(ENV_NAME, str(tmp_path / "nonexistent"))
        assert resolve_drawing_root(good).status == "OK"

    def test_subdirectories_are_not_recursed(self, tmp_path: Path) -> None:
        """**不递归子目录**：登记号与文件名一一对应，递归会重新引入路径歧义。"""
        nested = tmp_path / "sub"
        nested.mkdir()
        (nested / "deep.dxf").write_text("x", encoding="utf-8")
        assert resolve_drawing_root(tmp_path).status == "NO_DRAWING_FILES"

    def test_dwg_alongside_dxf_reports_both(self, tmp_path: Path) -> None:
        """有可用 DXF 时仍要提示存在不支持的 DWG，避免用户以为都被读了。"""
        (tmp_path / "a.dxf").write_text("x", encoding="utf-8")
        (tmp_path / "b.dwg").write_bytes(b"AC1032")
        result = resolve_drawing_root(tmp_path)
        assert result.status == "OK"
        assert [p.name for p in result.drawings] == ["a.dxf"]
        assert "DWG" in result.message


class TestSerialization:
    def test_as_dict_is_json_safe(self, tmp_path: Path) -> None:
        import json

        (tmp_path / "a.dxf").write_text("x", encoding="utf-8")
        payload = resolve_drawing_root(tmp_path).as_dict()
        assert json.loads(json.dumps(payload))["status"] == "OK"
        assert all(isinstance(p, str) for p in payload["drawings"])

    def test_suffix_constant_is_lowercase_dxf(self) -> None:
        assert DRAWING_SUFFIX == ".dxf"

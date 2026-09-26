"""回归测试：检索索引必须显式选出，歧义时报错而不是按文件名排序猜一个。

原实现是 ``sorted(data/indexes/*.db)[-1]``。``data/indexes/`` 同时存在 256 与 512
维两份索引，选中 512 纯属文件名巧合；再放进一份 768 维就会**无声换库** —— 维度与
``embedding.DEFAULT_DIMENSIONS`` 不符时检索质量崩掉，但没有任何一处会报错。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from water_agent.retrieval.trivium import (
    DEFAULT_INDEX_NAME,
    INDEX_PATH_ENV,
    IndexSelectionError,
    resolve_index_path,
)
from water_agent.runtime.agentic import _resolve_db_path, embedding_required


def _touch(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"")


def _no_env(_name: str) -> str | None:
    return None


def test_default_index_is_selected_by_name(tmp_path: Path) -> None:
    _touch(tmp_path, DEFAULT_INDEX_NAME, "qwen3.7__256__curated.db")
    assert resolve_index_path(getenv=_no_env, index_dir=tmp_path) == tmp_path / DEFAULT_INDEX_NAME


def test_selection_does_not_shift_when_another_index_sorts_last(tmp_path: Path) -> None:
    """加入一份排序在后的索引不得改变选择——这正是原实现的 footgun。"""
    _touch(tmp_path, DEFAULT_INDEX_NAME, "qwen3.7__768__curated.db", "zzz__1024__curated.db")
    assert resolve_index_path(getenv=_no_env, index_dir=tmp_path) == tmp_path / DEFAULT_INDEX_NAME


def test_empty_index_dir_is_a_legitimate_none(tmp_path: Path) -> None:
    """一份索引都没有是 P0 合法状态，调用方回落 fixture 检索。"""
    _touch(tmp_path)
    assert resolve_index_path(getenv=_no_env, index_dir=tmp_path) is None
    assert resolve_index_path(getenv=_no_env, index_dir=tmp_path / "absent") is None


def test_missing_default_among_others_refuses_instead_of_guessing(tmp_path: Path) -> None:
    _touch(tmp_path, "qwen3.7__256__curated.db", "qwen3.7__768__curated.db")
    with pytest.raises(IndexSelectionError) as excinfo:
        resolve_index_path(getenv=_no_env, index_dir=tmp_path)
    message = str(excinfo.value)
    assert DEFAULT_INDEX_NAME in message
    assert INDEX_PATH_ENV in message
    # 报错必须列出现有候选，否则用户不知道该把环境变量指向哪里
    assert "qwen3.7__256__curated.db" in message


def test_env_var_overrides_the_default(tmp_path: Path) -> None:
    _touch(tmp_path, DEFAULT_INDEX_NAME)
    override = tmp_path / "custom" / "other.db"
    _touch(override.parent, override.name)
    resolved = resolve_index_path(
        getenv={INDEX_PATH_ENV: str(override)}.get, index_dir=tmp_path
    )
    assert resolved == override


def test_env_var_pointing_nowhere_refuses(tmp_path: Path) -> None:
    _touch(tmp_path, DEFAULT_INDEX_NAME)
    with pytest.raises(IndexSelectionError, match=INDEX_PATH_ENV):
        resolve_index_path(getenv={INDEX_PATH_ENV: str(tmp_path / "gone.db")}.get, index_dir=tmp_path)


def test_explicit_path_wins_over_env_and_default(tmp_path: Path) -> None:
    _touch(tmp_path, DEFAULT_INDEX_NAME)
    explicit = tmp_path / "explicit.db"
    explicit.write_bytes(b"")
    resolved = resolve_index_path(
        explicit, getenv={INDEX_PATH_ENV: str(tmp_path / DEFAULT_INDEX_NAME)}.get, index_dir=tmp_path
    )
    assert resolved == explicit


def test_explicit_missing_path_refuses(tmp_path: Path) -> None:
    with pytest.raises(IndexSelectionError):
        resolve_index_path(tmp_path / "gone.db", getenv=_no_env, index_dir=tmp_path)


def test_allow_default_false_still_honours_both_explicit_sources(tmp_path: Path) -> None:
    """``allow_default=False`` 只关掉目录兜底那一层，显式参数与环境变量照旧最优先。"""
    _touch(tmp_path, DEFAULT_INDEX_NAME)
    explicit = tmp_path / "explicit.db"
    explicit.write_bytes(b"")

    assert (
        resolve_index_path(explicit, getenv=_no_env, index_dir=tmp_path, allow_default=False)
        == explicit
    )
    assert (
        resolve_index_path(
            getenv={INDEX_PATH_ENV: str(explicit)}.get, index_dir=tmp_path, allow_default=False
        )
        == explicit
    )


def test_allow_default_false_skips_the_directory_default(tmp_path: Path) -> None:
    """两层显式来源都没有时返回 None——调用方要的就是"别自动用默认索引"。"""
    _touch(tmp_path, DEFAULT_INDEX_NAME)
    assert resolve_index_path(getenv=_no_env, index_dir=tmp_path, allow_default=False) is None
    # 目录里只有非默认索引时也不报错：既然本来就不打算用默认索引，歧义无从发生
    other = tmp_path / "others"
    _touch(other, "qwen3.7__256__curated.db")
    assert resolve_index_path(getenv=_no_env, index_dir=other, allow_default=False) is None


class TestMockGeneratorIndexSelection:
    """"假模型"与"没索引"是两件事，不得再被 ``_resolve_db_path`` 绑在一起。

    默认仍走 fixture：harness 与单测靠这个保证 ``baseline.json`` 可复现，换成真实库
    会让基线漂移。但部署侧显式配了索引就必须照用——容器冒烟要的正是"真检索 + 假模型"
    这一组合（没有远端模型凭据也能跑通真实检索链路）。此前环境变量在这条路上被
    无声忽略，答案看似正常、实则出自 2 KB fixture。
    """

    def test_default_index_is_not_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _touch(tmp_path, DEFAULT_INDEX_NAME)
        monkeypatch.setattr("water_agent.retrieval.trivium.DEFAULT_INDEX_DIR", tmp_path)
        monkeypatch.delenv(INDEX_PATH_ENV, raising=False)
        args = argparse.Namespace(db_path=None, mock_generator=True)
        assert _resolve_db_path(args) is None

    def test_configured_index_is_honoured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _touch(tmp_path, DEFAULT_INDEX_NAME)
        monkeypatch.setattr("water_agent.retrieval.trivium.DEFAULT_INDEX_DIR", tmp_path)
        configured = tmp_path / "deployed.db"
        configured.write_bytes(b"")
        monkeypatch.setenv(INDEX_PATH_ENV, str(configured))
        args = argparse.Namespace(db_path=None, mock_generator=True)
        assert _resolve_db_path(args) == configured

    def test_configured_index_that_is_absent_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置指向不存在的库时报错，不得退回 fixture 装作没事。"""
        monkeypatch.setenv(INDEX_PATH_ENV, str(tmp_path / "gone.db"))
        args = argparse.Namespace(db_path=None, mock_generator=True)
        with pytest.raises(ValueError, match=INDEX_PATH_ENV):
            _resolve_db_path(args)

    def test_explicit_db_path_still_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        explicit = tmp_path / "explicit.db"
        explicit.write_bytes(b"")
        monkeypatch.setenv(INDEX_PATH_ENV, str(tmp_path / "gone.db"))
        args = argparse.Namespace(db_path=str(explicit), mock_generator=True)
        assert _resolve_db_path(args) == explicit

    def test_real_generator_keeps_using_the_default_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _touch(tmp_path, DEFAULT_INDEX_NAME)
        monkeypatch.setattr("water_agent.retrieval.trivium.DEFAULT_INDEX_DIR", tmp_path)
        monkeypatch.delenv(INDEX_PATH_ENV, raising=False)
        args = argparse.Namespace(db_path=None, mock_generator=False)
        assert _resolve_db_path(args) == tmp_path / DEFAULT_INDEX_NAME


def test_embedding_is_not_required_for_mock_generator() -> None:
    """mock 生成器不碰真实 embedding，预检不该因缺凭据而拦它。"""
    assert embedding_required(argparse.Namespace(mock_generator=True)) is False


def test_embedding_stays_unnecessary_when_mock_generator_uses_a_real_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mock 路径用的是 ``mock_embed``，就算接了真实索引也不需要 DashScope 凭据。"""
    configured = tmp_path / "deployed.db"
    configured.write_bytes(b"")
    monkeypatch.setenv(INDEX_PATH_ENV, str(configured))
    args = argparse.Namespace(db_path=None, mock_generator=True)
    assert _resolve_db_path(args) == configured
    assert embedding_required(args) is False


def test_embedding_required_when_index_selection_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """选索引本身出错时按"需要凭据"处理，让预检暴露问题而不是当作无索引降级。"""
    _touch(tmp_path, "qwen3.7__256__curated.db")
    monkeypatch.setattr("water_agent.retrieval.trivium.DEFAULT_INDEX_DIR", tmp_path)
    assert embedding_required(argparse.Namespace(mock_generator=False)) is True

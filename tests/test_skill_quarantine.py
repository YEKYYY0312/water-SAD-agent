"""P7-S13-A02：严格解析之后的隔离区（``docs/design/17-p7-external-capabilities.md`` §6.3 第 6 步）。"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from water_agent.skills import build_skill_library
from water_agent.skills.remote import quarantine as store

NAME, VERSION = "pump-room-notes", "1.0.0"
RAW = f"---\nname: {NAME}\ndescription: x。\nversion: {VERSION}\n---\nbody\n".encode()
SHA = "ab" * 32
TOOLS = ("retrieve_clauses", "cite_clauses", "calc_velocity", "material_stats")


def _store(quarantine: Path, *, raw: bytes = RAW, sha: str = SHA, version: str = VERSION) -> store.Candidate:
    return store.store_candidate(
        quarantine, name=NAME, version=version, raw=raw, sha256=sha, fetched_at="2026-10-22T03:00:00Z",
        registry_host="registry.example.com", safety={"passed": ["charset"], "warnings": []},
    )


def test_candidate_layout_and_permissions(tmp_path: Path) -> None:
    """P7-S13-A02 候选落在 ``<name>@<version>-<sha8>/``，里面是 ``SKILL.md`` + ``candidate.json``；目录 0700、文件 0600。"""

    quarantine = tmp_path / "q"
    candidate = _store(quarantine)
    assert candidate.directory.name == f"{NAME}@{VERSION}-{SHA[:8]}"
    assert sorted(path.name for path in candidate.directory.iterdir()) == ["SKILL.md", "candidate.json"]
    assert stat.S_IMODE(os.stat(quarantine).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(candidate.directory).st_mode) == 0o700
    for path in candidate.directory.iterdir():
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert candidate.skill_path.read_bytes() == RAW


def test_candidate_json_has_exactly_the_documented_keys_and_no_urls_or_paths(tmp_path: Path) -> None:
    """P7-S13-A02 ``candidate.json`` 键集合恒定，且不含完整 URL、本机路径、包内文本。"""

    import json

    candidate = _store(tmp_path / "q")
    document = json.loads((candidate.directory / store.CANDIDATE_FILENAME).read_text(encoding="utf-8"))
    assert set(document) == store.CANDIDATE_KEYS
    text = json.dumps(document)
    assert "http" not in text and str(tmp_path) not in text and "body" not in text


def test_storing_is_idempotent_and_distinct_digests_coexist(tmp_path: Path) -> None:
    """P7-S13-A02 同名同摘要重复写入原样返回；同名同版本不同摘要共存（目录名带 sha8）。"""

    quarantine = tmp_path / "q"
    first = _store(quarantine)
    again = _store(quarantine)
    assert first.directory == again.directory and again.fetched_at == first.fetched_at
    other = _store(quarantine, raw=RAW + b"x", sha="cd" * 32)
    assert other.directory != first.directory
    assert len(store.list_candidates(quarantine)) == 2


def test_no_partial_directory_is_visible_after_a_failed_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S13-A02 写到一半失败：暂存目录被清掉，正式目录名不出现（读者看不到写了一半的候选）。"""

    quarantine = tmp_path / "q"

    def boom(path: Path, payload: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_private", boom)
    with pytest.raises(OSError):
        _store(quarantine)
    assert [path.name for path in quarantine.iterdir()] == []


def test_finding_requires_valid_names_and_versions(tmp_path: Path) -> None:
    """P7-S13-A02 查找只认合法 name / version（``../`` 之类拿不到任何东西）；隔离区不存在 = 没有候选。"""

    quarantine = tmp_path / "q"
    _store(quarantine)
    assert store.find_candidate(quarantine, NAME, VERSION) is not None
    for bad in (("../q", VERSION), (NAME, "latest"), ("ab-cd\n", VERSION)):
        assert store.find_candidate(quarantine, *bad) is None
    assert store.find_candidate(tmp_path / "nope", NAME, VERSION) is None
    assert store.list_candidates(tmp_path / "nope") == []


def test_a_corrupt_candidate_json_is_skipped_not_fatal(tmp_path: Path) -> None:
    """P7-S13-A02 ``candidate.json`` 损坏的候选在列表里被跳过，不拖垮其余候选。"""

    quarantine = tmp_path / "q"
    good = _store(quarantine)
    bad = quarantine / f"other-skill@1.0.0-{'ee' * 4}"
    bad.mkdir()
    (bad / store.CANDIDATE_FILENAME).write_text("{ not json", encoding="utf-8")
    assert [item.directory for item in store.list_candidates(quarantine)] == [good.directory]


def test_a_quarantined_candidate_is_invisible_to_the_library(tmp_path: Path) -> None:
    """P7-S13-A02 候选永不被库看见：隔离区不在任何加载根下，所以即使里面是一份完全合法的 skill，``build_skill_library`` 也读不到它。"""

    quarantine, installed, operator = tmp_path / "q", tmp_path / "i", tmp_path / "o"
    for directory in (installed, operator):
        directory.mkdir()
    _store(quarantine)
    environ = {
        "WATER_AGENT_SKILLS": "on",
        "WATER_AGENT_SKILLS_DIR": str(operator),
        "WATER_AGENT_SKILLS_INSTALLED_DIR": str(installed),
    }
    library = build_skill_library(available_tools=TOOLS, environ=environ)
    assert NAME not in library.names and all(name != NAME for name, _code, _detail in library.skipped)


def test_roots_overlap_detects_equal_ancestor_descendant_and_symlinks(tmp_path: Path) -> None:
    """P7-S13-A02 四个根目录互不包含：相等、祖先 / 后代、符号链接指向同一物理目录都算重叠；互不相干则返回 ``None``。"""

    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "c").mkdir()
    assert store.roots_overlap([tmp_path / "a", tmp_path / "c"]) is None
    assert store.roots_overlap([tmp_path / "a", tmp_path / "a"]) is not None
    assert store.roots_overlap([tmp_path / "a", tmp_path / "a" / "b"]) is not None
    assert store.roots_overlap([tmp_path / "a" / "b", tmp_path / "a"]) is not None
    (tmp_path / "link").symlink_to(tmp_path / "c")
    assert store.roots_overlap([tmp_path / "c", tmp_path / "link"]) is not None
    assert store.roots_overlap([tmp_path / "x", tmp_path / "y"]) is None, "不存在的目录按其自身绝对路径判"


def test_discard_removes_the_candidate(tmp_path: Path) -> None:
    """P7-S13-A02 批准成功后候选被删除（已安装的字节与 lock 是唯一记录）。"""

    candidate = _store(tmp_path / "q")
    store.discard(candidate)
    assert not candidate.directory.exists()

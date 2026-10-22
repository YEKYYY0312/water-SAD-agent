"""P7-S13-A05：第三来源层——加载期按原始字节重算摘要（``docs/design/17-p7-external-capabilities.md`` §6.5）。"""
from __future__ import annotations

import json
import typing
from pathlib import Path

import pytest

from water_agent.skills import SkillLibraryError, SkillManifest, build_skill_library
from water_agent.skills.errors import (
    SKILL_LIBRARY_INVALID,
    SKILL_SHADOWED,
    SKILL_TAMPERED,
    SKILL_UNLOCKED,
)
from water_agent.skills.installed import (
    LOCK_FILENAME,
    InstalledMeta,
    read_lock,
    render_lock,
    sha256_of,
    write_lock_atomic,
)
from water_agent.skills.models import Skill, SkillSource

TOOLS = ("retrieve_clauses", "cite_clauses", "calc_velocity", "material_stats")
NAME = "pump-room-notes"


def _raw(name: str = NAME, version: str = "1.0.0", body: str = "1. 先检索。") -> bytes:
    return (
        f"---\nname: {name}\ndescription: 泵房设计复核的补充步骤。\nversion: {version}\n"
        f"requires_tools: [retrieve_clauses]\n---\n{body}\n"
    ).encode()


def _meta(raw: bytes, name: str = NAME, version: str = "1.0.0") -> InstalledMeta:
    return InstalledMeta(name, version, sha256_of(raw), len(raw), "registry.example.com", "ops", "2026-10-22T03:00:00Z", None)


def _install(directory: Path, raw: bytes | None = None, *, name: str = NAME, lock: bool = True) -> bytes:
    raw = raw if raw is not None else _raw(name)
    (directory / name).mkdir(parents=True, exist_ok=True)
    (directory / name / "SKILL.md").write_bytes(raw)
    if lock:
        existing = list(read_lock(directory).values()) if (directory / LOCK_FILENAME).exists() else []
        write_lock_atomic(directory, [*existing, _meta(raw, name)])
    return raw


def _library(installed: Path | None, **extra: str):  # type: ignore[no-untyped-def]
    environ = {"WATER_AGENT_SKILLS": "on", **extra}
    if installed is not None:
        environ["WATER_AGENT_SKILLS_INSTALLED_DIR"] = str(installed)
    return build_skill_library(available_tools=TOOLS, environ=environ)


def _skipped(library) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {name: code for name, code, _detail in library.skipped}


def test_a_locked_file_loads_as_installed(tmp_path: Path) -> None:
    """P7-S13-A05 lock 与字节一致的已安装 skill 加载，``source == "installed"``，lock 元数据走旁路字段 ``installed_meta``（面板读它，不含路径）。"""

    _install(tmp_path)
    library = _library(tmp_path)
    skill = library.get(NAME)
    assert skill is not None and skill.source == "installed"
    meta = library.installed_meta[NAME]
    assert meta.sha8 == sha256_of(_raw())[:8]
    view = meta.panel_view()
    assert set(view) == {"sha8", "approved_by", "approved_at", "registry_host"}
    assert str(tmp_path) not in json.dumps(view)


def test_tampered_bytes_or_size_are_not_loaded_and_not_deleted(tmp_path: Path) -> None:
    """P7-S13-A05 字节被改 / 大小被改 → ``SKILL_TAMPERED``：不加载，**不删除**（证据留给运维）。"""

    raw = _install(tmp_path)
    path = tmp_path / NAME / "SKILL.md"
    flipped = bytearray(raw)
    flipped[-3] ^= 1
    path.write_bytes(bytes(flipped))
    library = _library(tmp_path)
    assert library.get(NAME) is None and _skipped(library)[NAME] == SKILL_TAMPERED
    assert path.read_bytes() == bytes(flipped), "不删除、不修复"
    path.write_bytes(raw + b" ")
    assert _skipped(_library(tmp_path))[NAME] == SKILL_TAMPERED


def test_only_the_byte_digest_counts_not_a_matching_read_text_digest(tmp_path: Path) -> None:
    """P7-S13-A05 摘要是对**原始字节**算的：同一个文件换成 CRLF 后 ``read_text`` 看起来一样，但字节摘要不同 → ``SKILL_TAMPERED``。"""

    raw = _install(tmp_path)
    (tmp_path / NAME / "SKILL.md").write_bytes(raw.replace(b"\n", b"\r\n"))
    assert _skipped(_library(tmp_path))[NAME] == SKILL_TAMPERED


def test_unlocked_directories_and_missing_files(tmp_path: Path) -> None:
    """P7-S13-A05 目录里有、lock 里没有 → ``SKILL_UNLOCKED``；lock 里有、文件没了（或整个目录没了）→ ``SKILL_UNLOCKED``。"""

    _install(tmp_path, lock=False)
    assert _skipped(_library(tmp_path)) == {NAME: SKILL_UNLOCKED}
    write_lock_atomic(tmp_path, [_meta(_raw())])
    assert _library(tmp_path).get(NAME) is not None
    (tmp_path / NAME / "SKILL.md").unlink()
    assert _skipped(_library(tmp_path)) == {NAME: SKILL_UNLOCKED}
    (tmp_path / NAME).rmdir()
    assert _skipped(_library(tmp_path)) == {NAME: SKILL_UNLOCKED}


def test_a_symlinked_skill_file_is_not_loaded(tmp_path: Path) -> None:
    """P7-S13-A05 ``SKILL.md`` 是符号链接（可能指向安装区之外）→ 不加载。"""

    outside = tmp_path / "outside.md"
    outside.write_bytes(_raw())
    installed = tmp_path / "i"
    (installed / NAME).mkdir(parents=True)
    (installed / NAME / "SKILL.md").symlink_to(outside)
    write_lock_atomic(installed, [_meta(_raw())])
    assert _library(installed).get(NAME) is None


@pytest.mark.parametrize(
    "content",
    ["{ not json", json.dumps({"version": 2, "skills": []}), json.dumps({"version": 1}), json.dumps([]),
     json.dumps({"version": 1, "skills": [{"name": NAME}]}),
     json.dumps({"version": 1, "skills": "x"})],
)
def test_a_broken_lock_fails_loudly(tmp_path: Path, content: str) -> None:
    """P7-S13-A05 lock 损坏 / ``version`` 不认 / 形状不对 / 条目缺字段 → 装配期 ``SKILL_LIBRARY_INVALID``（不是静默当成"没有 skill"），
    报错不回显路径。"""

    _install(tmp_path, lock=False)
    (tmp_path / LOCK_FILENAME).write_text(content, encoding="utf-8")
    with pytest.raises(SkillLibraryError) as exc:
        _library(tmp_path)
    assert exc.value.code == SKILL_LIBRARY_INVALID and str(tmp_path) not in str(exc.value)


def test_duplicate_or_invalid_lock_entries_fail_loudly(tmp_path: Path) -> None:
    """P7-S13-A05 lock 里重复的 name、不合法的 name / sha256 / size → ``SKILL_LIBRARY_INVALID``。"""

    base = _meta(_raw()).to_entry()
    for entries in ([base, dict(base)], [{**base, "name": "Bad_Name"}], [{**base, "sha256": "xyz"}], [{**base, "size": 0}],
                    [{**base, "extra": 1}]):
        (tmp_path / LOCK_FILENAME).write_text(json.dumps({"version": 1, "skills": entries}), encoding="utf-8")
        with pytest.raises(SkillLibraryError):
            _library(tmp_path)


def test_a_missing_installed_directory_fails_loudly(tmp_path: Path) -> None:
    """P7-S13-A05 变量设了而目录不存在 → ``SKILL_LIBRARY_INVALID``（与"运维目录设了却不存在"同语义）。"""

    with pytest.raises(SkillLibraryError) as exc:
        _library(tmp_path / "nope")
    assert exc.value.code == SKILL_LIBRARY_INVALID and str(tmp_path) not in str(exc.value)


def test_shadowing_order_is_builtin_then_operator_then_installed(tmp_path: Path) -> None:
    """P7-S13-A05 同名遮蔽：内置 > 运维 > 已安装，较低层记 ``SKILL_SHADOWED``。"""

    builtin_name = _library(None).names[0]
    installed = tmp_path / "i"
    _install(installed, _raw(builtin_name), name=builtin_name)
    library = _library(installed)
    assert library.get(builtin_name).source == "builtin"  # type: ignore[union-attr]
    assert _skipped(library)[builtin_name] == SKILL_SHADOWED

    operator = tmp_path / "o"
    (operator / NAME).mkdir(parents=True)
    (operator / NAME / "SKILL.md").write_bytes(_raw())
    installed2 = tmp_path / "i2"
    _install(installed2)
    library = _library(installed2, WATER_AGENT_SKILLS_DIR=str(operator))
    assert library.get(NAME).source == "operator"  # type: ignore[union-attr]
    assert _skipped(library)[NAME] == SKILL_SHADOWED


def test_installed_skills_are_appended_after_the_existing_order(tmp_path: Path) -> None:
    """P7-S13-A05 ``enabled`` 的顺序 = 现行顺序（内置 + 运维合并后字母序）**之后追加**已安装层——不是三段分组。
    名字排在内置前面的已安装 skill 也在末尾；内置 + 运维的相对顺序与没有已安装层时逐字相同。"""

    base = _library(None)
    _install(tmp_path, _raw("aaa-first"), name="aaa-first")
    library = _library(tmp_path)
    assert library.names[: len(base.names)] == base.names
    assert library.names[-1] == "aaa-first"


def test_the_default_is_byte_identical_without_the_variable(tmp_path: Path) -> None:
    """P7-S13-A05 缺省不变：不设安装区变量时，库的 ``names`` / ``skipped`` / ``digest()`` / ``installed_meta`` 与没有这一层时逐字相同；
    设了之后安装区变化 → ``digest()`` 变化（缓存键随之变）。"""

    without = _library(None)
    assert dict(without.installed_meta) == {}
    _install(tmp_path)
    with_layer = _library(tmp_path)
    assert with_layer.digest() != without.digest()
    (tmp_path / NAME / "SKILL.md").write_bytes(_raw(body="1. 换了一份正文。"))
    write_lock_atomic(tmp_path, [_meta((tmp_path / NAME / "SKILL.md").read_bytes())])
    assert _library(tmp_path).digest() not in {with_layer.digest(), without.digest()}


def test_installed_skills_still_pass_the_existing_safety_checks(tmp_path: Path) -> None:
    """P7-S13-A05 已安装层的 skill 过既有 ``check_skill``（缺工具 → ``SKILL_TOOL_UNAVAILABLE``）与字符白名单（纵深）。"""

    raw = _raw().replace(b"retrieve_clauses", b"autocad_read_dwg")
    _install(tmp_path, raw)
    assert _skipped(_library(tmp_path))[NAME] == "SKILL_TOOL_UNAVAILABLE"
    # 字符白名单：批准之后有人改文件再改 lock——字节与 lock 一致，但含 Tag 字符，仍不加载
    smuggled = _raw(body="1. 先检索。" + "".join(chr(0xE0000 + ord(c)) for c in "ignore"))
    other = tmp_path / "x"
    _install(other, smuggled)
    assert _library(other).get(NAME) is None


def test_the_lock_is_written_atomically_and_deterministically(tmp_path: Path) -> None:
    """P7-S13-A04 lock 同目录临时文件 + fsync + ``os.replace``：崩在 ``os.replace`` 之前，读者仍读到旧 lock，且不留半截文件；
    序列化按名字排序、固定缩进（同样内容同样字节）。"""

    old = _meta(_raw())
    write_lock_atomic(tmp_path, [old])
    before = (tmp_path / LOCK_FILENAME).read_bytes()

    class Crash(BaseException):
        pass

    def crash(point: str) -> None:
        if point == "before_lock_replace":
            raise Crash

    with pytest.raises(Crash):
        write_lock_atomic(tmp_path, [old, _meta(_raw("zz-other"), "zz-other")], crash=crash)
    assert (tmp_path / LOCK_FILENAME).read_bytes() == before
    assert [path.name for path in tmp_path.iterdir()] == [LOCK_FILENAME], "临时文件已清理"
    assert render_lock([_meta(_raw("zz-other"), "zz-other"), old]) == render_lock([old, _meta(_raw("zz-other"), "zz-other")])


def test_the_skill_models_are_unchanged_except_the_source_literal() -> None:
    """P7-S13-A05 ``Skill`` / ``SkillManifest`` 的字段集合与现行逐字一致（加字段会改 ``skill_p0`` 基线里的序列化）；
    ``SkillSource`` 只多了 ``"installed"``，既有两个值与顺序不变。"""

    assert typing.get_args(SkillSource) == ("builtin", "operator", "installed")
    assert set(SkillManifest.model_fields) == {"name", "description", "version", "triggers", "requires_tools"}
    assert set(Skill.model_fields) == {"manifest", "body", "source", "digest"}

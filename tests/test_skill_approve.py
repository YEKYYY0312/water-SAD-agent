"""P7-S13-A04：批准与安装——必须 TTY、键入名字、三方摘要一致、原子安装、三个崩溃点无半安装
（``docs/design/17-p7-external-capabilities.md`` §6.4）。

TTY 与输入读取是注入的小替身（``ApprovalIO``），**不开伪终端**；生产实现 ``TerminalIO`` 另有一条静态断言：不读环境变量来跳过 TTY。
"""
from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path

import pytest

from water_agent.skills import build_skill_library
from water_agent.skills.errors import (
    SKILL_APPROVE_DIGEST_MISMATCH,
    SKILL_APPROVE_NAME_MISMATCH,
    SKILL_APPROVE_NO_TTY,
    SKILL_CANDIDATE_NOT_FOUND,
    SKILL_NAME_TAKEN,
    SKILL_TAMPERED,
    SKILL_UNLOCKED,
    SKILL_VERSION_NOT_NEWER,
)
from water_agent.skills.installed import LOCK_FILENAME, read_lock, sha256_of
from water_agent.skills.remote import approve as approval
from water_agent.skills.remote import quarantine as store

NAME = "pump-room-notes"
TOOLS = ("retrieve_clauses", "cite_clauses", "calc_velocity", "material_stats")
NOW = "2026-10-22T03:00:00Z"


class ScriptedIO:
    def __init__(self, *, tty: bool = True, answers: tuple[str, ...] = (NAME,)) -> None:
        self.tty = tty
        self.answers = list(answers)
        self.shown: list[str] = []
        self.prompts: list[str] = []

    def is_tty(self) -> bool:
        return self.tty

    def display(self, text: str) -> None:
        self.shown.append(text)

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class Crash(BaseException):
    """模拟进程在某点消失（BaseException：``approve`` 不会吞它）。"""


def _crash_at(point: str):  # type: ignore[no-untyped-def]
    def hook(reached: str) -> None:
        if reached == point:
            raise Crash(point)

    return hook


def _pkg(version: str, name: str = NAME) -> bytes:
    return (
        f"---\nname: {name}\ndescription: 泵房设计复核的补充步骤。\nversion: {version}\n"
        f"requires_tools: [retrieve_clauses]\n---\n# 步骤 {version}\n\n1. 先检索。\n"
    ).encode()


def _stage(quarantine: Path, version: str, name: str = NAME) -> bytes:
    raw = _pkg(version, name)
    store.store_candidate(
        quarantine, name=name, version=version, raw=raw, sha256=sha256_of(raw), fetched_at=NOW,
        registry_host="registry.example.com", safety={"passed": ["charset"], "warnings": []},
    )
    return raw


def _approve(quarantine: Path, installed: Path, version: str, raw: bytes, io: ScriptedIO | None = None, *,
             crash=None, sha: str | None = None, taken: frozenset[str] = frozenset(), name: str = NAME):  # type: ignore[no-untyped-def]
    return approval.approve(
        name=name, version=version, sha256_arg=sha if sha is not None else sha256_of(raw), quarantine_dir=quarantine,
        installed_dir=installed, taken_names=taken, io=io or ScriptedIO(), approved_by="eval-operator",
        approved_at=NOW, crash=crash,
    )


def _state(installed: Path) -> tuple[str | None, list[str], dict[str, str]]:
    library = build_skill_library(
        available_tools=TOOLS,
        environ={"WATER_AGENT_SKILLS": "on", "WATER_AGENT_SKILLS_INSTALLED_DIR": str(installed)},
    )
    loaded = library.get(NAME)
    return (
        loaded.manifest.version if loaded else None,
        [code for name, code, _detail in library.skipped if name == NAME],
        {key: value.version for key, value in read_lock(installed).items()},
    )


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    installed = tmp_path / "installed"
    installed.mkdir()
    return tmp_path / "quarantine", installed


# ---------------------------------------------------------------------------
# 人工确认门
# ---------------------------------------------------------------------------


def test_a_non_tty_is_refused_before_anything_is_shown_or_read(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A04 非 TTY 一律拒绝 ``SKILL_APPROVE_NO_TTY``——在展示任何内容、读取任何输入、写任何文件之前；没有 ``--yes``，没有旁路。"""

    quarantine, installed = dirs
    raw = _stage(quarantine, "1.0.0")
    io = ScriptedIO(tty=False)
    with pytest.raises(approval.ApprovalError) as exc:
        _approve(quarantine, installed, "1.0.0", raw, io)
    assert exc.value.code == SKILL_APPROVE_NO_TTY
    assert io.shown == [] and io.prompts == []
    assert _state(installed) == (None, [], {}) and not (installed / NAME).exists()


def test_the_typed_name_must_match_and_eof_is_not_confirmation(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A04 键入的 skill 名不对 / 空行 / EOF → ``SKILL_APPROVE_NAME_MISMATCH``，磁盘零写入。"""

    quarantine, installed = dirs
    raw = _stage(quarantine, "1.0.0")
    for answers in (("wrong-name",), ("",), (NAME + "x",), ()):
        with pytest.raises(approval.ApprovalError) as exc:
            _approve(quarantine, installed, "1.0.0", raw, ScriptedIO(answers=answers))
        assert exc.value.code == SKILL_APPROVE_NAME_MISMATCH
    assert _state(installed) == (None, [], {}) and not (installed / NAME).exists()


def test_the_three_way_digest_must_agree(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A04 ``--sha256``、隔离区文件的实际摘要、``candidate.json`` 里记录的摘要三者必须一致：参数不对 / fetch 之后文件被改 /
    candidate.json 被改，都 → ``SKILL_APPROVE_DIGEST_MISMATCH``，且不展示全文、不读输入。"""

    quarantine, installed = dirs
    raw = _stage(quarantine, "1.0.0")
    io = ScriptedIO()
    with pytest.raises(approval.ApprovalError) as exc:
        _approve(quarantine, installed, "1.0.0", raw, io, sha="0" * 64)
    assert exc.value.code == SKILL_APPROVE_DIGEST_MISMATCH and io.shown == []

    candidate = store.find_candidate(quarantine, NAME, "1.0.0")
    assert candidate is not None
    candidate.skill_path.write_bytes(raw + b"tampered")
    with pytest.raises(approval.ApprovalError) as exc:
        _approve(quarantine, installed, "1.0.0", raw, io)
    assert exc.value.code == SKILL_APPROVE_DIGEST_MISMATCH

    candidate.skill_path.write_bytes(raw)
    document = json.loads((candidate.directory / "candidate.json").read_text(encoding="utf-8"))
    document["sha256"] = "0" * 64
    (candidate.directory / "candidate.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(approval.ApprovalError):
        _approve(quarantine, installed, "1.0.0", raw, io)
    assert _state(installed) == (None, [], {})


def test_a_missing_candidate_and_a_taken_name_are_refused(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A04 隔离区里没有该候选 → ``SKILL_CANDIDATE_NOT_FOUND``；与内置 / 运维同名 → ``SKILL_NAME_TAKEN``（approve 再核一次）。"""

    quarantine, installed = dirs
    with pytest.raises(approval.ApprovalError) as exc:
        _approve(quarantine, installed, "1.0.0", _pkg("1.0.0"))
    assert exc.value.code == SKILL_CANDIDATE_NOT_FOUND
    raw = _stage(quarantine, "1.0.0")
    with pytest.raises(approval.ApprovalError) as exc:
        _approve(quarantine, installed, "1.0.0", raw, taken=frozenset({NAME}))
    assert exc.value.code == SKILL_NAME_TAKEN


def test_the_review_shows_full_text_report_and_a_diff_against_the_installed_version(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A04 展示全文、安检报告、与已装旧版的 diff；全文一次性传给 ``display``（分页由真实终端实现自己做，不消耗输入队列）。"""

    quarantine, installed = dirs
    _approve(quarantine, installed, "1.0.0", _stage(quarantine, "1.0.0"))
    raw2 = _stage(quarantine, "2.0.0")
    io = ScriptedIO()
    _approve(quarantine, installed, "2.0.0", raw2, io)
    assert len(io.shown) == 1 and len(io.prompts) == 1
    text = io.shown[0]
    assert raw2.decode() in text and "已通过的安检" in text and "确定性规则" in text
    assert "installed 1.0.0" in text and "candidate 2.0.0" in text and "-# 步骤 1.0.0" in text and "+# 步骤 2.0.0" in text


# ---------------------------------------------------------------------------
# 安装与生命周期
# ---------------------------------------------------------------------------


def test_happy_path_installs_enables_and_records_the_lock(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A04 批准即启用：库里 ``source == "installed"``；lock 记录名字 / 版本 / 摘要 / 大小 / 主机 / 批准人 / 批准时间 / ``from_version``（首装为空）；
    候选从隔离区消失；安装区文件权限不放宽。"""

    quarantine, installed = dirs
    raw = _stage(quarantine, "1.0.0")
    result = _approve(quarantine, installed, "1.0.0", raw)
    assert result.replaced is None and result.meta.from_version is None
    library = build_skill_library(
        available_tools=TOOLS, environ={"WATER_AGENT_SKILLS": "on", "WATER_AGENT_SKILLS_INSTALLED_DIR": str(installed)}
    )
    assert library.get(NAME).source == "installed"  # type: ignore[union-attr]
    entry = read_lock(installed)[NAME]
    assert (entry.version, entry.sha256, entry.size) == ("1.0.0", sha256_of(raw), len(raw))
    assert (entry.approved_by, entry.approved_at, entry.registry_host) == ("eval-operator", NOW, "registry.example.com")
    assert store.list_candidates(quarantine) == []
    assert stat.S_IMODE(os.stat(installed / NAME / "SKILL.md").st_mode) & 0o022 == 0


def test_an_upgrade_keeps_exactly_one_previous_version(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A04 升级：旧版进 ``.previous/``（只保留一版），``from_version`` 记旧版本号；再升一次只保留最近一版。"""

    quarantine, installed = dirs
    _approve(quarantine, installed, "1.0.0", _stage(quarantine, "1.0.0"))
    result = _approve(quarantine, installed, "2.0.0", _stage(quarantine, "2.0.0"))
    assert result.replaced == "1.0.0" and result.meta.from_version == "1.0.0"
    assert (installed / NAME / ".previous" / "SKILL.md").read_bytes() == _pkg("1.0.0")
    _approve(quarantine, installed, "3.0.0", _stage(quarantine, "3.0.0"))
    assert (installed / NAME / ".previous" / "SKILL.md").read_bytes() == _pkg("2.0.0")
    assert _state(installed) == ("3.0.0", [], {NAME: "3.0.0"})


def test_equal_and_lower_versions_are_refused(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A07 批准的版本必须**严格大于**已安装版本（按整数元组）：相等、更小都 → ``SKILL_VERSION_NOT_NEWER``；
    ``1.10.0`` 大于 ``1.9.0``（字符串比较会反过来）。"""

    quarantine, installed = dirs
    _approve(quarantine, installed, "1.9.0", _stage(quarantine, "1.9.0"))
    for version in ("1.9.0", "1.8.0", "0.9.9"):
        raw = _stage(quarantine, version)
        with pytest.raises(approval.ApprovalError) as exc:
            _approve(quarantine, installed, version, raw)
        assert exc.value.code == SKILL_VERSION_NOT_NEWER, version
    _approve(quarantine, installed, "1.10.0", _stage(quarantine, "1.10.0"))
    assert _state(installed)[0] == "1.10.0"


@pytest.mark.parametrize("point", approval.CRASH_POINTS)
def test_a_crash_during_a_first_install_leaves_no_loadable_half_install(dirs: tuple[Path, Path], point: str) -> None:
    """P7-S13-A04 首次安装在三个崩溃点（暂存后 / 替换后 / lock 更新前）消失：重启后库里**没有**任何未批准的字节被加载——
    ``after_stage`` 时安装区没有该目录；另两点是 ``SKILL_UNLOCKED``（文件在、lock 没有）。重试 ``approve`` 能完成。"""

    quarantine, installed = dirs
    raw = _stage(quarantine, "1.0.0")
    with pytest.raises(Crash):
        _approve(quarantine, installed, "1.0.0", raw, crash=_crash_at(point))
    loaded, skipped, lock = _state(installed)
    assert loaded is None and lock == {}
    assert skipped == ([] if point == approval.CRASH_AFTER_STAGE else [SKILL_UNLOCKED])
    assert (installed / LOCK_FILENAME).exists() is False
    _approve(quarantine, installed, "1.0.0", raw)
    assert _state(installed) == ("1.0.0", [], {NAME: "1.0.0"})


@pytest.mark.parametrize("point", approval.CRASH_POINTS)
def test_a_crash_during_an_upgrade_never_loads_bytes_the_lock_did_not_approve(dirs: tuple[Path, Path], point: str) -> None:
    """P7-S13-A04 升级在三个崩溃点消失：任何时刻加载进库的字节摘要都等于 lock 里的——``after_stage`` 仍是旧版；``after_replace`` /
    ``before_lock_replace`` 时文件已是新版而 lock 还是旧版 → ``SKILL_TAMPERED``（失败关闭，不加载）。重试 ``approve`` 完成升级。"""

    quarantine, installed = dirs
    _approve(quarantine, installed, "1.0.0", _stage(quarantine, "1.0.0"))
    raw2 = _stage(quarantine, "2.0.0")
    with pytest.raises(Crash):
        _approve(quarantine, installed, "2.0.0", raw2, crash=_crash_at(point))
    loaded, skipped, lock = _state(installed)
    assert lock == {NAME: "1.0.0"}, "lock 在三个崩溃点之前都没改"
    if point == approval.CRASH_AFTER_STAGE:
        assert loaded == "1.0.0" and skipped == []
    else:
        assert loaded is None and skipped == [SKILL_TAMPERED]
    _approve(quarantine, installed, "2.0.0", raw2)
    assert _state(installed) == ("2.0.0", [], {NAME: "2.0.0"})


def test_lock_temporary_files_do_not_survive_a_crash(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A04 崩在 ``before_lock_replace``：lock 仍是旧内容，目录里不留 lock 的临时文件。"""

    quarantine, installed = dirs
    _approve(quarantine, installed, "1.0.0", _stage(quarantine, "1.0.0"))
    before = (installed / LOCK_FILENAME).read_bytes()
    with pytest.raises(Crash):
        _approve(quarantine, installed, "2.0.0", _stage(quarantine, "2.0.0"), crash=_crash_at(approval.CRASH_BEFORE_LOCK_REPLACE))
    assert (installed / LOCK_FILENAME).read_bytes() == before
    assert not [path for path in installed.iterdir() if path.name.endswith(".tmp")]


def test_rollback_needs_a_human_and_restores_only_the_previous_version(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A07 ``rollback`` 同样必须 TTY + 键入名字；回到 ``.previous/`` 那一版（摘要核对），``from_version`` 记回滚前的版本，
    之后 ``.previous/`` 被清；没有上一版 → ``SKILL_ROLLBACK_UNAVAILABLE``；未安装 → ``SKILL_NOT_INSTALLED``。"""

    quarantine, installed = dirs
    with pytest.raises(approval.ApprovalError) as exc:
        approval.rollback(name=NAME, installed_dir=installed, io=ScriptedIO(), approved_by="o", approved_at=NOW)
    assert exc.value.code == "SKILL_NOT_INSTALLED"
    _approve(quarantine, installed, "1.0.0", _stage(quarantine, "1.0.0"))
    with pytest.raises(approval.ApprovalError) as exc:
        approval.rollback(name=NAME, installed_dir=installed, io=ScriptedIO(), approved_by="o", approved_at=NOW)
    assert exc.value.code == "SKILL_ROLLBACK_UNAVAILABLE"
    _approve(quarantine, installed, "2.0.0", _stage(quarantine, "2.0.0"))

    with pytest.raises(approval.ApprovalError) as exc:
        approval.rollback(name=NAME, installed_dir=installed, io=ScriptedIO(tty=False), approved_by="o", approved_at=NOW)
    assert exc.value.code == SKILL_APPROVE_NO_TTY and _state(installed)[0] == "2.0.0"
    with pytest.raises(approval.ApprovalError) as exc:
        approval.rollback(name=NAME, installed_dir=installed, io=ScriptedIO(answers=("nope",)), approved_by="o", approved_at=NOW)
    assert exc.value.code == SKILL_APPROVE_NAME_MISMATCH and _state(installed)[0] == "2.0.0"

    result = approval.rollback(name=NAME, installed_dir=installed, io=ScriptedIO(), approved_by="eval-operator", approved_at=NOW)
    assert result.meta.version == "1.0.0" and result.meta.from_version == "2.0.0"
    assert _state(installed) == ("1.0.0", [], {NAME: "1.0.0"}) and not (installed / NAME / ".previous").exists()


def test_a_tampered_previous_version_is_not_rolled_back_to(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A07 ``.previous/`` 里的文件被改过（摘要与 ``entry.json`` 不符）→ 不回滚，``SKILL_ROLLBACK_UNAVAILABLE``。"""

    quarantine, installed = dirs
    _approve(quarantine, installed, "1.0.0", _stage(quarantine, "1.0.0"))
    _approve(quarantine, installed, "2.0.0", _stage(quarantine, "2.0.0"))
    (installed / NAME / ".previous" / "SKILL.md").write_bytes(b"tampered")
    with pytest.raises(approval.ApprovalError) as exc:
        approval.rollback(name=NAME, installed_dir=installed, io=ScriptedIO(), approved_by="o", approved_at=NOW)
    assert exc.value.code == "SKILL_ROLLBACK_UNAVAILABLE" and _state(installed)[0] == "2.0.0"


def test_remove_needs_a_human_and_takes_the_lock_entry_out_first(dirs: tuple[Path, Path]) -> None:
    """P7-S13-A07 ``remove`` 必须 TTY + 键入名字；顺序是**先改 lock 再删文件**——崩在中间只会留下"目录在、lock 没有"（``SKILL_UNLOCKED``，不加载）。"""

    quarantine, installed = dirs
    _approve(quarantine, installed, "1.0.0", _stage(quarantine, "1.0.0"))
    with pytest.raises(approval.ApprovalError) as exc:
        approval.remove(name=NAME, installed_dir=installed, io=ScriptedIO(tty=False))
    assert exc.value.code == SKILL_APPROVE_NO_TTY and _state(installed)[0] == "1.0.0"
    with pytest.raises(Crash):
        approval.remove(name=NAME, installed_dir=installed, io=ScriptedIO(), crash=_crash_at(approval.CRASH_AFTER_REPLACE))
    assert _state(installed) == (None, [SKILL_UNLOCKED], {})
    _approve(quarantine, installed, "2.0.0", _stage(quarantine, "2.0.0"))
    approval.remove(name=NAME, installed_dir=installed, io=ScriptedIO())
    assert _state(installed) == (None, [], {}) and not (installed / NAME).exists()


def test_the_production_terminal_never_reads_the_environment_to_skip_the_tty_check() -> None:
    """P7-S13-A04 没有 ``--yes``、没有环境变量旁路（AST 判定，不看注释）：

    - ``skills/remote/approve.py`` 整个模块不引用 ``environ`` / ``getenv``；
    - ``skills/cli.py`` 里 ``environ`` 只出现在 ``real_deps()``（配置经 ``CliDeps.env`` 进来，那不是 TTY 判定）；
    - ``TerminalIO.is_tty`` 的函数体只引用 ``stdin`` / ``stdout``；
    - CLI 的参数名里没有 ``--yes`` / ``--force`` / ``--non-interactive`` 之类。
    """

    root = Path(approval.__file__).resolve().parents[1]
    approve_tree = ast.parse((root / "remote" / "approve.py").read_text(encoding="utf-8"))
    cli_tree = ast.parse((root / "cli.py").read_text(encoding="utf-8"))

    def mentions(tree: ast.AST, *needles: str) -> list[str]:
        return [
            getattr(node, "attr", None) or getattr(node, "id", "")
            for node in ast.walk(tree)
            if (isinstance(node, ast.Attribute) and node.attr in needles) or (isinstance(node, ast.Name) and node.id in needles)
        ]

    assert mentions(approve_tree, "environ", "getenv") == []

    functions = {node.name: node for node in ast.walk(cli_tree) if isinstance(node, ast.FunctionDef)}
    for name, function in functions.items():
        if name != "real_deps":
            assert mentions(function, "environ", "getenv") == [], f"{name} 不得读进程环境"
    is_tty = functions["is_tty"]
    assert set(mentions(is_tty, "stdin", "stdout", "isatty", "environ", "getenv")) == {"stdin", "stdout", "isatty"}

    literals = {node.value for tree in (approve_tree, cli_tree) for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    for forbidden in ("--yes", "-y", "--force", "--non-interactive", "--assume-yes", "--no-input", "--batch"):
        assert forbidden not in literals, forbidden

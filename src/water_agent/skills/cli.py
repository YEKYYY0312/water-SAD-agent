"""运维 CLI：``python -m water_agent.skills.cli fetch | approve | list | remove | rollback``（P7-S13，规格 §6.1）。

**这是唯一导入 ``skills/remote/`` 的模块**（P7-I12，AST 判定）。模型只有 ``load_skill``；Web 成员只读列表；Web 管理员
能看正文与启停，**不能取、不能批准**。取回与批准发生在有 shell 的运维主机上——被盗的管理员账号不能拥有"把一段文本写进
系统策略"的能力。

配置全部从 ``CliDeps.env`` 读（生产是 ``os.environ``；评测注入字典，不碰进程环境）：

- ``WATER_AGENT_SKILL_REGISTRY_URL``（基址，运维写死）· ``WATER_AGENT_SKILL_REGISTRY_ALLOW_PRIVATE=1``（才放行回环 / 私网，
  链路本地与云元数据永远拒）；**注册表基址只给 ``skills-admin`` 服务，不给 ``app``**；
- ``WATER_AGENT_SKILLS_INSTALLED_DIR`` · ``WATER_AGENT_SKILLS_QUARANTINE_DIR`` · ``WATER_AGENT_SKILLS_STATE_DIR``（审计）；
  ``WATER_AGENT_SKILLS_DIR``（运维目录，可选，用于同名预检）。

退出码：0 成功；1 失败（stdout 里有稳定错误码）；2 配置 / 用法错误（``SKILL_CONFIG_INVALID``）；3 人工确认门拒绝
（``SKILL_APPROVE_NO_TTY`` / ``SKILL_APPROVE_NAME_MISMATCH``）。每个子命令都支持 ``--json``：stdout 只有**一行** JSON。

**审计**：每个动作追加一行 JSON 到 ``<state>/skills-audit.jsonl``（0600，只增不改）：``ts · action · ref · sha12 · os_user · code``，
fetch 另有 ``registry_host``（只有主机名）。**不记正文，不记完整注册表 URL**。批准人是 ``getpass.getuser()``——那只是 OS 用户名，
**不是认证**。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import pydoc
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from water_agent.runtime.governance.binding import default_gateway
from water_agent.runtime.governance.gateway import ProviderGateway
from water_agent.skills.errors import (
    SKILL_APPROVE_NAME_MISMATCH,
    SKILL_APPROVE_NO_TTY,
    SKILL_CONFIG_INVALID,
    SKILL_INTERNAL_ERROR,
    SKILL_INVALID,
    SKILL_LIBRARY_INVALID,
    SKILL_NAME_TAKEN,
    SKILL_NOT_IN_REGISTRY,
    SKILL_REGISTRY_REPLAY,
    SKILL_REGISTRY_UNAVAILABLE,
    SKILL_VERSION_NOT_NEWER,
    SkillLibraryError,
)
from water_agent.skills.installed import (
    INSTALLED_DIR_ENV,
    PREVIOUS_DIRNAME,
    PREVIOUS_ENTRY_FILENAME,
    parse_entry,
    read_lock,
    scan_installed,
)
from water_agent.skills.library import BUILTIN_SKILLS_DIR, SKILLS_DIR_ENV
from water_agent.skills.naming import is_valid_name, is_valid_version, version_tuple
from water_agent.skills.remote import approve as approval
from water_agent.skills.remote import quarantine as quarantine_store
from water_agent.skills.remote.approve import ApprovalError, ApprovalIO
from water_agent.skills.remote.client import (
    RegistryClient,
    RegistryConfig,
    SkillRemoteError,
)
from water_agent.skills.remote.parse import SkillParseError, parse_package, to_skill
from water_agent.skills.remote.quarantine import QuarantineError
from water_agent.skills.remote.safety import RemoteSafetyError, check_remote_skill

REGISTRY_URL_ENV = "WATER_AGENT_SKILL_REGISTRY_URL"
REGISTRY_ALLOW_PRIVATE_ENV = "WATER_AGENT_SKILL_REGISTRY_ALLOW_PRIVATE"
QUARANTINE_DIR_ENV = "WATER_AGENT_SKILLS_QUARANTINE_DIR"
STATE_DIR_ENV = "WATER_AGENT_SKILLS_STATE_DIR"
AUDIT_FILENAME = "skills-audit.jsonl"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_HUMAN_GATE = 3

#: ``requires_tools`` 只能点名一方工具（本步的下载路径不看 MCP 工具：远程 server 的工具名不稳定，规程不应依赖它们）。
FIRST_PARTY_TOOLS: tuple[str, ...] = (
    "retrieve_clauses",
    "cite_clauses",
    "calc_velocity",
    "material_stats",
    "read_drawing",
    "load_skill",
)


# ---------------------------------------------------------------------------
# 依赖与真实终端 IO
# ---------------------------------------------------------------------------


class TerminalIO:
    """真实终端。``is_tty`` 要求 stdin 与 stdout **都是**终端；展示全文走分页器；**不读任何环境变量来跳过 TTY**。"""

    def is_tty(self) -> bool:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())

    def display(self, text: str) -> None:
        pydoc.pager(text)

    def read_line(self, prompt: str) -> str:
        return input(prompt)


@dataclass
class CliDeps:
    env: Mapping[str, str]
    stdout: TextIO
    stderr: TextIO
    io: ApprovalIO
    now: Callable[[], datetime]
    user: Callable[[], str]
    resolver: Callable[[str, int], list[str]] | None = None
    ssl_context: Any | None = None
    gateway: ProviderGateway | None = None
    crash: Callable[[str], None] | None = None
    builtin_dir: Path | None = None
    extra: dict[str, object] = field(default_factory=dict)


def real_deps() -> CliDeps:
    return CliDeps(
        env=dict(os.environ),
        stdout=sys.stdout,
        stderr=sys.stderr,
        io=TerminalIO(),
        now=lambda: datetime.now(UTC),
        user=getpass.getuser,
    )


class _Failure(Exception):
    """一个动作的失败结局（稳定码 + 退出码 + 固定说明）。"""

    def __init__(self, code: str, message: str, *, exit_code: int = EXIT_FAILED) -> None:
        super().__init__(f"{code}：{message}")
        self.code = code
        self.message = message
        self.exit_code = exit_code


@dataclass(frozen=True)
class _Paths:
    installed: Path
    quarantine: Path
    state: Path
    builtin: Path
    operator: Path | None


def _timestamp(deps: CliDeps) -> str:
    return deps.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paths(deps: CliDeps) -> _Paths:
    missing = [
        name
        for name in (INSTALLED_DIR_ENV, QUARANTINE_DIR_ENV, STATE_DIR_ENV)
        if not (deps.env.get(name) or "").strip()
    ]
    if missing:
        raise _Failure(SKILL_CONFIG_INVALID, f"缺少必需的环境变量：{', '.join(missing)}", exit_code=EXIT_CONFIG)
    operator_raw = (deps.env.get(SKILLS_DIR_ENV) or "").strip()
    paths = _Paths(
        installed=Path(deps.env[INSTALLED_DIR_ENV].strip()).expanduser(),
        quarantine=Path(deps.env[QUARANTINE_DIR_ENV].strip()).expanduser(),
        state=Path(deps.env[STATE_DIR_ENV].strip()).expanduser(),
        builtin=deps.builtin_dir if deps.builtin_dir is not None else BUILTIN_SKILLS_DIR,
        operator=Path(operator_raw).expanduser() if operator_raw else None,
    )
    roots = [paths.builtin, paths.installed, paths.quarantine, paths.state]
    if paths.operator is not None:
        roots.append(paths.operator)
    if quarantine_store.roots_overlap(roots) is not None:
        # 候选在批准前不得被库看见——靠"隔离区不在任何加载根下"这个结构事实，不靠"库会忽略它"
        raise _Failure(
            SKILL_CONFIG_INVALID,
            "内置 / 运维 / 已安装 / 隔离区 / 审计目录互相包含或相同；隔离区必须在任何加载根之外",
            exit_code=EXIT_CONFIG,
        )
    return paths


def _taken_names(paths: _Paths) -> frozenset[str]:
    """内置与运维目录里已有的 skill 目录名（**按目录名判，不看能否解析**：解析失败的同名目录也占着名字）。"""

    names: set[str] = set()
    for root in (paths.builtin, paths.operator):
        if root is not None and root.is_dir():
            names.update(child.name for child in root.iterdir() if child.is_dir() and not child.name.startswith("."))
    return frozenset(names)


def _parse_ref(ref: str) -> tuple[str, str]:
    name, separator, version = ref.partition("@")
    if not separator or "@" in version or not is_valid_name(name) or not is_valid_version(version):
        raise _Failure(SKILL_INVALID, "参数必须是 name@version（name 为 ASCII kebab-case，version 为 主.次.修订）")
    return name, version


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def _audit(deps: CliDeps, paths: _Paths | None, *, action: str, ref: str, sha256: str, code: str,
           registry_host: str | None = None) -> None:
    """追加一行审计。写不进去**不吞掉**：审计是长期留存的运维记录，缺它的动作不该悄悄成功。"""

    if paths is None:
        return
    record: dict[str, object] = {
        "ts": _timestamp(deps),
        "action": action,
        "ref": ref,
        "sha12": sha256[:12],
        "os_user": deps.user(),
        "code": code,
    }
    if registry_host is not None:
        record["registry_host"] = registry_host
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.state, 0o700)
        descriptor = os.open(paths.state / AUDIT_FILENAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        raise _Failure(SKILL_CONFIG_INVALID, f"审计目录不可写：{type(exc).__name__}", exit_code=EXIT_CONFIG) from exc


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------


def _registry_client(deps: CliDeps) -> RegistryClient:
    base = (deps.env.get(REGISTRY_URL_ENV) or "").strip()
    if not base:
        raise _Failure(SKILL_REGISTRY_UNAVAILABLE, f"未配置 {REGISTRY_URL_ENV}")
    config = RegistryConfig(base_url=base, allow_private=(deps.env.get(REGISTRY_ALLOW_PRIVATE_ENV) or "").strip() == "1")
    gateway = deps.gateway if deps.gateway is not None else default_gateway()
    return RegistryClient(config, gateway=gateway, resolver=deps.resolver, ssl_context=deps.ssl_context)


def _local_versions(paths: _Paths, name: str) -> dict[str, str]:
    """本地已知的该 skill 的版本 → sha256（已安装的 + ``.previous/`` 里的）。重放检查用。"""

    known: dict[str, str] = {}
    lock = read_lock(paths.installed)
    if name in lock:
        known[lock[name].version] = lock[name].sha256
    entry = paths.installed / name / PREVIOUS_DIRNAME / PREVIOUS_ENTRY_FILENAME
    try:
        previous = parse_entry(json.loads(entry.read_text(encoding="utf-8")))
        if previous.name == name:
            known.setdefault(previous.version, previous.sha256)
    except (OSError, ValueError, SkillLibraryError):
        pass
    return known


def _do_fetch(deps: CliDeps, paths: _Paths, ref: str) -> dict[str, Any]:
    name, version = _parse_ref(ref)
    # -- 本地检查（零请求）------------------------------------------------------------------------------------
    if name in _taken_names(paths):
        raise _Failure(SKILL_NAME_TAKEN, "与内置或运维目录的 skill 同名；已安装层优先级最低，不能覆盖它们")
    lock = read_lock(paths.installed)
    installed = lock.get(name)
    if installed is not None and version_tuple(version) < version_tuple(installed.version):
        raise _Failure(SKILL_VERSION_NOT_NEWER, "请求的版本低于已安装版本；回到旧版只有 rollback")
    client = _registry_client(deps)
    # -- index ------------------------------------------------------------------------------------------------
    try:
        index = client.fetch_index()
        # 重放：注册表里同一个已知版本的 sha256 变了（同版本不同内容）
        for known_version, known_sha in _local_versions(paths, name).items():
            declared_known = index.find(name, known_version)
            if declared_known is not None and declared_known.sha256 != known_sha:
                raise _Failure(SKILL_REGISTRY_REPLAY, "注册表里已批准版本的 sha256 与本地记录不一致（重放或被篡改）")
        declared = index.find(name, version)
        if declared is None:
            raise _Failure(SKILL_NOT_IN_REGISTRY, "注册表里没有该 name@version")
        if installed is not None and version == installed.version:
            raise _Failure(SKILL_VERSION_NOT_NEWER, "该版本已经安装；更新需要更高的版本号")
        fetched = client.fetch_skill_file(name, version, declared=declared)
    except SkillRemoteError as exc:
        raise _Failure(exc.code, exc.message) from None
    # -- 严格解析 + 安检（全在内存里，磁盘零写入）--------------------------------------------------------------
    try:
        package = parse_package(fetched.raw, expected_name=name, expected_version=version, sha256=fetched.sha256)
        report = check_remote_skill(to_skill(package), available_tools=FIRST_PARTY_TOOLS)
    except (SkillParseError, RemoteSafetyError) as exc:
        raise _Failure(exc.code, exc.detail) from None
    # -- 最后一步才写隔离区 -----------------------------------------------------------------------------------
    try:
        candidate = quarantine_store.store_candidate(
            paths.quarantine,
            name=name,
            version=version,
            raw=fetched.raw,
            sha256=fetched.sha256,
            fetched_at=_timestamp(deps),
            registry_host=fetched.registry_host,
            safety=report.to_dict(),
        )
    except (QuarantineError, OSError) as exc:
        raise _Failure(SKILL_CONFIG_INVALID, f"隔离区不可写：{type(exc).__name__}", exit_code=EXIT_CONFIG) from exc
    return {
        "name": name,
        "version": version,
        "sha256": fetched.sha256,
        "size": fetched.size,
        "candidate": candidate.directory.name,
        "registry_host": fetched.registry_host,
        "next": f"approve {name}@{version} --sha256 {fetched.sha256}",
    }


def _do_approve(deps: CliDeps, paths: _Paths, ref: str, sha256_arg: str) -> dict[str, Any]:
    name, version = _parse_ref(ref)
    try:
        result = approval.approve(
            name=name,
            version=version,
            sha256_arg=sha256_arg.strip().lower(),
            quarantine_dir=paths.quarantine,
            installed_dir=paths.installed,
            taken_names=_taken_names(paths),
            io=deps.io,
            approved_by=deps.user(),
            approved_at=_timestamp(deps),
            crash=deps.crash,
        )
    except ApprovalError as exc:
        raise _Failure(exc.code, exc.detail or exc.code, exit_code=_gate_exit(exc.code)) from None
    return {
        "name": name,
        "version": version,
        "sha256": result.meta.sha256,
        "from_version": result.replaced,
        "approved_by": result.meta.approved_by,
        "enabled": True,
    }


def _gate_exit(code: str) -> int:
    return EXIT_HUMAN_GATE if code in {SKILL_APPROVE_NO_TTY, SKILL_APPROVE_NAME_MISMATCH} else EXIT_FAILED


def _do_rollback(deps: CliDeps, paths: _Paths, name: str) -> dict[str, Any]:
    if not is_valid_name(name):
        raise _Failure(SKILL_INVALID, "skill 名不合法")
    try:
        result = approval.rollback(
            name=name, installed_dir=paths.installed, io=deps.io, approved_by=deps.user(),
            approved_at=_timestamp(deps), crash=deps.crash,
        )
    except ApprovalError as exc:
        raise _Failure(exc.code, exc.detail or exc.code, exit_code=_gate_exit(exc.code)) from None
    return {"name": name, "version": result.meta.version, "sha256": result.meta.sha256, "from_version": result.replaced}


def _do_remove(deps: CliDeps, paths: _Paths, name: str) -> dict[str, Any]:
    if not is_valid_name(name):
        raise _Failure(SKILL_INVALID, "skill 名不合法")
    try:
        meta = approval.remove(name=name, installed_dir=paths.installed, io=deps.io, crash=deps.crash)
    except ApprovalError as exc:
        raise _Failure(exc.code, exc.detail or exc.code, exit_code=_gate_exit(exc.code)) from None
    return {"name": name, "version": meta.version, "sha256": meta.sha256}


def _do_list(deps: CliDeps, paths: _Paths) -> dict[str, Any]:
    lock = read_lock(paths.installed) if paths.installed.is_dir() else {}
    problems: dict[str, str] = {}
    if paths.installed.is_dir():
        _ok, found = scan_installed(paths.installed)
        problems = {problem.name: problem.code for problem in found}
    installed: list[dict[str, str]] = []
    for name in sorted(set(lock) | set(problems)):
        meta = lock.get(name)
        installed.append(
            {
                "name": name,
                "version": meta.version if meta is not None else "",
                "sha8": meta.sha8 if meta is not None else "",
                "approved_by": meta.approved_by if meta is not None else "",
                "approved_at": meta.approved_at if meta is not None else "",
                "status": problems.get(name, "ok"),
            }
        )
    candidates = [
        {"name": item.name, "version": item.version, "sha8": item.sha8}
        for item in quarantine_store.list_candidates(paths.quarantine)
    ]
    return {"installed": installed, "candidates": candidates}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m water_agent.skills.cli",
        description="skill 注册表运维 CLI：fetch（取回到隔离区）→ approve（人工批准并安装）。只能在运维主机的交互终端里批准。",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--json", action="store_true", help="stdout 只输出一行 JSON")
        return sub

    fetch = add("fetch", "从注册表取回 name@version 到隔离区（不安装）")
    fetch.add_argument("ref")
    approve_cmd = add("approve", "人工批准隔离区里的候选并安装（必须交互终端，键入 skill 名）")
    approve_cmd.add_argument("ref")
    approve_cmd.add_argument("--sha256", required=True, help="候选文件的完整 64 位 sha256")
    add("list", "列出已安装 skill（含状态）与隔离区候选")
    remove = add("remove", "卸载已安装 skill（必须交互终端，键入 skill 名）")
    remove.add_argument("name")
    rollback = add("rollback", "回到上一版（必须交互终端，键入 skill 名）")
    rollback.add_argument("name")
    return parser


class _Parser(argparse.ArgumentParser):
    """用法错误不让 argparse 直接 ``sys.exit``——统一成退出码 2。"""


def main(argv: Sequence[str], *, deps: CliDeps | None = None) -> int:
    deps = deps if deps is not None else real_deps()
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:  # --help 正常退出 0；用法错误是 2
        return int(exc.code) if isinstance(exc.code, int) else EXIT_CONFIG
    as_json = bool(args.json)
    action = str(args.command)
    ref = str(getattr(args, "ref", None) or getattr(args, "name", None) or "")
    paths: _Paths | None = None
    sha256 = ""
    registry_host: str | None = None

    def emit(ok: bool, code: str, message: str, extra: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"ok": ok, "action": action, "code": code, "message": message}
        if extra:
            payload.update(extra)
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=deps.stdout)
        else:
            stream = deps.stdout if ok else deps.stderr
            print(f"{'OK' if ok else '失败'} {code}：{message}", file=stream)
            for key, value in (extra or {}).items():
                if key in {"installed", "candidates"}:
                    for item in value:
                        print("  " + "  ".join(f"{k}={v}" for k, v in item.items()), file=deps.stdout)
                elif value is not None:
                    print(f"  {key}: {value}", file=deps.stdout)

    try:
        paths = _paths(deps)
        if action == "approve":
            # 非 TTY 一律在做任何事之前拒绝：参数解析、读隔离区、展示全文都不会发生
            approval.require_human(deps.io)
            extra = _do_approve(deps, paths, ref, str(args.sha256))
            sha256 = str(extra["sha256"])
            message = f"已批准并安装 {extra['name']}@{extra['version']}（批准即启用）"
        elif action == "fetch":
            extra = _do_fetch(deps, paths, ref)
            sha256, registry_host = str(extra["sha256"]), str(extra["registry_host"])
            message = f"已取回 {extra['name']}@{extra['version']} 到隔离区；尚未安装，需要人工 approve"
        elif action == "rollback":
            approval.require_human(deps.io)
            extra = _do_rollback(deps, paths, ref)
            sha256 = str(extra["sha256"])
            message = f"已回滚到 {extra['name']}@{extra['version']}"
        elif action == "remove":
            approval.require_human(deps.io)
            extra = _do_remove(deps, paths, ref)
            sha256 = str(extra["sha256"])
            message = f"已卸载 {extra['name']}@{extra['version']}"
        else:
            extra = _do_list(deps, paths)
            message = f"已安装 {len(extra['installed'])} 个，候选 {len(extra['candidates'])} 个"
        _audit(deps, paths, action=action, ref=ref, sha256=sha256, code="ok", registry_host=registry_host)
        emit(True, "ok", message, extra)
        return EXIT_OK
    except ApprovalError as exc:  # require_human 在 _do_* 之外抛出
        failure = _Failure(exc.code, exc.detail or exc.code, exit_code=_gate_exit(exc.code))
    except _Failure as exc:
        failure = exc
    except SkillLibraryError as exc:
        failure = _Failure(exc.code if exc.code else SKILL_LIBRARY_INVALID, exc.message)
    except Exception as exc:  # noqa: BLE001 — 只给异常类型：消息里可能带路径或上游内容
        failure = _Failure(SKILL_INTERNAL_ERROR, f"未预期的错误（{type(exc).__name__}）")
    try:
        _audit(deps, paths, action=action, ref=ref, sha256=sha256, code=failure.code)
    except _Failure:
        pass  # 审计目录本身不可用时，失败结局以原始错误为准
    emit(False, failure.code, failure.message)
    return failure.exit_code


def run() -> None:
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    run()


__all__ = [
    "FIRST_PARTY_TOOLS",
    "CliDeps",
    "TerminalIO",
    "main",
    "real_deps",
]

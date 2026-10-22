"""P7-S13-A07：运维 CLI 的完整生命周期——fetch → approve → 更新 → rollback → remove，降级与重放被拒，审计与配置检查
（``docs/design/17-p7-external-capabilities.md`` §6.1 / §6.6）。

经 ``skills.cli.main`` 真实调用（进程内，注入 TTY 替身与固定时钟），对进程内线程假注册表；不起子进程、不开伪终端。
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from water_agent.runtime.governance.gateway import ProviderGateway
from water_agent.skills import build_skill_library
from water_agent.skills.cli import CliDeps, main
from water_agent.skills.installed import read_lock

NAME = "pump-room-notes"
TOOLS = ("retrieve_clauses", "cite_clauses", "calc_velocity", "material_stats")
SENTINEL_PATH = "/reg-SECRETPATH-7c1e"
UPSTREAM_SECRET = "upstream-body-SECRET-TOKEN https://upstream.internal/leak?key=sk-live-abcdef0123456789"


def _pkg(version: str, name: str = NAME) -> bytes:
    return (
        f"---\nname: {name}\ndescription: 泵房设计复核的补充步骤。\nversion: {version}\n"
        f"requires_tools: [retrieve_clauses, cite_clauses]\n---\n# 步骤 {version}\n\n1. 先检索。\n"
    ).encode()


class Registry:
    """可变的进程内假注册表：``catalog`` 是 版本 → 字节；``index_override`` 让某个版本在 index 里换 sha256（重放）。"""

    def __init__(self) -> None:
        self.catalog: dict[str, bytes] = {"1.0.0": _pkg("1.0.0"), "2.0.0": _pkg("2.0.0")}
        self.sha_override: dict[str, str] = {}
        self.status = 200
        self.requests: list[str] = []
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:
                registry.requests.append(self.path)
                if registry.status != 200:
                    self._send(registry.status, "text/plain", UPSTREAM_SECRET.encode())
                    return
                if self.path.endswith("/index.json"):
                    versions = [
                        {"version": v, "sha256": registry.sha_override.get(v, hashlib.sha256(raw).hexdigest()), "size": len(raw)}
                        for v, raw in registry.catalog.items()
                    ]
                    self._send(200, "application/json", json.dumps({"version": 1, "skills": [{"name": NAME, "versions": versions}]}).encode())
                elif self.path.endswith("/SKILL.md"):
                    version = self.path.rsplit("/", 2)[-2]
                    if version in registry.catalog:
                        self._send(200, "text/markdown", registry.catalog[version])
                        return
                    self._send(404, "text/plain", b"nope")
                else:
                    self._send(404, "text/plain", b"nope")

            def _send(self, status: int, ctype: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self) -> Registry:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{SENTINEL_PATH}"


class TtyIO:
    def __init__(self, *, tty: bool = True, answers: tuple[str, ...] = (NAME,)) -> None:
        self.tty, self.answers = tty, list(answers)
        self.shown: list[str] = []

    def is_tty(self) -> bool:
        return self.tty

    def display(self, text: str) -> None:
        self.shown.append(text)

    def read_line(self, prompt: str) -> str:
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@pytest.fixture()
def world(tmp_path: Path) -> Any:
    with Registry() as registry:
        env = {
            "WATER_AGENT_SKILLS": "on",
            "WATER_AGENT_SKILL_REGISTRY_URL": registry.url,
            "WATER_AGENT_SKILL_REGISTRY_ALLOW_PRIVATE": "1",
            "WATER_AGENT_SKILLS_INSTALLED_DIR": str(tmp_path / "installed"),
            "WATER_AGENT_SKILLS_QUARANTINE_DIR": str(tmp_path / "quarantine"),
            "WATER_AGENT_SKILLS_STATE_DIR": str(tmp_path / "state"),
        }
        (tmp_path / "installed").mkdir()

        def run(*argv: str, io_: TtyIO | None = None, env_: dict[str, str] | None = None) -> tuple[int, dict[str, Any], str]:
            out, err = io.StringIO(), io.StringIO()
            deps = CliDeps(
                env=env_ if env_ is not None else env, stdout=out, stderr=err, io=io_ or TtyIO(),
                now=lambda: datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc), user=lambda: "eval-operator",
                gateway=ProviderGateway(sleep=lambda seconds: None),
            )
            code = main([*argv, "--json"], deps=deps)
            text = out.getvalue().strip()
            return code, json.loads(text) if text else {}, out.getvalue() + err.getvalue()

        def library() -> Any:
            return build_skill_library(available_tools=TOOLS, environ=env)

        yield SimpleNamespace(registry=registry, env=env, run=run, library=library, tmp=tmp_path)


def _install(world: Any, version: str) -> dict[str, Any]:
    code, fetched, _ = world.run("fetch", f"{NAME}@{version}")
    assert code == 0, fetched
    code, approved, _ = world.run("approve", f"{NAME}@{version}", "--sha256", fetched["sha256"])
    assert code == 0, approved
    return approved


def test_fetch_approve_update_rollback_remove(world: Any) -> None:
    """P7-S13-A07 完整生命周期：fetch 只进隔离区（库里看不见）；approve 后库里 ``source == "installed"``；更新是新候选、不动已装版本，
    approve 后 ``from_version`` 记旧版；rollback 回到上一版；remove 后库里没有它、文件已删。"""

    code, fetched, _ = world.run("fetch", f"{NAME}@1.0.0")
    assert code == 0 and fetched["candidate"] == f"{NAME}@1.0.0-{fetched['sha256'][:8]}"
    assert world.library().get(NAME) is None, "候选在批准前不得被库看见"
    approved = _install(world, "1.0.0")
    assert approved["from_version"] is None and approved["enabled"] is True
    assert world.library().get(NAME).source == "installed"

    code, fetched2, _ = world.run("fetch", f"{NAME}@2.0.0")
    assert code == 0 and world.library().get(NAME).manifest.version == "1.0.0", "更新是新候选，不动已装版本"
    code, upgraded, _ = world.run("approve", f"{NAME}@2.0.0", "--sha256", fetched2["sha256"])
    assert code == 0 and upgraded["from_version"] == "1.0.0" and world.library().get(NAME).manifest.version == "2.0.0"

    code, rolled, _ = world.run("rollback", NAME)
    assert code == 0 and rolled["version"] == "1.0.0" and world.library().get(NAME).manifest.version == "1.0.0"
    code, removed, _ = world.run("remove", NAME)
    assert code == 0 and world.library().get(NAME) is None and not (world.tmp / "installed" / NAME).exists()


def test_human_gate_exit_codes_and_zero_writes(world: Any) -> None:
    """P7-S13-A07 approve / rollback / remove 在非 TTY 下退出码 3 与 ``SKILL_APPROVE_NO_TTY``，磁盘零写入；键入名字不对退出码 3；
    没有 ``--yes`` 之类的参数（用法错误 → 退出码 2，不会被当成确认）。"""

    world.registry.catalog["3.0.0"] = _pkg("3.0.0")
    _install(world, "1.0.0")
    _install(world, "2.0.0")  # 有 .previous/，rollback 才会走到"键入名字"这一步
    code, fetched, _ = world.run("fetch", f"{NAME}@3.0.0")
    before = (world.tmp / "installed" / "skills.lock.json").read_bytes()
    for argv in (("approve", f"{NAME}@3.0.0", "--sha256", fetched["sha256"]), ("rollback", NAME), ("remove", NAME)):
        code, result, _ = world.run(*argv, io_=TtyIO(tty=False))
        assert code == 3 and result["code"] == "SKILL_APPROVE_NO_TTY", argv
        code, result, _ = world.run(*argv, io_=TtyIO(answers=("wrong",)))
        assert code == 3 and result["code"] == "SKILL_APPROVE_NAME_MISMATCH", argv
    assert (world.tmp / "installed" / "skills.lock.json").read_bytes() == before
    assert world.library().get(NAME).manifest.version == "2.0.0"
    for flag in ("--yes", "-y", "--force", "--non-interactive"):
        code, _, _ = world.run("approve", f"{NAME}@3.0.0", "--sha256", fetched["sha256"], flag)
        assert code == 2, flag
    assert world.library().get(NAME).manifest.version == "2.0.0"


def test_downgrade_and_equal_versions_are_refused_with_zero_requests(world: Any) -> None:
    """P7-S13-A07 已装 2.0.0 时 ``fetch name@1.0.0`` → ``SKILL_VERSION_NOT_NEWER``，**一个请求都不发**（本地检查）；
    回到旧版只有 ``rollback``。相等版本也拒绝（先发 index，不请求 SKILL.md）。"""

    _install(world, "1.0.0")
    _install(world, "2.0.0")
    world.registry.requests.clear()
    code, result, _ = world.run("fetch", f"{NAME}@1.0.0")
    assert code == 1 and result["code"] == "SKILL_VERSION_NOT_NEWER" and world.registry.requests == []
    code, result, _ = world.run("fetch", f"{NAME}@2.0.0")
    assert code == 1 and result["code"] == "SKILL_VERSION_NOT_NEWER"
    assert not [path for path in world.registry.requests if path.endswith("SKILL.md")]


def test_a_replayed_version_is_refused_before_the_file_is_requested(world: Any) -> None:
    """P7-S13-A07 注册表把**已批准过**的版本在 index 里换了 sha256（同版本不同内容）→ ``SKILL_REGISTRY_REPLAY``，不请求 SKILL.md；
    无论请求的是已装版本还是更高版本；``.previous/`` 里那一版同样受保护。"""

    _install(world, "1.0.0")
    world.registry.sha_override["1.0.0"] = "f" * 64
    world.registry.requests.clear()
    for version in ("1.0.0", "2.0.0"):
        code, result, _ = world.run("fetch", f"{NAME}@{version}")
        assert code == 1 and result["code"] == "SKILL_REGISTRY_REPLAY", version
    assert not [path for path in world.registry.requests if path.endswith("SKILL.md")]

    world.registry.sha_override.clear()
    _install(world, "2.0.0")  # 1.0.0 进了 .previous/
    world.registry.sha_override["1.0.0"] = "e" * 64
    code, result, _ = world.run("fetch", f"{NAME}@3.0.0")
    assert code == 1 and result["code"] == "SKILL_REGISTRY_REPLAY"


def test_names_taken_by_builtin_or_operator_are_refused_with_zero_requests(world: Any) -> None:
    """P7-S13-A03 与内置 / 运维目录同名 → ``SKILL_NAME_TAKEN``，**零请求**；按目录名判，解析失败的同名运维目录也占着名字。"""

    code, result, _ = world.run("fetch", "calc-book-workflow@1.0.0")
    assert code == 1 and result["code"] == "SKILL_NAME_TAKEN" and world.registry.requests == []
    operator = world.tmp / "operator"
    (operator / "broken-operator-skill").mkdir(parents=True)
    env = {**world.env, "WATER_AGENT_SKILLS_DIR": str(operator)}
    code, result, _ = world.run("fetch", "broken-operator-skill@1.0.0", env_=env)
    assert code == 1 and result["code"] == "SKILL_NAME_TAKEN" and world.registry.requests == []


def test_argument_errors_send_nothing(world: Any) -> None:
    """P7-S13-A02 参数不是合法 ``name@version``（大写、``../``、换行、缺版本、两个 ``@``、``latest``）→ ``SKILL_INVALID``，零请求、零写入。"""

    for ref in ("Pump-Room@1.0.0", "../x@1.0.0", "ab-cd\n@1.0.0", f"{NAME}", f"{NAME}@1.0.0@2", f"{NAME}@latest", "@1.0.0", f"{NAME}@"):
        code, result, _ = world.run("fetch", ref)
        assert code == 1 and result["code"] == "SKILL_INVALID", repr(ref)
    assert world.registry.requests == [] and not (world.tmp / "quarantine").exists()


def test_config_errors_exit_two_and_overlapping_roots_are_refused(world: Any) -> None:
    """P7-S13-A02 缺必需的环境变量 / 隔离区与任一加载根互相包含或相同 → 退出码 2 ``SKILL_CONFIG_INVALID``；注册表基址缺失 → ``SKILL_REGISTRY_UNAVAILABLE``。"""

    code, result, _ = world.run("list", env_={})
    assert code == 2 and result["code"] == "SKILL_CONFIG_INVALID"
    nested = {**world.env, "WATER_AGENT_SKILLS_QUARANTINE_DIR": str(world.tmp / "installed" / "q")}
    code, result, _ = world.run("list", env_=nested)
    assert code == 2 and result["code"] == "SKILL_CONFIG_INVALID"
    same = {**world.env, "WATER_AGENT_SKILLS_QUARANTINE_DIR": world.env["WATER_AGENT_SKILLS_INSTALLED_DIR"]}
    assert world.run("list", env_=same)[0] == 2
    operator_inside = {**world.env, "WATER_AGENT_SKILLS_DIR": str(world.tmp / "quarantine" / "ops")}
    assert world.run("list", env_=operator_inside)[0] == 2
    without_url = {key: value for key, value in world.env.items() if key != "WATER_AGENT_SKILL_REGISTRY_URL"}
    code, result, _ = world.run("fetch", f"{NAME}@1.0.0", env_=without_url)
    assert code == 1 and result["code"] == "SKILL_REGISTRY_UNAVAILABLE"


def test_list_reports_status_and_candidates(world: Any) -> None:
    """P7-S13-A07 ``list`` 只读：已安装（版本 / 摘要前 8 位 / 批准人 / 状态）与隔离区候选；篡改后状态是 ``SKILL_TAMPERED``。"""

    _install(world, "1.0.0")
    world.run("fetch", f"{NAME}@2.0.0")
    code, result, _ = world.run("list")
    assert code == 0
    assert [(item["name"], item["version"], item["status"], item["approved_by"]) for item in result["installed"]] == [
        (NAME, "1.0.0", "ok", "eval-operator")
    ]
    assert [(item["name"], item["version"]) for item in result["candidates"]] == [(NAME, "2.0.0")]
    skill_file = world.tmp / "installed" / NAME / "SKILL.md"
    skill_file.write_bytes(skill_file.read_bytes() + b"x")
    assert world.run("list")[1]["installed"][0]["status"] == "SKILL_TAMPERED"


def test_audit_log_records_every_action_without_content_or_full_urls(world: Any) -> None:
    """P7-S13 审计：每个动作追加一行 JSON（``ts · action · ref · sha12 · os_user · code``，fetch 另有 ``registry_host``），0600、只增不改；
    失败动作也记；**不记正文、不记完整注册表 URL**（路径哨兵与上游响应体从不出现在审计、stdout、stderr 里）。"""

    import os
    import stat

    _install(world, "1.0.0")
    world.run("fetch", "calc-book-workflow@1.0.0")  # 失败
    world.run("approve", f"{NAME}@1.0.0", "--sha256", "0" * 64, io_=TtyIO(tty=False))  # 失败
    world.registry.status = 503
    world.run("fetch", f"{NAME}@2.0.0")
    audit = world.tmp / "state" / "skills-audit.jsonl"
    assert stat.S_IMODE(os.stat(audit).st_mode) == 0o600
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [(row["action"], row["code"]) for row in rows] == [
        ("fetch", "ok"), ("approve", "ok"), ("fetch", "SKILL_NAME_TAKEN"), ("approve", "SKILL_APPROVE_NO_TTY"),
        ("fetch", "SKILL_REGISTRY_UNAVAILABLE"),
    ]
    assert all(set(row) >= {"ts", "action", "ref", "sha12", "os_user", "code"} for row in rows)
    assert rows[0]["registry_host"] == "127.0.0.1" and "registry_host" not in rows[1]
    assert all(row["os_user"] == "eval-operator" and row["ts"] == "2026-10-22T03:00:00Z" for row in rows)
    text = audit.read_text(encoding="utf-8")
    for leaked in (SENTINEL_PATH, "SECRET-TOKEN", "http://", "https://", "先检索", "步骤"):
        assert leaked not in text, leaked


def test_failures_never_echo_the_registry_url_path_or_upstream_body(world: Any) -> None:
    """P7-S13-A01 注册表返回 401/403/429/5xx 时，CLI 的 stdout / stderr 只含稳定码与固定文案，不含完整 URL、路径哨兵、上游响应体。"""

    for status in (401, 403, 429, 500, 503):
        world.registry.status = status
        code, result, raw = world.run("fetch", f"{NAME}@1.0.0")
        assert code == 1 and result["code"] == "SKILL_REGISTRY_UNAVAILABLE", status
        for leaked in (SENTINEL_PATH, "SECRET-TOKEN", "upstream.internal", "sk-live", "http://"):
            assert leaked not in raw, (status, leaked)
    assert not (world.tmp / "quarantine").exists() or not list((world.tmp / "quarantine").iterdir())


def test_the_installed_lock_is_the_only_record_of_what_was_approved(world: Any) -> None:
    """P7-S13-A04 lock 记录批准人 = ``deps.user()``（OS 用户名，不是认证）与批准时间；大小与摘要等于实际安装的字节。"""

    _install(world, "1.0.0")
    entry = read_lock(world.tmp / "installed")[NAME]
    raw = (world.tmp / "installed" / NAME / "SKILL.md").read_bytes()
    assert (entry.approved_by, entry.approved_at) == ("eval-operator", "2026-10-22T03:00:00Z")
    assert entry.sha256 == hashlib.sha256(raw).hexdigest() and entry.size == len(raw)

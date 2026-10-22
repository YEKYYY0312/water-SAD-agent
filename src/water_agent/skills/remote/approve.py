"""批准、安装、回滚、卸载（P7-S13，规格 §6.4 / §6.6）。

**批准是 skill 的唯一信任根**：skill 正文是 ``SYSTEM_POLICY``，批准 = 把"写系统策略"交给一段文本。所以：

- **必须 TTY**（``stdin`` 与 ``stdout`` 都是终端），**没有 ``--yes``，没有任何旁路**——TTY 判定与输入读取封装成 :class:`ApprovalIO`
  由调用方注入（测试注入替身），**生产代码不读任何环境变量来跳过 TTY**；非 TTY 在读取任何输入、展示任何内容之前就拒绝；
- 展示全文、与已安装旧版的 diff、安检报告，然后要求**键入 skill 名**；EOF / 中断 / 不一致都算没确认；
- 三方摘要一致：``--sha256`` 的完整 64 位、隔离区文件的实际摘要、``candidate.json`` 里记录的摘要；
- **批准即启用**（D4）：批准动作已包含全文审阅与键入确认。

**原子安装与三个崩溃点**（评测里 ``partial_install_count = 0``）。实测（macOS / Linux 同）：``os.replace`` 不能把目录原子替换成
非空目录（errno 66），**单个文件可以**。所以升级时原子的单位是 ``SKILL.md`` 这一个文件::

    首次安装   暂存目录写好 SKILL.md ──[after_stage]── os.rename(暂存, <name>/) ──[after_replace]── 更新 lock
    升级       <name>/.previous/ 写好旧版 + entry.json ──[after_stage]── os.replace(新 SKILL.md, <name>/SKILL.md)
               ──[after_replace]── 更新 lock（临时文件 + fsync ──[before_lock_replace]── os.replace）

任何一点崩溃，重启后要么是旧状态要么是新状态，**加载期兜住"文件已换、lock 未更新"**：首次安装是 ``SKILL_UNLOCKED``，
升级是 ``SKILL_TAMPERED``（字节摘要对不上 lock）——都不加载（失败关闭），重试 ``approve`` 能完成（幂等：隔离区候选还在，
安装区里的同名同摘要文件会被覆盖到 lock 一致为止）。同一台主机用 ``fcntl.flock`` 串行 CLI 的写操作。

批准人记 ``getpass.getuser()`` 由调用方传入（``os.getlogin()`` 在无控制终端时返回 ``root``，不可信）。**那只是 OS 用户名，
不是认证。**
"""
from __future__ import annotations

import difflib
import fcntl
import json
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from water_agent.skills.errors import (
    SKILL_APPROVE_DIGEST_MISMATCH,
    SKILL_APPROVE_NAME_MISMATCH,
    SKILL_APPROVE_NO_TTY,
    SKILL_CANDIDATE_NOT_FOUND,
    SKILL_NAME_TAKEN,
    SKILL_NOT_INSTALLED,
    SKILL_ROLLBACK_UNAVAILABLE,
    SKILL_VERSION_NOT_NEWER,
)
from water_agent.skills.installed import (
    PREVIOUS_DIRNAME,
    PREVIOUS_ENTRY_FILENAME,
    SKILL_FILENAME,
    InstalledMeta,
    parse_entry,
    read_lock,
    sha256_of,
    write_lock_atomic,
)
from water_agent.skills.naming import version_tuple
from water_agent.skills.remote import quarantine as quarantine_store

#: 崩溃注入点（``crash(point)``）。生产传 ``None``。
CRASH_AFTER_STAGE = "after_stage"
CRASH_AFTER_REPLACE = "after_replace"
CRASH_BEFORE_LOCK_REPLACE = "before_lock_replace"
CRASH_POINTS = (CRASH_AFTER_STAGE, CRASH_AFTER_REPLACE, CRASH_BEFORE_LOCK_REPLACE)

CrashHook = Callable[[str], None]


class ApprovalIO(Protocol):
    """人机交互的最小面。生产实现读真实终端；测试注入脚本替身。**生产实现不得读环境变量跳过 TTY。**"""

    def is_tty(self) -> bool: ...

    def display(self, text: str) -> None: ...

    def read_line(self, prompt: str) -> str: ...


class ApprovalError(Exception):
    """批准 / 生命周期动作被拒。``code`` 稳定；``detail`` 只含固定说明（不含包内文本与路径）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}：{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class InstallResult:
    meta: InstalledMeta
    replaced: str | None


# ---------------------------------------------------------------------------
# 人工确认门
# ---------------------------------------------------------------------------


def require_human(io: ApprovalIO) -> None:
    """非 TTY 一律拒绝——**在展示任何内容、读取任何输入之前**。"""

    if not io.is_tty():
        raise ApprovalError(SKILL_APPROVE_NO_TTY, "需要在交互终端里运行；没有 --yes，也没有任何旁路")


def confirm_by_typing(io: ApprovalIO, expected_name: str, *, prompt: str) -> None:
    """要求键入 skill 名。EOF / 中断 / 不一致都算没确认。"""

    try:
        typed = io.read_line(prompt)
    except (EOFError, KeyboardInterrupt):
        raise ApprovalError(SKILL_APPROVE_NAME_MISMATCH, "没有收到确认输入") from None
    if typed.strip() != expected_name:
        raise ApprovalError(SKILL_APPROVE_NAME_MISMATCH, "键入的 skill 名与待操作的不一致")


# ---------------------------------------------------------------------------
# 写锁
# ---------------------------------------------------------------------------


@contextmanager
def exclusive_lock(directory: Path) -> Iterator[None]:
    """安装区的写锁（``fcntl.flock``，只覆盖同一台主机）：串行化 CLI 的写操作。"""

    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / ".install.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------


def _strings(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, (list, tuple)) else []


def render_review(
    candidate: quarantine_store.Candidate, text: str, installed: InstalledMeta | None, previous_text: str | None
) -> str:
    """给批准人看的完整文本：头部元数据 + 安检报告 + 与已装旧版的 diff + 全文。"""

    lines = [
        f"候选 skill：{candidate.name}@{candidate.version}",
        f"sha256：{candidate.sha256}",
        f"大小：{candidate.size} 字节；取回时间：{candidate.fetched_at}；注册表主机：{candidate.registry_host}",
        f"已通过的安检：{', '.join(_strings(candidate.safety.get('passed')))}",
    ]
    warnings = _strings(candidate.safety.get("warnings"))
    if warnings:
        lines.append(f"警告：{', '.join(warnings)}")
    lines.append(
        "注意：安检是确定性规则，**不是**边界；内容错误但措辞合规的规程任何规则都检不出。请通读下面的全文。"
    )
    if installed is not None and previous_text is not None:
        lines.append(f"\n--- 与已安装的 {installed.name}@{installed.version} 的差异 ---")
        diff = list(
            difflib.unified_diff(
                previous_text.splitlines(), text.splitlines(), fromfile=f"installed {installed.version}",
                tofile=f"candidate {candidate.version}", lineterm="",
            )
        )
        lines.extend(diff or ["（内容无差异）"])
    lines.append("\n--- 全文 ---")
    lines.append(text)
    lines.append("--- 全文结束 ---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def approve(
    *,
    name: str,
    version: str,
    sha256_arg: str,
    quarantine_dir: Path,
    installed_dir: Path,
    taken_names: frozenset[str],
    io: ApprovalIO,
    approved_by: str,
    approved_at: str,
    crash: CrashHook | None = None,
) -> InstallResult:
    """批准一个候选并安装。顺序：TTY → 找候选 → 同名核对 → 三方摘要 → 版本严格更新 → 展示 + 键入名字 → 原子安装。"""

    require_human(io)
    if name in taken_names:
        raise ApprovalError(SKILL_NAME_TAKEN, "与内置或运维目录的 skill 同名")
    candidate = quarantine_store.find_candidate(quarantine_dir, name, version)
    if candidate is None:
        raise ApprovalError(SKILL_CANDIDATE_NOT_FOUND, "隔离区里没有这个候选，请先 fetch")
    try:
        raw = candidate.skill_path.read_bytes()
    except OSError:
        raise ApprovalError(SKILL_CANDIDATE_NOT_FOUND, "隔离区里的候选文件不可读") from None
    actual = sha256_of(raw)
    if not (sha256_arg == actual == candidate.sha256) or len(raw) != candidate.size:
        raise ApprovalError(SKILL_APPROVE_DIGEST_MISMATCH, "--sha256、隔离区文件、candidate.json 三者的摘要不一致")

    with exclusive_lock(installed_dir):
        lock = read_lock(installed_dir)
        current = lock.get(name)
        previous_text: str | None = None
        if current is not None:
            if version_tuple(version) <= version_tuple(current.version):
                raise ApprovalError(SKILL_VERSION_NOT_NEWER, "批准的版本必须严格大于已安装版本；回到旧版只有 rollback")
            existing = installed_dir / name / SKILL_FILENAME
            previous_text = existing.read_text(encoding="utf-8", errors="replace") if existing.is_file() else None
        text = raw.decode("utf-8", errors="strict")
        io.display(render_review(candidate, text, current, previous_text))
        confirm_by_typing(io, name, prompt=f"确认安装 {name}@{version}，请键入 skill 名 {name}：")

        meta = InstalledMeta(
            name=name,
            version=version,
            sha256=actual,
            size=len(raw),
            registry_host=candidate.registry_host,
            approved_by=approved_by,
            approved_at=approved_at,
            from_version=current.version if current is not None else None,
        )
        _install_atomically(installed_dir, meta, raw, current, lock, crash)
    quarantine_store.discard(candidate)
    return InstallResult(meta=meta, replaced=current.version if current is not None else None)


def _fire(crash: CrashHook | None, point: str) -> None:
    if crash is not None:
        crash(point)


def _install_atomically(
    installed_dir: Path,
    meta: InstalledMeta,
    raw: bytes,
    current: InstalledMeta | None,
    lock: dict[str, InstalledMeta],
    crash: CrashHook | None,
) -> None:
    target_dir = installed_dir / meta.name
    new_lock = {**lock, meta.name: meta}
    if current is None:
        # 首次安装：暂存目录写好 → rename 成正式目录。若上次崩溃留下了孤儿目录（文件已在、lock 没有），先清掉再装。
        if target_dir.exists():
            shutil.rmtree(target_dir)
        staging = installed_dir / f".stage-{meta.name}-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(mode=0o755)
        (staging / SKILL_FILENAME).write_bytes(raw)
        _fsync_file(staging / SKILL_FILENAME)
        _fire(crash, CRASH_AFTER_STAGE)
        os.rename(staging, target_dir)
        _fire(crash, CRASH_AFTER_REPLACE)
    else:
        # 升级：旧版进 .previous/（只保留一版，先清掉更早的），新版先写成同目录暂存文件，再对 SKILL.md 做**文件级**原子替换。
        previous = target_dir / PREVIOUS_DIRNAME
        shutil.rmtree(previous, ignore_errors=True)
        previous.mkdir(mode=0o755)
        existing = target_dir / SKILL_FILENAME
        shutil.copy2(existing, previous / SKILL_FILENAME)
        (previous / PREVIOUS_ENTRY_FILENAME).write_text(
            json.dumps(current.to_entry(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        stage_file = target_dir / f".{SKILL_FILENAME}.stage-{os.getpid()}"
        stage_file.write_bytes(raw)
        _fsync_file(stage_file)
        _fire(crash, CRASH_AFTER_STAGE)
        os.replace(stage_file, existing)
        _fire(crash, CRASH_AFTER_REPLACE)
    write_lock_atomic(installed_dir, new_lock.values(), crash=crash)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------------------
# rollback / remove
# ---------------------------------------------------------------------------


def rollback(
    *,
    name: str,
    installed_dir: Path,
    io: ApprovalIO,
    approved_by: str,
    approved_at: str,
    crash: CrashHook | None = None,
) -> InstallResult:
    """回到 ``.previous/`` 里那一版（**唯一**能回到旧版的动作）。同样必须 TTY + 键入名字。"""

    require_human(io)
    with exclusive_lock(installed_dir):
        lock = read_lock(installed_dir)
        current = lock.get(name)
        if current is None:
            raise ApprovalError(SKILL_NOT_INSTALLED, "该 skill 没有安装")
        previous_dir = installed_dir / name / PREVIOUS_DIRNAME
        try:
            previous_meta = parse_entry(json.loads((previous_dir / PREVIOUS_ENTRY_FILENAME).read_text(encoding="utf-8")))
            raw = (previous_dir / SKILL_FILENAME).read_bytes()
        except (OSError, ValueError, Exception):  # noqa: BLE001 — 任何读不出来都是"没有可回的上一版"，不细分
            raise ApprovalError(SKILL_ROLLBACK_UNAVAILABLE, "没有可回到的上一版") from None
        if sha256_of(raw) != previous_meta.sha256 or len(raw) != previous_meta.size:
            raise ApprovalError(SKILL_ROLLBACK_UNAVAILABLE, "上一版的摘要与记录不符，不回滚")
        io.display(
            f"将把 {name} 从 {current.version} 回滚到 {previous_meta.version}（sha256 {previous_meta.sha256}）。\n"
            f"--- 回到的版本全文 ---\n{raw.decode('utf-8', errors='replace')}\n--- 全文结束 ---"
        )
        confirm_by_typing(io, name, prompt=f"确认回滚 {name}，请键入 skill 名 {name}：")
        restored = InstalledMeta(
            name=name,
            version=previous_meta.version,
            sha256=previous_meta.sha256,
            size=previous_meta.size,
            registry_host=previous_meta.registry_host,
            approved_by=approved_by,
            approved_at=approved_at,
            from_version=current.version,
        )
        target_dir = installed_dir / name
        stage_file = target_dir / f".{SKILL_FILENAME}.stage-{os.getpid()}"
        stage_file.write_bytes(raw)
        _fsync_file(stage_file)
        _fire(crash, CRASH_AFTER_STAGE)
        os.replace(stage_file, target_dir / SKILL_FILENAME)
        _fire(crash, CRASH_AFTER_REPLACE)
        write_lock_atomic(installed_dir, {**lock, name: restored}.values(), crash=crash)
        shutil.rmtree(previous_dir, ignore_errors=True)  # 回滚后不再保留：想再回去要重新 fetch + approve
    return InstallResult(meta=restored, replaced=current.version)


def remove(
    *,
    name: str,
    installed_dir: Path,
    io: ApprovalIO,
    crash: CrashHook | None = None,
) -> InstalledMeta:
    """卸载。同样必须 TTY + 键入名字。顺序：**先改 lock 再删文件**——崩在中间只会留下"目录在、lock 没有"（``SKILL_UNLOCKED``，不加载）。"""

    require_human(io)
    with exclusive_lock(installed_dir):
        lock = read_lock(installed_dir)
        current = lock.get(name)
        if current is None:
            raise ApprovalError(SKILL_NOT_INSTALLED, "该 skill 没有安装")
        io.display(f"将卸载 {name}@{current.version}（sha256 {current.sha256}）。")
        confirm_by_typing(io, name, prompt=f"确认卸载 {name}，请键入 skill 名 {name}：")
        remaining = {key: value for key, value in lock.items() if key != name}
        write_lock_atomic(installed_dir, remaining.values(), crash=crash)
        _fire(crash, CRASH_AFTER_REPLACE)
        shutil.rmtree(installed_dir / name, ignore_errors=True)
    return current


__all__ = [
    "CRASH_AFTER_REPLACE",
    "CRASH_AFTER_STAGE",
    "CRASH_BEFORE_LOCK_REPLACE",
    "CRASH_POINTS",
    "ApprovalError",
    "ApprovalIO",
    "InstallResult",
    "approve",
    "confirm_by_typing",
    "exclusive_lock",
    "remove",
    "render_review",
    "require_human",
    "rollback",
]

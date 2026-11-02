"""已安装 skill 的第三来源层：lock 读写、加载期按**原始字节**重算摘要（P7-S13，规格 §6.5）。

本模块是**库与面板唯一可以碰的"已安装"代码**（取回 / 批准在 ``skills/remote/``，只被运维 CLI 导入，P7-I12）。

布局::

    <WATER_AGENT_SKILLS_INSTALLED_DIR>/
        skills.lock.json                    {"version": 1, "skills": [{name, version, sha256, size,
                                              registry_host, approved_by, approved_at, from_version}]}
        <name>/SKILL.md                     当前版本
        <name>/.previous/SKILL.md           上一版（只保留一版）
        <name>/.previous/entry.json         上一版的 lock 条目（rollback 用）

**加载期纪律**（主防线是重算哈希，不是信任目录）：

- 对每个目录里的 skill 读**原始字节**，``sha256`` 与大小必须等于 lock → 否则 ``SKILL_TAMPERED``（不加载、**不删除**——
  证据留给运维）；
- 目录里有、lock 里没有，或 lock 里有、文件没了 → ``SKILL_UNLOCKED``（不加载）。批准流程的崩溃点落在这里：
  "文件已换、lock 未更新"要么是 ``SKILL_UNLOCKED``（首次安装）要么是 ``SKILL_TAMPERED``（升级：字节摘要对不上），
  两种都是**失败关闭**，绝不加载一个 lock 没批准的字节；
- lock 损坏 / 不可读 / ``version`` 不认 / 条目形状不对 → ``SKILL_LIBRARY_INVALID``，**装配期响亮失败**（与"运维目录
  设了却不存在"同语义：静默忽略等于规程悄悄消失）；设了变量但目录不存在同样响亮失败。

为什么按字节：``Skill.digest`` 是 ``sha256(read_text)[:16]``，``read_text`` 会翻译换行；批准时绑定的是字节，
所以重算的也必须是字节。``Skill.digest`` 的口径**不变**（``skill_p0`` 基线逐字不变），字节摘要另存在 ``InstalledMeta``。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from water_agent.skills.errors import (
    SKILL_LIBRARY_INVALID,
    SKILL_TAMPERED,
    SKILL_UNLOCKED,
    SkillLibraryError,
)
from water_agent.skills.naming import is_valid_name, is_valid_version

LOCK_FILENAME = "skills.lock.json"
SKILL_FILENAME = "SKILL.md"
PREVIOUS_DIRNAME = ".previous"
PREVIOUS_ENTRY_FILENAME = "entry.json"
LOCK_FORMAT_VERSION = 1
#: 单个已安装 skill 文件的字节上限（与下载上限一致；再大的文件在批准时就过不去）。
MAX_INSTALLED_BYTES = 64 * 1024
MAX_LOCK_BYTES = 1024 * 1024
MAX_LOCK_ENTRIES = 500

_ENTRY_KEYS = frozenset(
    {"name", "version", "sha256", "size", "registry_host", "approved_by", "approved_at", "from_version"}
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class InstalledMeta:
    """lock 里的一条记录（面板只读它；``registry_host`` 只有主机名）。"""

    name: str
    version: str
    sha256: str
    size: int
    registry_host: str
    approved_by: str
    approved_at: str
    from_version: str | None

    @property
    def sha8(self) -> str:
        return self.sha256[:8]

    def to_entry(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
            "size": self.size,
            "registry_host": self.registry_host,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "from_version": self.from_version,
        }

    def panel_view(self) -> dict[str, str]:
        """面板能看到的元数据：**不含路径**，不含完整摘要（只给前 8 位）。"""

        return {
            "sha8": self.sha8,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "registry_host": self.registry_host,
        }


def sha256_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _invalid(message: str) -> SkillLibraryError:
    return SkillLibraryError(SKILL_LIBRARY_INVALID, message)


def parse_entry(item: object) -> InstalledMeta:
    """校验一条 lock 条目。形状不对即 ``SKILL_LIBRARY_INVALID``（不回显条目内容）。"""

    if not isinstance(item, dict) or set(item) != _ENTRY_KEYS:
        raise _invalid("skills.lock.json 的条目形状不对")
    name, version = item["name"], item["version"]
    digest, size = item["sha256"], item["size"]
    if not is_valid_name(name) or not is_valid_version(version):
        raise _invalid("skills.lock.json 里有不合法的 name 或 version")
    if not (isinstance(digest, str) and _SHA256_RE.fullmatch(digest)):
        raise _invalid("skills.lock.json 里的 sha256 不是 64 位小写十六进制")
    if type(size) is not int or not 1 <= size <= MAX_INSTALLED_BYTES:
        raise _invalid("skills.lock.json 里的 size 不合法")
    from_version = item["from_version"]
    if from_version is not None and not is_valid_version(from_version):
        raise _invalid("skills.lock.json 里的 from_version 不合法")
    for key in ("registry_host", "approved_by", "approved_at"):
        value = item[key]
        if not isinstance(value, str) or not value or len(value) > 253 or any(ord(ch) < 0x20 for ch in value):
            raise _invalid(f"skills.lock.json 里的 {key} 不合法")
    return InstalledMeta(
        name=name,
        version=version,
        sha256=digest,
        size=size,
        registry_host=item["registry_host"],
        approved_by=item["approved_by"],
        approved_at=item["approved_at"],
        from_version=from_version,
    )


def read_lock(directory: Path) -> dict[str, InstalledMeta]:
    """读 ``<directory>/skills.lock.json``。

    文件不存在 = 还没有批准过任何 skill（返回空）；存在但不可读 / 超限 / 不是 JSON / ``version`` 不认 / 条目坏 /
    名字重复 = ``SKILL_LIBRARY_INVALID``（响亮失败，不当作"没有 skill"）。
    """

    path = directory / LOCK_FILENAME
    try:
        if not path.exists():
            return {}
        if path.stat().st_size > MAX_LOCK_BYTES:
            raise _invalid("skills.lock.json 超过大小上限")
        raw = path.read_bytes()
    except OSError as exc:
        raise _invalid(f"读不了 skills.lock.json：{type(exc).__name__}") from exc
    try:
        document = json.loads(raw.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeDecodeError):
        raise _invalid("skills.lock.json 不是合法 JSON") from None
    if not isinstance(document, dict) or set(document) != {"version", "skills"}:
        raise _invalid("skills.lock.json 的形状不对")
    if document["version"] != LOCK_FORMAT_VERSION:
        raise _invalid("skills.lock.json 的 version 不是本版本认识的格式")
    entries = document["skills"]
    if not isinstance(entries, list) or len(entries) > MAX_LOCK_ENTRIES:
        raise _invalid("skills.lock.json 的 skills 不是合理大小的列表")
    parsed: dict[str, InstalledMeta] = {}
    for item in entries:
        meta = parse_entry(item)
        if meta.name in parsed:
            raise _invalid("skills.lock.json 里有重复的 name")
        parsed[meta.name] = meta
    return parsed


def render_lock(entries: Iterable[InstalledMeta]) -> bytes:
    """lock 的规范序列化（按名字排序、固定缩进；同样的内容永远同样的字节）。"""

    document = {
        "version": LOCK_FORMAT_VERSION,
        "skills": [meta.to_entry() for meta in sorted(entries, key=lambda item: item.name)],
    }
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_lock_atomic(directory: Path, entries: Iterable[InstalledMeta], *, crash: object | None = None) -> None:
    """同目录临时文件 + ``os.fsync`` + ``os.replace``：读者要么看到旧 lock 要么看到新 lock，没有半截。

    ``crash`` 是崩溃注入钩子（``Callable[[str], None]``）：在 ``os.replace`` 之前以 ``"before_lock_replace"`` 调用，
    测试用它模拟"lock 临时文件已写好但还没替换"时进程消失。
    """

    target = directory / LOCK_FILENAME
    temporary = directory / f".{LOCK_FILENAME}.{os.getpid()}.tmp"
    payload = render_lock(entries)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if callable(crash):
            crash("before_lock_replace")
        os.replace(temporary, target)
    except BaseException:
        # 崩溃注入的 BaseException 也会走这里：临时文件留不留都不影响读者（读的是 target）；尽力清理
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    _fsync_directory(directory)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------------------
# 加载期：目录 + lock → 通过字节校验的 skill 文件
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstalledProblem:
    """一份没能加载的已安装 skill（名字是目录名，原样记下；不进任何提示）。"""

    name: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class InstalledFile:
    """通过字节校验的已安装 skill 文件：``text`` 是同一份字节解码的结果（避免"校验的与加载的不是同一份"）。"""

    meta: InstalledMeta
    text: str
    path: Path


def scan_installed(directory: Path) -> tuple[list[InstalledFile], list[InstalledProblem]]:
    """扫描安装区，逐个按字节重算摘要。

    返回 (通过校验的文件, 问题)。**读文件只读一次**：同一份字节既拿来算摘要也拿来解码，不存在"校验后被换掉"的窗口。
    """

    lock = read_lock(directory)
    ok: list[InstalledFile] = []
    problems: list[InstalledProblem] = []
    seen_dirs: set[str] = set()
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise _invalid(f"读不了已安装 skill 目录：{type(exc).__name__}") from exc
    for child in children:
        if child.name.startswith(".") or child.name == LOCK_FILENAME:
            continue
        if not child.is_dir():
            continue
        name = child.name
        seen_dirs.add(name)
        meta = lock.get(name)
        if meta is None:
            problems.append(InstalledProblem(name, SKILL_UNLOCKED, "安装区里有这个目录，但 lock 里没有"))
            continue
        path = child / SKILL_FILENAME
        try:
            if path.is_symlink() or not path.is_file():
                problems.append(InstalledProblem(name, SKILL_UNLOCKED, "lock 里有这个 skill，但文件不存在"))
                continue
            if path.stat().st_size > MAX_INSTALLED_BYTES:
                problems.append(InstalledProblem(name, SKILL_TAMPERED, "文件大小超过上限，与 lock 不符"))
                continue
            raw = path.read_bytes()
        except OSError as exc:
            problems.append(InstalledProblem(name, SKILL_UNLOCKED, f"读不了文件：{type(exc).__name__}"))
            continue
        if len(raw) != meta.size or sha256_of(raw) != meta.sha256:
            problems.append(InstalledProblem(name, SKILL_TAMPERED, "文件的字节摘要或大小与 lock 不符"))
            continue
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            problems.append(InstalledProblem(name, SKILL_TAMPERED, "文件不是合法 UTF-8"))
            continue
        ok.append(InstalledFile(meta=meta, text=text, path=path))
    for name in sorted(set(lock) - seen_dirs):
        problems.append(InstalledProblem(name, SKILL_UNLOCKED, "lock 里有这个 skill，但安装区里没有它的目录"))
    return ok, problems


def installed_dir_from_env(environ: object) -> Path | None:
    """``WATER_AGENT_SKILLS_INSTALLED_DIR``；未设 / 空 = 没有这一层。"""

    getter = getattr(environ, "get", None)
    raw = (getter(INSTALLED_DIR_ENV) if callable(getter) else None) or ""
    raw = str(raw).strip()
    return Path(raw).expanduser() if raw else None


INSTALLED_DIR_ENV = "WATER_AGENT_SKILLS_INSTALLED_DIR"


__all__ = [
    "INSTALLED_DIR_ENV",
    "LOCK_FILENAME",
    "LOCK_FORMAT_VERSION",
    "MAX_INSTALLED_BYTES",
    "PREVIOUS_DIRNAME",
    "PREVIOUS_ENTRY_FILENAME",
    "SKILL_FILENAME",
    "InstalledFile",
    "InstalledMeta",
    "InstalledProblem",
    "installed_dir_from_env",
    "parse_entry",
    "read_lock",
    "render_lock",
    "scan_installed",
    "sha256_of",
    "write_lock_atomic",
]

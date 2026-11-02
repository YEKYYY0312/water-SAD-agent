"""隔离区（P7-S13，规格 §6.3 第 6 步）。

``<WATER_AGENT_SKILLS_QUARANTINE_DIR>/<name>@<version>-<sha8>/`` 里是 ``SKILL.md`` + ``candidate.json``。

**这个目录永不在任何加载根下**：``build_skill_library`` 的三个来源目录（内置 / 运维 / 已安装）与隔离区必须**互不包含**
（互为祖先或相等都算包含），CLI 启动时校验，违反即退出码 2。候选在批准前不得被库看见——这条是靠"目录不在任何根下"
这个**结构事实**保证的，不是靠"库会忽略它"。目录权限 0700、文件 0600；隔离区不进备份（compose 里在 ``/var/lib/water-agent`` 之外）。

``candidate.json`` 只含：``name`` · ``version`` · ``sha256`` · ``size`` · ``fetched_at`` · ``registry_host`` · ``safety``
（检查 id 与警告）。**不含**完整注册表 URL、本机路径、包内文本。
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from water_agent.skills.naming import is_valid_name, is_valid_version

CANDIDATE_FILENAME = "candidate.json"
SKILL_FILENAME = "SKILL.md"
CANDIDATE_KEYS = frozenset({"name", "version", "sha256", "size", "fetched_at", "registry_host", "safety"})


class QuarantineError(Exception):
    """隔离区操作失败（目录不可写、候选损坏）。说明里不含路径。"""


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    version: str
    sha256: str
    size: int
    fetched_at: str
    registry_host: str
    safety: dict[str, object]
    directory: Path

    @property
    def sha8(self) -> str:
        return self.sha256[:8]

    @property
    def skill_path(self) -> Path:
        return self.directory / SKILL_FILENAME


def candidate_dirname(name: str, version: str, sha256: str) -> str:
    return f"{name}@{version}-{sha256[:8]}"


def roots_overlap(paths: Iterable[Path]) -> tuple[Path, Path] | None:
    """四个根目录是否有互相包含（相等、祖先 / 后代）。返回第一对重叠的路径，没有返回 ``None``。

    用 ``resolve()`` 消除符号链接与 ``..``：同一个物理目录的两个写法算重叠。不存在的目录按其自身绝对路径判。
    """

    resolved = [Path(os.path.realpath(path)) for path in paths]
    for index, first in enumerate(resolved):
        for second in resolved[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                return first, second
    return None


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def store_candidate(
    quarantine: Path,
    *,
    name: str,
    version: str,
    raw: bytes,
    sha256: str,
    fetched_at: str,
    registry_host: str,
    safety: Mapping[str, object],
) -> Candidate:
    """把通过全部检查的候选写进隔离区。**只在最后一步调用**——此前任何失败都磁盘零写入。

    先写到同目录的 ``.partial-*`` 暂存目录，再原子改名为正式目录名：读者（含 ``approve``）看不到写了一半的候选。
    同名同摘要的候选已存在时原样返回（幂等）；同名不同摘要共存（目录名带 sha8）。
    """

    if not is_valid_name(name) or not is_valid_version(version):
        raise QuarantineError("候选的 name 或 version 不合法")
    _ensure_private_dir(quarantine)
    final = quarantine / candidate_dirname(name, version, sha256)
    if final.exists():
        return _load(final)
    staging = quarantine / f".partial-{os.getpid()}-{sha256[:8]}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700)
    try:
        _write_private(staging / SKILL_FILENAME, raw)
        document = {
            "name": name,
            "version": version,
            "sha256": sha256,
            "size": len(raw),
            "fetched_at": fetched_at,
            "registry_host": registry_host,
            "safety": safety,
        }
        _write_private(
            staging / CANDIDATE_FILENAME,
            (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.chmod(staging, 0o700)
        os.replace(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return _load(final)


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _load(directory: Path) -> Candidate:
    try:
        document = json.loads((directory / CANDIDATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QuarantineError("candidate.json 不可读或不是合法 JSON") from exc
    if not isinstance(document, dict) or set(document) != CANDIDATE_KEYS:
        raise QuarantineError("candidate.json 的形状不对")
    try:
        return Candidate(
            name=str(document["name"]),
            version=str(document["version"]),
            sha256=str(document["sha256"]),
            size=int(document["size"]),
            fetched_at=str(document["fetched_at"]),
            registry_host=str(document["registry_host"]),
            safety=dict(document["safety"]),
            directory=directory,
        )
    except (TypeError, ValueError) as exc:
        raise QuarantineError("candidate.json 的字段类型不对") from exc


def find_candidate(quarantine: Path, name: str, version: str) -> Candidate | None:
    """按 ``name@version`` 找候选（同名同版本只会有一个摘要；若有多个取最近取回的）。目录不存在 = 没有。"""

    if not is_valid_name(name) or not is_valid_version(version) or not quarantine.is_dir():
        return None
    prefix = f"{name}@{version}-"
    found: list[Candidate] = []
    for child in sorted(quarantine.iterdir(), key=lambda item: item.name):
        if child.name.startswith(prefix) and child.is_dir():
            try:
                found.append(_load(child))
            except QuarantineError:
                continue
    return max(found, key=lambda item: item.fetched_at) if found else None


def list_candidates(quarantine: Path) -> list[Candidate]:
    if not quarantine.is_dir():
        return []
    out: list[Candidate] = []
    for child in sorted(quarantine.iterdir(), key=lambda item: item.name):
        if child.name.startswith(".") or not child.is_dir():
            continue
        try:
            out.append(_load(child))
        except QuarantineError:
            continue
    return out


def discard(candidate: Candidate) -> None:
    """批准成功后删除候选（已安装的字节与 lock 是唯一记录；审计里留有 sha12）。"""

    shutil.rmtree(candidate.directory, ignore_errors=True)


__all__ = [
    "CANDIDATE_FILENAME",
    "Candidate",
    "QuarantineError",
    "candidate_dirname",
    "discard",
    "find_candidate",
    "list_candidates",
    "roots_overlap",
    "store_candidate",
]

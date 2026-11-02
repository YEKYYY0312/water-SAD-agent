"""MCP server 的凭据槽位（P7-S6，``docs/design/16-p7-plugin-mcp.md`` §3.8）。

"凭据引用"的落点：运维在仓库默认层或部署覆盖层给 server 声明 ``credential_env``——要注入子进程
的**环境变量名**；Web 扩展面板只能给已声明的槽位填**值**。名字由运维定、值由管理员填：让浏览器
决定环境变量名就等于让它执行代码（``LD_PRELOAD``、``NODE_OPTIONS``、``PYTHONSTARTUP``……），所以
槽位名在加载期就拒绝加载器 / 解释器会执行的变量（:func:`validate_slot_name`），即便运维误声明也不行。

值与 LLM 凭据同一套（``config/credentials.py``）：只写不读，状态只有 ``configured / updated_at``；
存在凭据卷 ``<credentials_dir>/mcp/<server_id>/<ENV_NAME>``（文件 0600、目录 0700）。凭据卷刻意
不在 ``/var/lib/water-agent`` 之下，不进备份；密钥不进任何配置文件。路径只由已校验的 server_id 与
槽位名拼出。连接时由 ``config.child_env`` 注入：``env_file`` < 凭据槽位 < 内联 ``env``（槽位与内联
``env`` 重名在加载期已拒绝）。没填值的槽位让整台 server 在连接前跳过（``MCP_CREDENTIAL_MISSING``）。

本模块是叶子模块（只依赖 ``config/credentials.py``），``mcp/config.py`` 在顶层导入它。
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from water_agent.config.credentials import (
    CredentialStoreUnavailableError,
    credentials_dir,
    validate_credential_value,
)

#: 槽位名：ASCII 大写环境变量名。
_SLOT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
#: 与 ``config._SERVER_ID_RE`` 同形。这里再校验一次，是因为凭据文件的路径直接由它拼出。
_SERVER_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: 不能作为槽位的变量：子进程默认透传的 PATH / HOME，以及 shell、动态链接器与解释器启动时会读来
#: 执行代码或改变加载行为的变量。
FORBIDDEN_SLOT_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "BASH_ENV",
        "ENV",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "PROMPT_COMMAND",
        "IFS",
        "GCONV_PATH",
    }
)
FORBIDDEN_SLOT_PREFIXES = ("LD_", "DYLD_", "PYTHON", "NODE_", "PERL", "RUBY")

#: 凭据卷里 MCP 槽位的子目录。
MCP_CREDENTIALS_SUBDIR = "mcp"


@dataclass(frozen=True)
class CredentialSlotStatus:
    """一个槽位的状态。**刻意不含值本身，也不含任何片段。**"""

    name: str
    configured: bool
    updated_at: str | None


def validate_slot_name(name: str) -> str:
    """槽位名的加载期校验：大写环境变量名，且不是加载器 / 解释器会执行的变量。"""
    if not isinstance(name, str) or not _SLOT_NAME_RE.match(name):
        raise ValueError(f"凭据槽位 {name!r} 必须是大写环境变量名（如 CAD_API_TOKEN）")
    if name in FORBIDDEN_SLOT_NAMES or name.startswith(FORBIDDEN_SLOT_PREFIXES):
        raise ValueError(
            f"凭据槽位 {name!r} 不能使用：PATH / HOME 与 shell、动态链接器、解释器会读来执行代码的变量"
            f"（{sorted(FORBIDDEN_SLOT_NAMES)}，前缀 {list(FORBIDDEN_SLOT_PREFIXES)}）一律拒绝——"
            "槽位的值来自浏览器"
        )
    return name


def slot_path(server_id: str, env_name: str) -> Path:
    """凭据文件路径。两个分量都先过形状校验，路径里不会出现调用方的任意文本。"""
    if not _SERVER_SEGMENT_RE.match(server_id):
        raise ValueError("server_id 形状不合法")
    validate_slot_name(env_name)
    return credentials_dir() / MCP_CREDENTIALS_SUBDIR / server_id / env_name


def read_slot(server_id: str, env_name: str) -> str | None:
    """读槽位的值；没填、空文件、读不了一律 ``None``。只给 ``child_env`` 注入子进程用。"""
    try:
        value = slot_path(server_id, env_name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def missing_slots(server_id: str, names: Iterable[str]) -> tuple[str, ...]:
    """声明了但没填值的槽位名，按声明顺序。"""
    return tuple(name for name in names if read_slot(server_id, name) is None)


def slot_status(server_id: str, env_name: str) -> CredentialSlotStatus:
    path = slot_path(server_id, env_name)
    configured = read_slot(server_id, env_name) is not None
    updated_at: str | None = None
    if configured:
        try:
            updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        except OSError:
            updated_at = None
    return CredentialSlotStatus(env_name, configured, updated_at)


def write_slot(server_id: str, env_name: str, value: str) -> None:
    """写入槽位的值（校验同 LLM 凭据：非空、不含空白、不短于 8 个字符）。

    以 0600 建临时文件再 ``os.replace``：读者看不到半截内容，值也不会有任何一刻以默认权限落盘。
    值不足或含空白抛 ``InvalidCredentialError``；凭据卷不可写抛 ``CredentialStoreUnavailableError``。
    """
    cleaned = validate_credential_value(value)
    target = slot_path(server_id, env_name)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        for directory in (target.parent.parent, target.parent):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, cleaned.encode("utf-8"))
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise CredentialStoreUnavailableError(str(credentials_dir())) from error


def delete_slot(server_id: str, env_name: str) -> None:
    """清掉槽位的值（幂等）。之后这台 server 在连接前跳过（``MCP_CREDENTIAL_MISSING``）。"""
    target = slot_path(server_id, env_name)
    try:
        target.unlink(missing_ok=True)
    except OSError as error:
        raise CredentialStoreUnavailableError(str(credentials_dir())) from error


__all__ = [
    "FORBIDDEN_SLOT_NAMES",
    "FORBIDDEN_SLOT_PREFIXES",
    "MCP_CREDENTIALS_SUBDIR",
    "CredentialSlotStatus",
    "delete_slot",
    "missing_slots",
    "read_slot",
    "slot_path",
    "slot_status",
    "validate_slot_name",
    "write_slot",
]

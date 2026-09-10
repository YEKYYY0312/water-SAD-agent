"""MCP 网关版本化配置（P4 Step 23）。

设计约束（规格 §3）：

1. **文件不存在 = 功能关闭**（特性开关语义，不是错误）；
   **文件存在但坏 = ``MCP_CONFIG_INVALID``**（响亮失败）——"写错了却静默跳过
   全部 server"是最危险的静默降级。
2. **extra="forbid"**：配置字段拼错必须响亮失败，不能被当"未配置"放行
   （与治理契约同纪律）。
3. **最小权限**：server 子进程 env 只透传 ``PATH``/``HOME`` 两个本机变量；
   其余环境（含 API Key）必须由 ``env_file`` 或内联 ``env`` **显式**提供。
   ``env``（dict）覆盖 ``env_file``（key=value 文本文件）—— 同一 key 只留一个真源。
4. **工具白名单 fail closed**：``allow=None`` = 全拒（任何远程工具都不暴露）。
   工具必须被人工逐名放行；"server 上有这个工具"本身不构成暴露资格。

设计取舍（`p4-step-23-decision.md`）：
- 早期草稿的 ``env_passthrough``（按名透传本机变量）被 ``env_file + env`` 的
  **显式给值**形态取代 —— 只列变量名仍会把本机当前值整体送进第三方进程，
  显式给值才可审计；
- 早期草稿的 ``tool_bindings``（按工具绑定任意 Capability）被 ``allow`` 白名单 +
  统一 ``Capability.MCP_INVOKE`` 取代 —— 按 server 绑定能力会把"远程工具能做什么"
  变成配置可写的自由变量，而能力枚举是**代码定义的偏序**；MCP 工具统一持
  ``MCP_INVOKE``（默认无主体持有，装配期按配置授予），权限面由**白名单 +
  主体能力授予**两侧共同收敛，不给配置"随意授危险能力"的口子。
"""
from __future__ import annotations

import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from water_agent.mcp.errors import MCP_CONFIG_INVALID, MCPError

#: 配置版本。**改任何字段语义都要改它**（与治理配置同纪律）。
MCP_CONFIG_VERSION = "p4-step23-2026-09-08"

#: 配置文件位置覆盖环境变量。
ENV_MCP_CONFIG = "WATER_AGENT_MCP_CONFIG"

#: 默认配置文件名（相对仓库根，由调用方传入 base）。
DEFAULT_CONFIG_FILENAME = "data/mcp_servers.json"

_SERVER_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: 子进程默认透传的本机变量。**只有这两个**：PATH（找可执行文件）、
#: HOME（多数 server 读用户配置目录）。API Key/凭据一律不在默认透传内。
DEFAULT_ENV_PASSTHROUGH: tuple[str, ...] = ("PATH", "HOME")


class ServerTransport(str, Enum):
    """传输方式。本步只支持 stdio；HTTP/SSE 是 blocked 待办（规格 §7.3）。"""

    STDIO = "stdio"


class MCPServerConfig(BaseModel):
    """一个本地 stdio MCP server 的连接与治理配置。

    ``command``/``args`` 由**人工配置**提供，装配期原样以 list 形式 spawn
    （无 shell、无字符串插值）。模型的任何输入都不能改变它们 —— 这是宿主
    集成面，与 ``DRAWING_ROOT`` 的装配期注入同边界（规格 §5）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: str = Field(description="server 命名空间 id（本地工具名前缀来源）")
    command: str = Field(min_length=1, description="可执行文件（配置可写 list，加载时归一化为 str+args）")
    args: tuple[str, ...] = Field(default=())
    transport: ServerTransport = ServerTransport.STDIO
    #: 内联环境变量（key=value，覆盖 env_file 的同名项）。显式给值才进子进程。
    env: dict[str, str] = Field(default_factory=dict)
    #: 环境文件路径（key=value 文本，``#`` 注释与空行忽略）。
    #: 相对路径按**配置文件所在目录**解析；缺失是配置错误（响亮失败）。
    env_file: str | None = None
    #: 工具白名单。**None = 全拒（fail closed）**；列出的名字与 server
    #: tools/list 清单**精确匹配**。
    allow: tuple[str, ...] | None = None
    #: server 上的写/危险操作是否需要人工确认门。``True`` 且确认门未接线
    #: （``MCPGatewayConfig.confirmation_handler_ready=False``）时整台 server
    #: 的工具**不暴露**（宁可不给，也不给"先问一句再执行"的假象）。
    human_confirmation: bool = False
    startup_timeout_seconds: float = Field(default=10.0, gt=0)
    call_timeout_seconds: float = Field(default=30.0, gt=0)
    max_response_bytes: int = Field(default=65536, gt=0)

    @field_validator("server_id")
    @classmethod
    def _validate_server_id(cls, value: str) -> str:
        if not _SERVER_ID_RE.match(value):
            raise ValueError(
                f"server_id {value!r} 必须匹配 {_SERVER_ID_RE.pattern!r}"
                "（它会成为本地工具名前缀 mcp_<server>_）"
            )
        return value

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str | list[str]) -> str:
        # 归一化：list 形式取首元素作为 command（args 由 model_validator 处理）
        if isinstance(value, list):
            if not value:
                raise ValueError("command list 不得为空")
            value = value[0]
        if not isinstance(value, str):
            raise TypeError(f"command 必须是 str 或 list[str]，得到 {type(value).__name__}")
        if "\x00" in value or "\n" in value:
            raise ValueError("command 不得含 NUL 或换行")
        if value.strip() != value:
            raise ValueError(f"command {value!r} 不得含首尾空白")
        return value

    @field_validator("allow")
    @classmethod
    def _validate_allow(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        seen: set[str] = set()
        for tool in value:
            if tool.strip() != tool or any(ch.isspace() for ch in tool):
                raise ValueError(
                    f"allow 项 {tool!r} 不得含空白（与 tools/list 清单精确匹配）"
                )
            if tool in seen:
                raise ValueError(f"allow 项 {tool!r} 重复")
            seen.add(tool)
        return value

    @field_validator("env")
    @classmethod
    def _validate_env_keys(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if not name or not name.isidentifier() or not name.isupper():
                raise ValueError(
                    f"env 的 key {name!r} 必须是大写环境变量名（如 PATH、HTTPS_PROXY）"
                )
        return value

    @model_validator(mode="before")
    @classmethod
    def _normalize_command_list(cls, data: Any) -> Any:
        """归一化 command list 形式：[exec, ...args] → command=exec, args=(...args)。"""
        if not isinstance(data, dict):
            return data
        cmd = data.get("command")
        if isinstance(cmd, list) and cmd:
            # list 形式：首元素是 command，剩余合并到 args
            data = dict(data)
            data["command"] = cmd[0]
            existing_args = data.get("args", ())
            if not isinstance(existing_args, (list, tuple)):
                existing_args = ()
            data["args"] = tuple(cmd[1:]) + tuple(existing_args)
        return data


class MCPGatewayConfig(BaseModel):
    """配置根：版本 + 确认门状态 + server 清单。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default=MCP_CONFIG_VERSION, min_length=1)
    #: 危险操作人工确认门是否已接线。``False`` 时任何
    #: ``human_confirmation=true`` 的 server 的工具都不暴露。
    confirmation_handler_ready: bool = False
    servers: tuple[MCPServerConfig, ...] = Field(default=())

    @model_validator(mode="after")
    def _validate_server_ids_unique(self) -> MCPGatewayConfig:
        ids = [server.server_id for server in self.servers]
        duplicates = {name for name in ids if ids.count(name) > 1}
        if duplicates:
            raise ValueError(f"server_id 重复：{sorted(duplicates)}")
        return self

    def enabled_servers(self) -> tuple[MCPServerConfig, ...]:
        return tuple(server for server in self.servers if server.transport is ServerTransport.STDIO)


def default_config_path(*, repo_root: Path | None = None) -> Path:
    """配置文件路径：环境变量覆盖 → 仓库根默认位置。"""
    override = os.environ.get(ENV_MCP_CONFIG, "")
    if override.strip():
        return Path(override).expanduser()
    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[3]
    return root / DEFAULT_CONFIG_FILENAME


def load_gateway_config(path: str | Path | None = None) -> MCPGatewayConfig | None:
    """加载网关配置。

    - 路径不存在（或未显式指定且默认文件不存在）→ ``None``（功能关闭）；
    - 显式指定的路径不存在 → ``MCP_CONFIG_INVALID``（显式给路径却找不到，
      与"没配"是两件事，不能都算关闭）；
    - 解析/校验失败 → ``MCP_CONFIG_INVALID``（响亮失败）。
    """
    resolved = Path(path).expanduser() if path is not None else default_config_path()
    if not resolved.exists():
        if path is not None:
            raise MCPError(MCP_CONFIG_INVALID, f"指定的 MCP 配置文件不存在：{resolved}")
        return None
    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise MCPError(MCP_CONFIG_INVALID, f"无法读取 {resolved}：{exc}") from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MCPError(
            MCP_CONFIG_INVALID,
            f"{resolved} 不是合法 JSON（行 {exc.lineno} 列 {exc.colno}）：{exc.msg}",
        ) from exc
    try:
        return MCPGatewayConfig.model_validate(payload)
    except ValueError as exc:
        raise MCPError(MCP_CONFIG_INVALID, f"{resolved} 校验失败：{exc}") from exc


def _parse_env_file(text: str) -> dict[str, str]:
    """解析 key=value 环境文件；``#`` 注释与空行忽略，值不做 shell 展开。"""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key and key.isidentifier() and key.isupper():
            parsed[key] = value.strip()
    return parsed


def child_env(config: MCPServerConfig, *, env_file_dir: Path | str | None = None) -> dict[str, str]:
    """构造子进程 env：默认透传 + env_file + 内联 env（内联覆盖文件）。

    - ``env_file`` 为相对路径时按 ``env_file_dir``（配置所在目录）解析；
      env_file 配置了但缺失 = 配置错误（响亮失败，不静默裸奔）。
    - 内联 ``env`` 与 env_file 的同名 key 以**内联**为准（单一真源）。
    - 本机其他变量（含 API Key）一律不进入子进程（最小权限，规格 §3）。
    """
    child_env_vars: dict[str, str] = {}
    for name in DEFAULT_ENV_PASSTHROUGH:
        value = os.environ.get(name)
        if value is not None:
            child_env_vars[name] = value

    if config.env_file is not None:
        env_path = Path(config.env_file).expanduser()
        if not env_path.is_absolute():
            base = Path(env_file_dir).expanduser() if env_file_dir is not None else Path.cwd()
            env_path = base / env_path
        if not env_path.exists():
            raise MCPError(
                MCP_CONFIG_INVALID,
                f"server {config.server_id!r} 的 env_file 不存在：{env_path}",
            )
        try:
            child_env_vars.update(_parse_env_file(env_path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise MCPError(
                MCP_CONFIG_INVALID, f"无法读取 env_file {env_path}：{exc}"
            ) from exc

    child_env_vars.update(config.env)
    return child_env_vars


__all__ = [
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_ENV_PASSTHROUGH",
    "ENV_MCP_CONFIG",
    "MCP_CONFIG_VERSION",
    "MCPGatewayConfig",
    "MCPServerConfig",
    "ServerTransport",
    "child_env",
    "default_config_path",
    "load_gateway_config",
]

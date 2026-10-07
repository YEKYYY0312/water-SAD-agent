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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from water_agent.mcp.errors import MCP_CONFIG_INVALID, MCPError

#: 配置版本。**改任何字段语义都要改它**（与治理配置同纪律）。``version`` 是写进配置文件的
#: 元数据、加载时不比对，旧配置照常有效。
#: P7-S1 收窄了 ``confirmation_handler_ready`` 的取值，当时未递进（没有新增字段）。
#: P7-S3 新增 ``disabled`` / ``cwd`` 字段、收紧 ``command``、``env_file`` 改按配置文件所在
#: 目录解析，递进。
#: P7-S5 新增 ``lifecycle``（``per_run`` / ``shared``），递进。
#: P7-S6 新增第三层 Web 管理层（``WATER_AGENT_MCP_WEB_LAYER``）与 ``credential_env`` 凭据槽位，递进。
MCP_CONFIG_VERSION = "p7-s6-2026-10-07"

#: 配置文件位置覆盖环境变量。
ENV_MCP_CONFIG = "WATER_AGENT_MCP_CONFIG"

#: 部署覆盖层文件位置（P7-S3，不入库）。未设 = 没有覆盖层；设了但文件不存在 = 配置错误。
ENV_MCP_OVERLAY = "WATER_AGENT_MCP_OVERLAY"

#: Web 管理层文件位置（P7-S6）。未设 = 没有这一层；设了但文件不存在 = 空层（面板首次写入时创建）。
#: 合并顺序：仓库默认层 → 部署覆盖层 → Web 管理层。读写见 ``mcp/web_layer.py``。
ENV_MCP_WEB_LAYER = "WATER_AGENT_MCP_WEB_LAYER"

#: Web 管理层每台 server 能改的键（P7-S6）。其余字段——``command`` / ``args`` / ``env`` /
#: ``env_file`` / ``cwd`` / ``lifecycle`` / ``human_confirmation`` / 超时 / 凭据槽位——只能由运维写在
#: 下面两层：面板能写它们，就等于浏览器能决定 agent 执行什么。
WEB_LAYER_SERVER_KEYS = frozenset({"server_id", "disabled", "allow"})

#: 覆盖层顶层只允许这两个键。``confirmation_handler_ready`` 只能写在仓库默认层：
#: 覆盖层能改它，S1 的确认门限制就能从部署侧绕开。
_OVERLAY_TOP_LEVEL_KEYS = frozenset({"version", "servers"})

#: 默认配置文件名（相对仓库根，由调用方传入 base）。
DEFAULT_CONFIG_FILENAME = "data/mcp_servers.json"

_SERVER_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: 子进程默认透传的本机变量。**只有这两个**：PATH（找可执行文件）、
#: HOME（多数 server 读用户配置目录）。API Key/凭据一律不在默认透传内。
DEFAULT_ENV_PASSTHROUGH: tuple[str, ...] = ("PATH", "HOME")


def check_allow_items(value: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """``allow`` 白名单的形状：工具名不含空白、不重复（与 tools/list 清单精确匹配）。

    ``MCPServerConfig`` 与 Web 管理层（P7-S6）共用这一份规则。
    """
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
    #: server 上的写/危险操作是否需要人工确认门（P7-S7 起是**每次执行前确认**）。``True`` 时：
    #: 接了确认门的入口（Web 会话）照常暴露，工具在 ToolNode 里是延迟工具、每次执行前由 run 所有者
    #: 批准；没有确认门的入口（CLI、评测 runner）整台 server 的工具**不暴露**（宁可不给，也不给
    #: "先问一句再执行"的假象）。
    human_confirmation: bool = False
    startup_timeout_seconds: float = Field(default=10.0, gt=0)
    call_timeout_seconds: float = Field(default=30.0, gt=0)
    max_response_bytes: int = Field(default=65536, gt=0)
    #: 停用（P7-S3）：不 spawn、不暴露任何工具、不进 manifest。通常写在部署覆盖层里，
    #: 只需 ``{"server_id": ..., "disabled": true}``。
    disabled: bool = False
    #: 子进程工作目录（P7-S3）。必须是绝对路径；加载期再校验它存在。不配置时每次连接
    #: 新建一个空临时目录——子进程不继承 agent 的工作目录，模型借工具参数里的相对路径
    #: 读不到 agent 自己的文件（F15）。
    cwd: str | None = None
    #: 连接生命周期（P7-S5）。``per_run``（缺省）：每个 run 现开现关，与此前一致。``shared``：
    #: Web 进程内长驻、被所有用户的 run 复用——**声明 shared 即声明这台 server 不持有按用户
    #: 区分的状态**（例如"当前打开的文档"），agent 无法替运维验证这一点。CLI 与评测 runner
    #: 不看这个字段，始终现开现关。
    lifecycle: Literal["per_run", "shared"] = "per_run"

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
        # P7-S3：子进程工作目录不再是 agent 的工作目录，含 "/" 的相对路径的含义会悄悄改变；
        # 只接受绝对路径或不含 "/" 的裸命令名（按 PATH 查找）。
        if "/" in value and not Path(value).is_absolute():
            raise ValueError(
                f"command {value!r} 必须是绝对路径或不含 '/' 的命令名（按 PATH 查找）"
            )
        return value

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError(f"cwd {value!r} 必须是绝对路径")
        return value

    @field_validator("allow")
    @classmethod
    def _validate_allow(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return check_allow_items(value)

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
    #: 历史字段：确认门是否可用曾由它声明。P7-S7 起由**入口**决定（只有 Web 会话工厂接了确认门，
    #: 见 ``wiring.open_gateway_sessions(confirmation_ready=)``），配置里只接受 ``False``，见
    #: :meth:`_confirmation_gate_not_wired`。
    confirmation_handler_ready: bool = False
    servers: tuple[MCPServerConfig, ...] = Field(default=())

    @field_validator("confirmation_handler_ready")
    @classmethod
    def _confirmation_gate_not_wired(cls, value: bool) -> bool:
        """P7-S1（F7）/ P7-S7：配置不能声明"确认门可用"。

        S1 之前把它改成 ``True``，``human_confirmation=true`` 的 server 就会直接暴露、工具可被
        调用，却没有任何人被问过。S7 接通确认门之后，可用与否取决于**入口**有没有确认 UI——写在
        配置里会让 CLI、评测 runner 这类没有确认门的入口凭空"可确认"。文件加载与直接构造走同一个
        校验；``load_gateway_config`` 把它包成 ``MCP_CONFIG_INVALID``。
        """
        if value:
            raise ValueError(
                "confirmation_handler_ready 不能写成 true：人工确认门是否可用由入口决定"
                "（Web 会话已接线，human_confirmation=true 的 server 在 Web 下照常暴露、每次执行前"
                "由 run 所有者确认；CLI 与评测 runner 没有确认门，这类 server 整台不暴露）。"
                "请删除该键或保持 false"
            )
        return value

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
    payload = _read_json_object(resolved, raw_text)
    try:
        config = MCPGatewayConfig.model_validate(_resolve_env_files(payload, resolved.parent))
    except ValueError as exc:
        raise MCPError(MCP_CONFIG_INVALID, f"{resolved} 校验失败：{exc}") from exc
    _check_paths_exist(config, resolved)
    return config


def merge_overlay(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """把部署覆盖层合并进仓库默认层（纯函数，输入输出都是 dict）。

    - 覆盖层顶层只允许 ``version`` / ``servers``；
    - 按 ``server_id`` 合并：默认层没有的 id 是新增 server，有的 id 逐字段覆盖；
    - 字段值**整体替换**，不做深合并：``allow`` 写了就完全以覆盖层为准，``env`` / ``args``
      同理——覆盖层不能把白名单"合并"成比两层都更宽的集合。

    返回的 dict 还要经 ``MCPGatewayConfig.model_validate`` 才算合法配置。
    """
    unknown = set(overlay) - _OVERLAY_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(
            f"覆盖层只能写 {sorted(_OVERLAY_TOP_LEVEL_KEYS)}，不能写 {sorted(unknown)}"
            "（confirmation_handler_ready 只能写在仓库默认层）"
        )
    overlay_servers = overlay.get("servers", [])
    if not isinstance(overlay_servers, list):
        raise TypeError("覆盖层的 servers 必须是列表")
    merged_servers: list[Any] = [
        dict(server) if isinstance(server, dict) else server
        for server in base.get("servers", []) or []
    ]
    index = {
        server.get("server_id"): position
        for position, server in enumerate(merged_servers)
        if isinstance(server, dict)
    }
    for patch in overlay_servers:
        if not isinstance(patch, dict) or not isinstance(patch.get("server_id"), str):
            raise TypeError("覆盖层的每台 server 都必须是对象且写 server_id")
        position = index.get(patch["server_id"])
        if position is None:
            index[patch["server_id"]] = len(merged_servers)
            merged_servers.append(dict(patch))
        else:
            merged_servers[position] = {**merged_servers[position], **patch}
    merged = {key: value for key, value in base.items() if key != "servers"}
    if "version" in overlay:
        merged["version"] = overlay["version"]
    merged["servers"] = merged_servers
    return merged


def merge_web_layer(
    base: dict[str, Any], overrides: Iterable[Mapping[str, Any]]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """把 Web 管理层叠在运维两层的合并结果上（P7-S6，纯函数）。

    - 每条只能写 :data:`WEB_LAYER_SERVER_KEYS`，写了别的键即 ``ValueError``；
    - **不能新增 server**：下层没有的 ``server_id`` 是孤立条目，不进合并结果，按出现顺序返回给调用方
      （孤立条目只能停用 / 放行一台不存在的 server，没有效果；为它让整个配置失败，等于运维删一台
      server 就把 Web 的 MCP 全停了）；
    - ``disabled`` / ``allow`` 与覆盖层同规则**整体替换**。

    返回的 dict 还要经 ``MCPGatewayConfig.model_validate`` 才算合法配置。
    """
    merged_servers: list[Any] = [
        dict(server) if isinstance(server, dict) else server
        for server in base.get("servers", []) or []
    ]
    index = {
        server.get("server_id"): position
        for position, server in enumerate(merged_servers)
        if isinstance(server, dict)
    }
    orphans: list[str] = []
    for patch in overrides:
        unknown = set(patch) - WEB_LAYER_SERVER_KEYS
        if unknown:
            raise ValueError(
                f"Web 管理层每台 server 只能写 {sorted(WEB_LAYER_SERVER_KEYS)}，不能写 {sorted(unknown)}"
            )
        server_id = patch["server_id"]
        position = index.get(server_id)
        if position is None:
            orphans.append(server_id)
            continue
        merged_servers[position] = {
            **merged_servers[position],
            **{key: value for key, value in patch.items() if key != "server_id"},
        }
    merged = {key: value for key, value in base.items() if key != "servers"}
    merged["servers"] = merged_servers
    return merged, tuple(orphans)


@dataclass(frozen=True)
class OperatorLayers:
    """运维的两层配置（仓库默认层 + 部署覆盖层）读出来的原始内容（P7-S6 拆出）。

    相对 ``env_file`` 已按各自所在目录改写成绝对路径；还没做模型校验，也没查文件系统。
    ``base`` 为 ``None`` 表示默认层文件不存在；``overlay`` 为 ``None`` 表示没设覆盖层。
    """

    base: Any
    base_path: Path
    overlay: dict[str, Any] | None = None
    overlay_path: Path | None = None

    @property
    def configured(self) -> bool:
        return self.base is not None or self.overlay is not None

    @property
    def source(self) -> Path:
        """报错时指向的文件：有覆盖层指覆盖层，否则指默认层。"""
        return self.overlay_path if self.overlay_path is not None else self.base_path

    def payload(self) -> Any:
        """两层合并后的原始 dict（不校验）；两层都没有时为 ``None``。"""
        if self.overlay is None:
            return self.base
        try:
            return merge_overlay(self.base if self.base is not None else {}, self.overlay)
        except (TypeError, ValueError) as exc:
            raise MCPError(
                MCP_CONFIG_INVALID, f"覆盖层 {self.overlay_path} 合并后校验失败：{exc}"
            ) from exc

    def validate(self) -> MCPGatewayConfig | None:
        """模型层校验（不查文件系统）；两层都没有时 ``None``（功能关闭）。报错口径与 P7-S3 相同。"""
        if not self.configured:
            return None
        if self.overlay is None:
            try:
                return MCPGatewayConfig.model_validate(self.base)
            except ValueError as exc:
                raise MCPError(MCP_CONFIG_INVALID, f"{self.base_path} 校验失败：{exc}") from exc
        payload = self.payload()
        try:
            return MCPGatewayConfig.model_validate(payload)
        except ValueError as exc:
            raise MCPError(
                MCP_CONFIG_INVALID, f"覆盖层 {self.overlay_path} 合并后校验失败：{exc}"
            ) from exc


def read_operator_layers() -> OperatorLayers:
    """读仓库默认层与部署覆盖层（不校验）。读不了、不是合法 JSON、覆盖层给了却不存在 →
    ``MCP_CONFIG_INVALID``。"""
    base_path = default_config_path()
    overlay_raw = os.environ.get(ENV_MCP_OVERLAY, "").strip()
    if not overlay_raw:
        if not base_path.exists():
            return OperatorLayers(None, base_path)
        try:
            base_text = base_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MCPError(MCP_CONFIG_INVALID, f"无法读取 {base_path}：{exc}") from exc
        base = _read_json_object(base_path, base_text)
        return OperatorLayers(_resolve_env_files(base, base_path.parent), base_path)

    overlay_path = Path(overlay_raw).expanduser()
    if not overlay_path.is_file():
        raise MCPError(MCP_CONFIG_INVALID, f"{ENV_MCP_OVERLAY} 指向的覆盖层文件不存在：{overlay_path}")
    try:
        overlay_text = overlay_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MCPError(MCP_CONFIG_INVALID, f"无法读取覆盖层 {overlay_path}：{exc}") from exc
    overlay = _read_json_object(overlay_path, overlay_text)
    if not isinstance(overlay, dict):
        raise MCPError(MCP_CONFIG_INVALID, f"覆盖层 {overlay_path} 顶层必须是对象")

    if base_path.exists():
        try:
            base_text = base_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MCPError(MCP_CONFIG_INVALID, f"无法读取 {base_path}：{exc}") from exc
        base = _read_json_object(base_path, base_text)
        if not isinstance(base, dict):
            raise MCPError(MCP_CONFIG_INVALID, f"{base_path} 顶层必须是对象")
        base = _resolve_env_files(base, base_path.parent)
    else:
        base = {}
    return OperatorLayers(
        base, base_path, _resolve_env_files(overlay, overlay_path.parent), overlay_path
    )


def web_layer_path() -> Path | None:
    """``WATER_AGENT_MCP_WEB_LAYER`` 指向的文件；未设 → ``None``（没有这一层）。"""
    raw = os.environ.get(ENV_MCP_WEB_LAYER, "").strip()
    return Path(raw).expanduser() if raw else None


def load_layered_gateway_config() -> MCPGatewayConfig | None:
    """仓库默认层 + 部署覆盖层（P7-S3）+ Web 管理层（P7-S6）。

    | 默认层 | 覆盖层 | 结果 |
    |---|---|---|
    | 有 | 未设 | 只用默认层（与 :func:`load_gateway_config` 相同） |
    | 缺失 | 有 | 默认层按空骨架处理 |
    | 缺失 | 未设 | ``None``（功能关闭） |

    ``WATER_AGENT_MCP_OVERLAY`` 设了但文件不存在、覆盖层不是合法 JSON、合并结果校验不过，
    一律 ``MCP_CONFIG_INVALID``。两层里的相对 ``env_file`` 各按自己所在目录解析。

    设了 ``WATER_AGENT_MCP_WEB_LAYER`` 时再叠一层 Web 管理层（``mcp/web_layer.py``）：只改
    ``disabled`` / ``allow``、不能新增 server（孤立条目忽略）、文件损坏即 ``MCP_CONFIG_INVALID``。
    下面两层都没有时 Web 管理层照样校验，结果仍是 ``None``。路径存在性在**最终**配置上检查：
    面板停用一台 ``cwd`` 已不存在的 server，其余 server 就能照常加载。
    """
    layers = read_operator_layers()
    web_path = web_layer_path()
    if web_path is None:
        config = layers.validate()
        if config is not None:
            _check_paths_exist(config, layers.source)
        return config
    from water_agent.mcp.web_layer import effective_config, load_web_layer

    return effective_config(layers, load_web_layer(web_path), web_path=web_path)


def _read_json_object(path: Path, raw_text: str) -> Any:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MCPError(
            MCP_CONFIG_INVALID,
            f"{path} 不是合法 JSON（行 {exc.lineno} 列 {exc.colno}）：{exc.msg}",
        ) from exc


def _resolve_env_files(payload: Any, base_dir: Path) -> Any:
    """把各 server 的相对 ``env_file`` 改写成按 ``base_dir`` 解析的绝对路径（F9）。

    相对路径的基准是**定义它的那份配置文件所在目录**；改写成绝对路径后写进模型，之后在哪个
    工作目录连接都读同一个文件。形状不对的输入原样返回，交给模型校验报错。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("servers"), list):
        return payload
    servers = []
    for server in payload["servers"]:
        env_file = server.get("env_file") if isinstance(server, dict) else None
        if isinstance(env_file, str) and env_file:
            path = Path(env_file).expanduser()
            if not path.is_absolute():
                server = {**server, "env_file": str((base_dir / path).resolve())}
        servers.append(server)
    return {**payload, "servers": servers}


def missing_paths(config: MCPGatewayConfig) -> list[tuple[str, str]]:
    """启用的 server 里 ``env_file`` / ``cwd`` 不存在的 ``(server_id, 字段名)``，按配置顺序。

    停用的 server 不查——停用就是为了让一台暂时不可用的 server 不影响其余。
    """
    missing: list[tuple[str, str]] = []
    for server in config.servers:
        if server.disabled:
            continue
        if server.env_file is not None and not Path(server.env_file).expanduser().is_file():
            missing.append((server.server_id, "env_file"))
        if server.cwd is not None and not Path(server.cwd).is_dir():
            missing.append((server.server_id, "cwd"))
    return missing


def _check_paths_exist(config: MCPGatewayConfig, source: Path | str) -> None:
    """加载期就确认 ``env_file`` 与 ``cwd`` 存在（F9）：缺失是配置错误，不能拖到连接时被当成
    "这台 server 跳过"。停用的 server 不查（见 :func:`missing_paths`）。"""
    servers = {server.server_id: server for server in config.servers}
    for server_id, field in missing_paths(config)[:1]:
        server = servers[server_id]
        if field == "env_file":
            raise MCPError(
                MCP_CONFIG_INVALID,
                f"{source}：server {server_id!r} 的 env_file 不存在：{server.env_file}",
            )
        raise MCPError(
            MCP_CONFIG_INVALID,
            f"{source}：server {server_id!r} 的 cwd 不是存在的目录：{server.cwd}",
        )


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
    "ENV_MCP_OVERLAY",
    "ENV_MCP_WEB_LAYER",
    "MCP_CONFIG_VERSION",
    "WEB_LAYER_SERVER_KEYS",
    "MCPGatewayConfig",
    "MCPServerConfig",
    "OperatorLayers",
    "ServerTransport",
    "child_env",
    "default_config_path",
    "load_gateway_config",
    "load_layered_gateway_config",
    "merge_overlay",
    "merge_web_layer",
    "missing_paths",
    "read_operator_layers",
    "web_layer_path",
]

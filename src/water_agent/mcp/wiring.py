"""MCP 网关装配接线（P4 Step 25）：配置 → 连接 → 桥接 → 注册表。

把 Step 23 的地基（``open_bridge`` / ``register_mcp_tool_binding`` /
``ToolRegistry``）接成**装配期单入口**：CLI（``AgenticSession``）与 harness
（``--variant real`` agentic 路径）都从这里拿 MCP 工具，不存在第二条装配路径。

治理语义（规格 §3/§4，`p4-step-25-decision.md`）：

1. **文件不存在 = 功能关闭**（返回空装配，不报错）；**文件坏 =
   ``MCP_CONFIG_INVALID`` 上抛**（响亮失败 —— 静默跳过全部 server 是最危险的降级）。
2. ``human_confirmation=true`` 且确认门未接线（``confirmation_handler_ready=false``）
   的 server **整台不暴露**（`MCP_CONFIRMATION_UNAVAILABLE`，只记 skip，不 spawn
   进程）—— 宁可不暴露，也不给"先问一句再执行"的假象（interrupt/HITL 是后续 Step）。
3. 工具暴露需要**两把钥匙**：``allow`` 白名单（装配期，``None`` = 全拒）+
   ``Principal`` 显式授予 ``Capability.MCP_INVOKE``（本模块构造主体时叠加授予，
   基础能力集不变）。缺任何一把，``ToolRegistry.invoke`` 的护栏都拒绝。
4. 子进程 env 只透传 ``PATH``/``HOME`` + 配置显式给值（``env``/``env_file``），
   本机其余变量（含 API Key）一律不进第三方进程（``config.child_env`` 承担）。
5. 远端返回内容统一是 ``SourceTag.TOOL_OUTPUT`` 数据（桥接层 description 明示），
   数值不进公式口径、条文不得重标 ``SYSTEM_POLICY``（铁律①②）。

**诚实边界**：``autocad_mcp``/``revit_python`` 的配置模板指向本机真实仓库，
但 revit server 需 Windows+Revit+pyRevit（darwin 上连不上 → ``CONNECT_FAILED``
skip 或确认门拒绝），真实 CAD/BIM 端到端验证仍为 Step 26 blocked —— 本模块
不声称"已接通任何实机"。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from water_agent.mcp.bridge import MCPServerSession, open_bridge
from water_agent.mcp.config import MCPGatewayConfig, load_gateway_config
from water_agent.mcp.errors import (
    MCP_CONFIRMATION_UNAVAILABLE,
)
from water_agent.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class SkippedServer:
    """一台 server 未暴露任何工具的结构化原因。"""

    server_id: str
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"server_id": self.server_id, "code": self.code, "detail": self.detail}


@dataclass(slots=True)
class MCPGatewayAssembly:
    """一次装配的产物：存活的 session、注册名、skip 记录与 manifest 摘要。

    ``manifest_hash`` 是**全部已校验 server 配置**（含被确认门跳过的）的稳定
    摘要 —— 它进缓存键的 ``mcp_server_manifest`` 因子：server 清单/白名单/
    超时/DWG 版本任一变化，旧回答缓存自然失效。配置文件不存在（功能关闭）时
    为空串，与"从未启用 MCP"的旧键同键（不无谓清空既有缓存）。
    """

    sessions: tuple[MCPServerSession, ...]
    skipped_servers: tuple[SkippedServer, ...]
    manifest_hash: str = ""
    raw_config: MCPGatewayConfig | None = None

    @property
    def exposed_tool_names(self) -> tuple[str, ...]:
        return tuple(
            name for session in self.sessions for name in session.result.exposed_names
        )

    def summary(self) -> dict[str, object]:
        """脱敏装配摘要（进 trace / CLI --json）。"""
        return {
            "mcp_enabled": bool(self.sessions or self.skipped_servers),
            "mcp_servers": [
                {
                    "server_id": s.config.server_id,
                    "exposed": list(s.result.exposed_names),
                    "skipped": [skip.to_dict() for skip in s.result.skipped],
                }
                for s in self.sessions
            ],
            "mcp_skipped_servers": [s.to_dict() for s in self.skipped_servers],
            "mcp_manifest_hash": self.manifest_hash,
        }

    def close(self) -> None:
        for session in self.sessions:
            session.close()


def _config_digest(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_manifest_hash(config: MCPGatewayConfig) -> str:
    """server manifest 摘要（缓存失效因子 ``mcp_server_manifest`` 的值）。

    覆盖全部**已校验** server（含确认门跳过的）：清单、命令、白名单、超时、
    env 显式值与确认位任一变化 → 键变化。空 servers 返回空串（功能关闭态
    与"从未启用"同键，不无谓失效）。
    """
    if not config.servers:
        return ""
    return _config_digest(config.model_dump(mode="json"))


def load_mcp_gateway(path: str | Path | None = None) -> MCPGatewayConfig | None:
    """加载网关配置（``load_gateway_config`` 的直通别名，语义见 config 模块）。

    - 文件缺失（且未显式给路径）→ ``None``：功能关闭，不是错误；
    - 文件存在但坏/显式路径缺失 → ``MCP_CONFIG_INVALID`` 上抛（响亮失败）。
    """
    return load_gateway_config(path)


def _confirmation_skips(config: MCPGatewayConfig) -> list[SkippedServer]:
    """确认门未接线时，``human_confirmation=true`` 的 server 整台拒绝（不 spawn）。"""
    if config.confirmation_handler_ready:
        return []
    return [
        SkippedServer(
            server.server_id,
            MCP_CONFIRMATION_UNAVAILABLE,
            "server 配置 human_confirmation=true，但 confirmation_handler_ready=false："
            "危险操作人工确认门未接线，整台 server 的工具不暴露（interrupt/HITL 为后续 Step）",
        )
        for server in config.servers
        if server.human_confirmation
    ]


def open_gateway_sessions(
    config: MCPGatewayConfig | None,
    *,
    config_path: str | Path | None = None,
) -> MCPGatewayAssembly:
    """连接全部可用 server 并桥接工具（装配期入口，单台故障不拖垮其余）。

    ``config=None``（功能关闭）→ 空装配。``MCP_CONFIG_INVALID`` 由
    ``load_gateway_config`` 在**调用方加载阶段**上抛 —— 装配函数只接受已校验
    配置，保证"坏文件响亮失败"发生在最外层（CLI main / runner main）。

    Args:
        config: 已加载的网关配置；``None`` 表示功能关闭。
        config_path: 配置路径（仅用于 env_file 相对路径解析的目录推导，
            透传给子进程 env 构造的调用方；本函数不重读文件）。
    """
    if config is None:
        return MCPGatewayAssembly(sessions=(), skipped_servers=(), manifest_hash="")

    blocked = {s.server_id for s in _confirmation_skips(config)}
    sessions: list[MCPServerSession] = []
    skipped: list[SkippedServer] = list(_confirmation_skips(config))
    for server in config.servers:
        if server.server_id in blocked:
            continue
        session = open_bridge(server, config)
        if session.client is None and not session.result.tools:
            # 连接失败 / 全部工具被拒：记录原因后与"没有这台 server"同态。
            # 连接已由 open_bridge 收掉（不留活进程）。
            skipped.extend(
                SkippedServer(
                    session.result.server_id,
                    skip.code,
                    skip.detail,
                )
                for skip in session.result.skipped
            )
            continue
        sessions.append(session)
    return MCPGatewayAssembly(
        sessions=tuple(sessions),
        skipped_servers=tuple(skipped),
        manifest_hash=compute_manifest_hash(config),
        raw_config=config,
    )


def register_mcp_tools(
    assembly: MCPGatewayAssembly,
    registry: ToolRegistry,
) -> list[str]:
    """把装配产物里的桥接工具注册进 ``ToolRegistry``。

    注册顺序按装配顺序；重名抛 ``ValueError``（``MCP_NAME_COLLISION`` 在
    ``register_mcp_tool_binding`` 已挡，这里是对 ToolRegistry 的同义保护）。
    返回实际注册的本地工具名。

    **主体能力不在这里授予** —— ``Capability.MCP_INVOKE`` 必须在构造
    ``Principal`` 时显式给（见 :func:`principal_with_mcp`），注册表持有的主体
    缺这把钥匙时护栏照常拒绝（两把钥匙各自独立生效）。
    """
    registered: list[str] = []
    for session in assembly.sessions:
        for tool in session.result.tools:
            registry.register(tool)
            registered.append(tool.name)
    return registered


def principal_with_mcp(
    base_capabilities: frozenset,  # type: ignore[type-arg]
) -> frozenset:
    """基础能力集 + ``MCP_INVOKE``（装配期显式授予，规格 §3 权限模型）。

    MCP 网关启用时，本地主体才会被授予 ``MCP_INVOKE``；调用方只在**配置非空
    且有工具暴露**时叠加，保证"没配 MCP 的主体不白拿远程调用能力"。
    """
    from water_agent.runtime.governance.contracts import Capability

    return frozenset(base_capabilities) | {Capability.MCP_INVOKE}


__all__ = [
    "MCPGatewayAssembly",
    "SkippedServer",
    "compute_manifest_hash",
    "load_mcp_gateway",
    "open_gateway_sessions",
    "principal_with_mcp",
    "register_mcp_tools",
]

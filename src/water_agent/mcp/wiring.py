"""MCP 网关装配接线（P4 Step 25）：配置 → 连接 → 桥接 → 注册表。

把 Step 23 的地基（``open_bridge`` / ``register_mcp_tool_binding`` /
``ToolRegistry``）接成**装配期单入口**：CLI（``AgenticSession``）与 harness
（``--variant real`` agentic 路径）都从这里拿 MCP 工具，不存在第二条装配路径。

治理语义（规格 §3/§4，`p4-step-25-decision.md`）：

1. **文件不存在 = 功能关闭**（返回空装配，不报错）；**文件坏 =
   ``MCP_CONFIG_INVALID`` 上抛**（响亮失败 —— 静默跳过全部 server 是最危险的降级）。
2. ``human_confirmation=true`` 的 server 只在**接了确认门的入口**暴露（``confirmation_ready=True``，
   P7-S7：只有 Web 会话工厂）；其余入口（CLI、评测 runner）**整台不暴露**
   （`MCP_CONFIRMATION_UNAVAILABLE`，只记 skip，不 spawn 进程）—— 宁可不暴露，也不给"先问一句
   再执行"的假象。确认门是否可用由入口决定，不由配置声明（``confirmation_handler_ready`` 在加载期
   即被拒绝）。
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
from water_agent.mcp.config import (
    MCPGatewayConfig,
    load_gateway_config,
    load_layered_gateway_config,
)
from water_agent.mcp.errors import (
    MCP_CONFIRMATION_UNAVAILABLE,
    MCP_SERVER_DISABLED,
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
    #: 每台已连接 server 的连接方式（P7-S5）：``per_run``（本次现开）、``shared_new``（连接池新开）、
    #: ``shared_reused``（连接池复用）。没有记录的按 ``per_run``。只进 trace 摘要，不进 manifest。
    connections: dict[str, str] | None = None

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

    @property
    def has_activity(self) -> bool:
        """有暴露的工具或跳过 / 停用记录。没有任何 server 配置时为 False。"""
        return bool(self.sessions or self.skipped_servers)

    def trace_summary(self) -> dict[str, object]:
        """写进 trace 的装配摘要（P7-S3）：只有 server id、暴露的工具名与跳过的错误码。

        **不含 detail 文本**：跳过原因里可能有第三方 server 的 stderr 尾部与本机路径，trace 是
        设计为可落盘、可复制的诊断材料。manifest 也只记是否非空（摘要值含解释器路径）。
        """
        return {
            "mcp_servers": [
                {
                    "server_id": session.config.server_id,
                    "exposed": list(session.result.exposed_names),
                    "skipped": [[skip.remote_tool, skip.code] for skip in session.result.skipped],
                    "connection": (self.connections or {}).get(
                        session.config.server_id, "per_run"
                    ),
                }
                for session in self.sessions
            ],
            "mcp_skipped_servers": [
                [skip.server_id, skip.code] for skip in self.skipped_servers
            ],
            "mcp_manifest_nonempty": bool(self.manifest_hash),
        }

    def close(self) -> None:
        for session in self.sessions:
            session.close()


def _config_digest(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_manifest_hash(config: MCPGatewayConfig) -> str:
    """server manifest 摘要（缓存失效因子 ``mcp_server_manifest`` 的值）。

    覆盖全部**未停用的**已校验 server（含确认门跳过的）：清单、命令、工作目录、白名单、
    超时、env 显式值与确认位任一变化 → 键变化。停用的 server 不计入（P7-S3）：停用一台
    server 让缓存键回到没有它的状态。没有任何启用的 server 时返回空串（功能关闭态与
    "从未启用"同键，不无谓失效）。
    """
    enabled = [server for server in config.servers if not server.disabled]
    if not enabled:
        return ""
    payload = config.model_dump(mode="json")
    payload["servers"] = [server.model_dump(mode="json") for server in enabled]
    return _config_digest(payload)


def load_mcp_gateway(path: str | Path | None = None) -> MCPGatewayConfig | None:
    """加载网关配置。会话、runner 与 Web 能力端点共用这一个入口。

    - 不给 ``path``：仓库默认层 + 部署覆盖层（``WATER_AGENT_MCP_OVERLAY``），见
      ``config.load_layered_gateway_config``（P7-S3）；
    - 给了 ``path``：只读这一份文件（单层，测试与示例校验用）；
    - 文件缺失（且未显式给路径、也没有覆盖层）→ ``None``：功能关闭，不是错误；
    - 文件存在但坏 / 显式路径缺失 / 覆盖层坏 → ``MCP_CONFIG_INVALID`` 上抛（响亮失败）。
    """
    if path is None:
        return load_layered_gateway_config()
    return load_gateway_config(path)


def _disabled_skips(config: MCPGatewayConfig) -> list[SkippedServer]:
    """``disabled=true`` 的 server 不 spawn、不暴露（P7-S3）。先于确认门判定。"""
    return [
        SkippedServer(server.server_id, MCP_SERVER_DISABLED, "server 配置 disabled=true")
        for server in config.servers
        if server.disabled
    ]


def _confirmation_skips(
    config: MCPGatewayConfig, *, confirmation_ready: bool
) -> list[SkippedServer]:
    """本入口没有确认门时，``human_confirmation=true`` 的 server 整台拒绝（不 spawn）。"""
    if confirmation_ready:
        return []
    return [
        SkippedServer(
            server.server_id,
            MCP_CONFIRMATION_UNAVAILABLE,
            "server 配置 human_confirmation=true，但本入口没有接人工确认门（只有 Web 会话接线）："
            "整台 server 的工具不暴露",
        )
        for server in config.servers
        if server.human_confirmation and not server.disabled
    ]


def pre_connection_skips(
    config: MCPGatewayConfig, *, confirmation_ready: bool = False
) -> list[SkippedServer]:
    """不连接就能判定的跳过：停用，以及本入口没有确认门时要求人工确认的 server（停用先于确认门）。"""
    return [*_disabled_skips(config), *_confirmation_skips(config, confirmation_ready=confirmation_ready)]


def bridge_gateway_view(
    config: MCPGatewayConfig, *, confirmation_ready: bool
) -> MCPGatewayConfig:
    """交给 ``open_bridge`` 的网关配置视图：入口接了确认门时，确认位取入口的决定（P7-S7）。

    ``confirmation_handler_ready`` 在配置文件里写 ``true`` 仍在加载期被拒绝——配置不能凭空声明
    "可确认"。这里用 ``model_copy`` 生成只在本次连接里使用的视图，不经过那条校验；视图不进
    manifest、不进 ``raw_config``，也不回写任何配置。
    """
    if not confirmation_ready:
        return config
    return config.model_copy(update={"confirmation_handler_ready": True})


def session_failed(session: MCPServerSession) -> bool:
    """连接失败或没有任何工具被暴露（这时 ``open_bridge`` 已收掉进程）。"""
    return session.client is None and not session.result.tools


def session_skips(session: MCPServerSession) -> list[SkippedServer]:
    return [
        SkippedServer(session.result.server_id, skip.code, skip.detail)
        for skip in session.result.skipped
    ]


def open_gateway_sessions(
    config: MCPGatewayConfig | None,
    *,
    config_path: str | Path | None = None,
    confirmation_ready: bool = False,
) -> MCPGatewayAssembly:
    """连接全部可用 server 并桥接工具（装配期入口，单台故障不拖垮其余）。

    ``config=None``（功能关闭）→ 空装配。``MCP_CONFIG_INVALID`` 由
    ``load_gateway_config`` 在**调用方加载阶段**上抛 —— 装配函数只接受已校验
    配置，保证"坏文件响亮失败"发生在最外层（CLI main / runner main）。

    Args:
        config: 已加载的网关配置；``None`` 表示功能关闭。
        config_path: 配置路径（仅用于 env_file 相对路径解析的目录推导，
            透传给子进程 env 构造的调用方；本函数不重读文件）。
        confirmation_ready: 调用方入口是否接了人工确认门（P7-S7，只有 Web 会话工厂传 ``True``）。
            为 ``True`` 时 ``human_confirmation=true`` 的 server 照常连接与桥接，其工具由装配换成
            延迟工具、执行须经确认门签发的凭证。
    """
    if config is None:
        return MCPGatewayAssembly(sessions=(), skipped_servers=(), manifest_hash="")

    pre_skips = pre_connection_skips(config, confirmation_ready=confirmation_ready)
    blocked = {s.server_id for s in pre_skips}
    bridge_view = bridge_gateway_view(config, confirmation_ready=confirmation_ready)
    sessions: list[MCPServerSession] = []
    skipped: list[SkippedServer] = list(pre_skips)
    for server in config.servers:
        if server.server_id in blocked:
            continue
        session = open_bridge(server, bridge_view)
        if session_failed(session):
            # 连接失败 / 全部工具被拒：记录原因后与"没有这台 server"同态。
            # 连接已由 open_bridge 收掉（不留活进程）。
            skipped.extend(session_skips(session))
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
    "bridge_gateway_view",
    "compute_manifest_hash",
    "load_mcp_gateway",
    "open_gateway_sessions",
    "pre_connection_skips",
    "principal_with_mcp",
    "register_mcp_tools",
    "session_failed",
    "session_skips",
]

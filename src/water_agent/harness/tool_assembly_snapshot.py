"""工具装配快照（P7-S0 H1）：把"模型看到的工具面"与"装配生命周期"钉成可比对的基线。

为什么需要它
------------
P7 要把工具装配收编成插件 + 单入口（``docs/design/16-p7-plugin-mcp.md``）。重构有没有
改变行为，读代码判断不了：

- 注册顺序决定提示词里工具指引的顺序与截断（800 字预算，5 个工具时已截掉 1 行），
  发给模型的工具却按 ``list_all_names()`` 的字母序；
- runner 的 real-agentic 路径另有一套手工装配，与会话路径有多处差异。

本模块在**生产代码自己的接缝**上采集装配结果，离线、确定、不调用模型：

- 捕获点是 ``build_agentic_app``：会话路径与 runner 路径都只在这里把工具交给图。
  装配没抛异常时，每个 profile 必须**恰好捕获一次**，否则说明捕获点移位，直接报错，
  而不是采到一份空快照、当成"没有变化"。
- 每个 profile 分两层记录，两层的变更规则不同（见 :func:`write_guard_violations`）：
  ``surface`` 是模型可见面（工具名、两种顺序、schema 摘要、截断后的工具指引、系统提示
  摘要、主体能力）；``lifecycle`` 是装配生命周期（MCP 暴露/跳过、子进程在作用域结束后
  是否存活、ExitStack 回调、trace 事件是否真正落盘、异常、行为探针）。

隔离
----
装配结果受环境变量影响（``DRAWING_ROOT``、``WATER_AGENT_MCP_CONFIG``、本地模型开关……），
这里用保存/恢复式上下文管理器钉住它们（``--write`` 在 pytest 之外运行，不能靠
monkeypatch）。MCP 用 ``tests/fixtures/fake_mcp_server.py``，每个 profile 一个独立的
server id——运行时绑定登记只增不删，同名不同配置会冲突。随机或机器相关的值（subject、
thread id、manifest hash 的值、tmp 路径、跳过原因的 detail 文本）不进比较。

用法
----
    python -m water_agent.harness.tool_assembly_snapshot --check     # 与入仓基线比对
    python -m water_agent.harness.tool_assembly_snapshot --diff      # 打印差异
    python -m water_agent.harness.tool_assembly_snapshot --write --reason "<为什么变>"

退出码：0 一致 / 已写入；1 有差异 / 写入守卫拒绝；2 基线缺失或 harness 自身出错。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASELINE = REPO_ROOT / "data" / "eval" / "tool_assembly_main.json"
FAKE_MCP_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_mcp_server.py"
FAKE_MCP_HTTP_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_mcp_http_server.py"
FIXTURE_CLAUSES = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"

SNAPSHOT_FORMAT_VERSION = 1
GENERATED_BY = "water_agent.harness.tool_assembly_snapshot"

#: 探针用的检索词与一个必然不在检索结果里的条款号（形状合法，cite 的格式校验能过）。
PROBE_QUERY = "给水管道设计秒流量"
BOGUS_ARTICLE = "GB00000-0000 9.9.9"
#: fake server 的 tools/list 有 5 个工具：前两个应暴露，nested_schema_tool 应被 schema
#: 子集拒绝；dangerous_write / unlisted_tool 不在白名单里。
FAKE_MCP_ALLOW = ("echo_query", "lookup_project", "nested_schema_tool")

#: 装配会读到的环境变量。快照期间一律先清掉，再按 profile 显式设置。
PINNED_ENV_KEYS = (
    "DRAWING_ROOT",
    "WATER_AGENT_MCP_CONFIG",
    "WATER_AGENT_MCP_OVERLAY",
    "WATER_AGENT_MCP_WEB_LAYER",
    "WATER_AGENT_SKILLS",
    "WATER_AGENT_SKILLS_DIR",
    "WATER_AGENT_TOOL_EXPOSURE",
    "WEB_PUBLIC_BASE_URL",
    "WEB_MOCK_GENERATOR",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY_FILE",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_API_KEY_FILE",
    "WATER_AGENT_INDEX_PATH",
    "WATER_AGENT_TRACE_DIR",
    "WATER_AGENT_CREDENTIALS_DIR",
    "LOCAL_MODEL_ENABLED",
    "LOCAL_MODEL_BASE_URL",
    "LOCAL_MODEL_NAME",
    "LOCAL_MODEL_COMPATIBILITY_REPORT",
    # P7-S10：对话 / embedding 端点、兼容性报告、超时 / 价格覆盖与检索模式
    "WATER_AGENT_LLM_BASE_URL",
    "WATER_AGENT_LLM_MODEL",
    "WATER_AGENT_LLM_API_KEY",
    "WATER_AGENT_LLM_API_KEY_FILE",
    "WATER_AGENT_LLM_COMPATIBILITY_REPORT",
    "WATER_AGENT_LLM_TIMEOUT_SECONDS",
    "WATER_AGENT_LLM_PRICE",
    "WATER_AGENT_EMBEDDING_BASE_URL",
    "WATER_AGENT_EMBEDDING_MODEL",
    "WATER_AGENT_EMBEDDING_API_KEY",
    "WATER_AGENT_EMBEDDING_API_KEY_FILE",
    "WATER_AGENT_RETRIEVAL_MODE",
    # P7-S12：联网搜索的部署开关 / provider / 基址 / 私网放行与密钥槽位
    "WATER_AGENT_WEB_SEARCH",
    "WATER_AGENT_WEB_SEARCH_PROVIDER",
    "WATER_AGENT_WEB_SEARCH_BASE_URL",
    "WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE",
    "WATER_AGENT_WEB_SEARCH_API_KEY",
    "WATER_AGENT_WEB_SEARCH_API_KEY_FILE",
)

#: Web profile 用的占位 Key：只用来让 ``build_chat_model`` 能构造对象，永远不会被发出去
#: （捕获点之后不调用模型）。
_PLACEHOLDER_API_KEY = "sk-" + "tool-assembly-snapshot-placeholder"
#: P7-S10 ``session_endpoint_custom`` 的非缺省对话端点（``.invalid`` 顶级域，永远解析不到；捕获点之后不调用模型）。
CUSTOM_ENDPOINT_BASE_URL = "https://llm.snapshot.invalid/v1"
CUSTOM_ENDPOINT_MODEL = "snapshot-compatible-model"
#: P7-S15 ``session_pick_model_off`` 在目录里选中的模型（缺省 DeepSeek 端点上的目录项，思考 ``off``）。
PICKED_MODEL = "snapshot-picked-model"
#: P7-S12 ``session_web_search_fake`` 的搜索服务基址（``.invalid`` 顶级域，永远解析不到；快照不调用工具）。
WEB_SEARCH_BASE_URL = "https://search.snapshot.invalid/"

DrawingMode = Literal["off", "arg", "env", "none"]
McpMode = Literal["disabled", "empty", "fake", "http_fake", "confirm_skip", "ready_flag", "invalid"]


class SnapshotHarnessError(RuntimeError):
    """harness 自身无法给出可信快照（捕获点移位、fixture 缺失……）。不是"有差异"。"""


@dataclass(frozen=True, slots=True)
class Profile:
    """一种装配场景。``name`` 是快照里的键，只用小写字母、数字和下划线（JSON pointer 友好）。"""

    name: str
    side: Literal["session", "runner"]
    description: str
    drawing: DrawingMode = "off"
    mcp: McpMode = "disabled"
    mcp_server_id: str = ""
    fault: Literal["mcp_register"] | None = None
    web: bool = False
    cli_checkpointer: bool = False
    #: P7-S9：打开 Skill 库（``WATER_AGENT_SKILLS=on``，只用随包分发的内置 skill）。
    skills: bool = False
    #: P7-S10：非缺省 OpenAI 兼容对话端点 + 一份精确匹配的测试兼容报告（真生成器装配，不调用模型）。
    endpoint: bool = False
    #: P7-S15：缺省端点上从模型目录里选一个思考 ``off`` 的模型（目录 + 逐对兼容性报告写进凭据卷，真生成器装配）。
    pick_model: bool = False
    #: P7-S12：联网搜索部署开关 on + 假 provider（``.invalid`` 基址，永远解析不到；捕获点之后不调用工具）+ 本条消息勾选联网。
    web_search: bool = False


#: 会话侧 ``drawing``：off = ``drawing_root=""``（不注册图纸工具）；arg = 显式目录；
#: env = ``drawing_root=None`` + ``DRAWING_ROOT``；none = ``drawing_root=None`` 且不设环境变量。
#: runner 侧没有参数入口，只有 env / none。
PROFILES: tuple[Profile, ...] = (
    Profile("session_mock_default", "session", "mock-agentic 同款：drawing_root=''、MCP 关闭"),
    Profile(
        "session_cli_default",
        "session",
        "CLI 默认：MCP 打开但没有 server、图纸根未配置、真 SqliteSaver",
        drawing="none",
        mcp="empty",
        cli_checkpointer=True,
    ),
    Profile("session_drawing_arg", "session", "图纸根经参数给出（5 个本地工具）", drawing="arg"),
    Profile("session_drawing_env", "session", "图纸根经 DRAWING_ROOT 给出", drawing="env"),
    Profile(
        "session_mcp_fake",
        "session",
        "fake MCP server：两个工具暴露、一个被 schema 子集拒绝",
        mcp="fake",
        mcp_server_id="snapfake",
    ),
    Profile(
        "session_mcp_confirm_skip",
        "session",
        "human_confirmation=true 且确认门未接线：整台 server 不 spawn、不授权",
        mcp="confirm_skip",
        mcp_server_id="snapconfirm",
    ),
    Profile(
        "session_mcp_ready_flag",
        "session",
        "confirmation_handler_ready=true：现状是直接暴露并放行（F7，S1 修复）",
        mcp="ready_flag",
        mcp_server_id="snapready",
    ),
    Profile(
        "session_mcp_config_invalid",
        "session",
        "MCP 配置文件损坏：必须响亮失败",
        mcp="invalid",
    ),
    Profile(
        "session_web_member",
        "session",
        "Web 工厂参数（真生成器、本地 rerank）+ 已认证 member 主体 + fake MCP",
        drawing="none",
        mcp="fake",
        mcp_server_id="snapweb",
        web=True,
    ),
    Profile(
        "session_skills_builtin",
        "session",
        "Skill 库打开（只有内置 skill）：多出 load_skill（intent，目录在工具描述里）",
        skills=True,
    ),
    Profile(
        "session_http_mcp_fake",
        "session",
        "fake Streamable HTTP MCP server（明文回环 + allow_private_network）：两个工具暴露、一个被 schema 子集拒绝",
        mcp="http_fake",
        mcp_server_id="snaphttp",
    ),
    Profile(
        "session_endpoint_custom",
        "session",
        "非缺省 OpenAI 兼容对话端点 + 精确匹配的测试兼容报告：chat model 指向配置的端点与模型",
        endpoint=True,
    ),
    Profile(
        "session_pick_model_off",
        "session",
        "缺省端点上从目录选 thinking=off 的模型 + 逐对兼容性报告：chat model 指向所选模型并附带关闭思考的 extra_body",
        pick_model=True,
    ),
    Profile(
        "session_web_search_fake",
        "session",
        "联网搜索部署开关 on + 假 searxng（.invalid 基址）+ 本条消息勾选联网：多出 web_search（request 组）、主体多 web_search 能力",
        web_search=True,
    ),
    Profile(
        "session_mcp_register_fault",
        "session",
        "MCP 工具注册抛错：会话路径不降级、直接抛出",
        mcp="fake",
        mcp_server_id="snapfault",
        fault="mcp_register",
    ),
    Profile("runner_no_mcp", "runner", "real-agentic：MCP 配置为空、图纸根未配置", drawing="none",
            mcp="empty"),
    Profile("runner_drawing_env", "runner", "real-agentic：DRAWING_ROOT 指向合成图纸目录",
            drawing="env", mcp="empty"),
    Profile(
        "runner_mcp_fake",
        "runner",
        "real-agentic：fake MCP（现状：模块级缓存，用例结束后子进程仍存活）",
        drawing="none",
        mcp="fake",
        mcp_server_id="snaprfake",
    ),
    Profile(
        "runner_mcp_config_invalid",
        "runner",
        "real-agentic：MCP 配置文件损坏",
        drawing="none",
        mcp="invalid",
    ),
    Profile(
        "runner_mcp_register_fault",
        "runner",
        "real-agentic：MCP 工具注册抛错（现状：降级为纯本地工具、不授权）",
        drawing="none",
        mcp="fake",
        mcp_server_id="snaprfault",
        fault="mcp_register",
    ),
)


# ---------------------------------------------------------------------------
# 隔离与打桩
# ---------------------------------------------------------------------------


@contextmanager
def hermetic_environment(workdir: Path) -> Iterator[None]:
    """清掉装配会读的环境变量，结束时**整体**恢复 ``os.environ``。"""

    saved = dict(os.environ)
    try:
        for key in PINNED_ENV_KEYS:
            os.environ.pop(key, None)
        credentials = workdir / "credentials"
        credentials.mkdir(parents=True, exist_ok=True)
        os.environ["WATER_AGENT_CREDENTIALS_DIR"] = str(credentials)
        os.environ["WATER_AGENT_TRACE_DIR"] = str(workdir / "traces")
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@contextmanager
def _patched_bindings(original: Any, replacement: Any) -> Iterator[int]:
    """把 ``water_agent`` 各模块里**指向同一对象**的名字都换成 ``replacement``。

    ``from x import f`` 会在导入方模块里另存一份绑定，只改定义处的模块不够；这里扫描已加载
    的 ``water_agent.*`` 模块，凡是 ``is original`` 的属性一律替换，结束时还原。之后才被导入
    的模块从定义处取到的已经是替换后的对象。
    """

    patched: list[tuple[Any, str]] = []
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "water_agent" or name.startswith("water_agent.")):
            continue
        for attr, value in list(vars(module).items()):
            if value is original:
                setattr(module, attr, replacement)
                patched.append((module, attr))
    try:
        yield len(patched)
    finally:
        for module, attr in patched:
            setattr(module, attr, original)


@contextmanager
def _patched_attribute(owner: Any, attr: str, replacement: Any) -> Iterator[None]:
    original = getattr(owner, attr)
    setattr(owner, attr, replacement)
    try:
        yield
    finally:
        setattr(owner, attr, original)


def _qualname(obj: Any) -> str:
    return str(getattr(obj, "__qualname__", None) or type(obj).__qualname__)


class _RecordingStack(ExitStack):
    """记录装配期往 ExitStack 上登记了什么（关闭顺序是可观察行为）。"""

    def __init__(self, sink: list[str]) -> None:
        super().__init__()
        self._sink = sink

    def callback(self, callback: Any, /, *args: Any, **kwds: Any) -> Any:  # type: ignore[override]
        self._sink.append(f"callback:{_qualname(callback)}")
        return super().callback(callback, *args, **kwds)

    def enter_context(self, cm: Any) -> Any:  # type: ignore[override]
        self._sink.append(f"enter_context:{type(cm).__qualname__}")
        return super().enter_context(cm)

    def push(self, exit: Any) -> Any:  # type: ignore[override]
        self._sink.append(f"push:{_qualname(exit)}")
        return super().push(exit)


class _CaptureComplete(Exception):
    """runner 路径在捕获点抛出，中止用例，不构图、不调用模型。"""


class _PlaceholderChatModel:
    """runner 路径的模型替身：只需要 runner 会写入的两个属性。"""

    def __init__(self) -> None:
        self.request_context: Any = None
        self.gateway: Any = None


@dataclass
class _ProfileState:
    side: str
    captures: int = 0
    #: ``http_fake`` profile 的假 HTTP MCP server 端口（由 ``_run_profile`` 起停）。
    http_port: int | None = None
    trace_calls: list[dict[str, Any]] = field(default_factory=list)
    trace_calls_at_capture: list[dict[str, Any]] | None = None
    assemblies: list[Any] = field(default_factory=list)
    stack_ops: list[str] = field(default_factory=list)
    surface: dict[str, Any] | None = None
    capture_lifecycle: dict[str, Any] | None = None
    error: BaseException | None = None
    #: 捕获到的 ``build_agentic_app(model=...)``（P7-S10 端点 profile 用）。
    chat_model: Any = None


# ---------------------------------------------------------------------------
# 从捕获到的 build_agentic_app 入参里读出快照
# ---------------------------------------------------------------------------


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _principal_view(principal: Any) -> dict[str, Any] | None:
    if principal is None:
        return None
    return {
        "kind": getattr(principal.kind, "value", str(principal.kind)),
        "auth_domain": principal.auth_domain,
        "capabilities": sorted(getattr(cap, "value", str(cap)) for cap in principal.capabilities),
    }


def _model_principal(model: Any) -> dict[str, Any] | None:
    """模型发起 provider 调用时会带上的主体（会话侧惰性读取，runner 侧是赋值）。"""

    if model is None:
        return None
    factory = getattr(model, "request_context_factory", None)
    context = factory() if callable(factory) else getattr(model, "request_context", None)
    return _principal_view(getattr(context, "principal", None))


def _segment(prompt_text: str, header: str) -> str | None:
    """取以 ``header`` 开头、到下一个空行为止的提示段（段与段之间以空行分隔）。"""

    start = prompt_text.find(header)
    if start < 0:
        return None
    end = prompt_text.find("\n\n", start)
    return prompt_text[start:] if end < 0 else prompt_text[start:end]


_VERSION_SEGMENT_HEADER = "【规范版本状态（注册表快照）】"
_TOOL_GUIDE_HEADER = "【可用工具】"


def _normalized_system_prompt(prompt_text: str) -> str:
    """版本快照段随 ``data/versions.json`` 变化，与工具装配无关，用占位符替换后再摘要。"""

    segment = _segment(prompt_text, _VERSION_SEGMENT_HEADER)
    if segment is None:
        return prompt_text
    return prompt_text.replace(segment, "<version_snapshot>", 1)


def _retriever_chain(spec: Any) -> list[str]:
    """retrieve 工具闭包里实际持有的检索器，沿 ``_inner`` 展开（rerank 包装是否套上）。"""

    from water_agent.retrieval.base import Retriever

    found: Any = None
    for cell in getattr(spec.execute, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if isinstance(value, Retriever):
            found = value
            break
    chain: list[str] = []
    while found is not None and len(chain) < 5:
        chain.append(type(found).__name__)
        found = getattr(found, "_inner", None)
    return chain


def _probes(registry: Any) -> dict[str, Any]:
    """经 ``ToolRegistry.invoke``（含护栏）跑几次真实调用，验证工具之间的接线没断。"""

    names = registry.list_all_names()
    out: dict[str, Any] = {}
    if "retrieve_clauses" in names:
        result = registry.invoke("retrieve_clauses", query=PROBE_QUERY, top_k=3)
        ids = [str(hit.get("source_id")) for hit in result.get("hits", []) or []]
        out["retrieve"] = {"error": bool(result.get("error")), "ids": ids}
        out["retriever_chain"] = _retriever_chain(registry.get("retrieve_clauses"))
        if "cite_clauses" in names and ids:
            # 一个刚检索到的条款号 + 一个不可能检索到的：两者共享上下文时只拒绝后者
            cited = registry.invoke("cite_clauses", article_ids=[ids[0], BOGUS_ARTICLE])
            out["cite"] = {
                "registered": cited.get("registered"),
                "invalid_ids": list(cited.get("invalid_ids", []) or []),
                "code": cited.get("code"),
            }
    echoes: dict[str, Any] = {}
    for name in names:
        if name.startswith("mcp_") and name.endswith("_echo_query"):
            echoed = registry.invoke(name, query="snapshot-probe")
            echoes[name] = {
                "error": bool(echoed.get("error")),
                "code": echoed.get("code"),
                "keys": sorted(echoed),
            }
    if echoes:
        out["mcp_echo"] = echoes
    return out


#: 暴露探针（P7-S4）：对每个 profile 的暴露策略各决定一次，记下开放了什么、为什么、丢了什么。
#: 四条问题分别对应规范问答 / 计算 / 材料统计 / 读图 + 材料统计（最紧的预算组合）。
EXPOSURE_PROBES: tuple[tuple[str, str], ...] = (
    ("clause_qa", "给水管道的设计秒流量如何确定"),
    ("calc", "DN150 给水管、流量 20 L/s 时流速多少"),
    ("materials", "帮我统计材料：S1 DN100 PPR 50 米"),
    ("drawing_materials", "根据图纸 P-01 统计管材用量"),
)


def _exposure_view(policy: Any) -> dict[str, Any] | None:
    if policy is None:
        return None
    probes: dict[str, Any] = {}
    requested = [group.group_id for group in policy.groups if group.rule.mode == "request"]
    cases: list[tuple[str, str, list[str]]] = [(label, query, []) for label, query in EXPOSURE_PROBES]
    if requested:
        # P7-S12：有 request 组时再探一次"本条消息勾选了它"（只对有 request 组的 profile 出现）
        cases.append(("clause_qa_requested", EXPOSURE_PROBES[0][1], requested))
    for label, query, enabled in cases:
        decision = policy.decide(query, request_enabled=enabled) if enabled else policy.decide(query)
        view = decision.to_state()
        if decision.strategy == "intent":
            guide = policy.render_guide(decision.exposed)
            view["guide"] = {"chars": len(guide), "sha256": _sha16(guide)}
        probes[label] = view
    return {
        "strategy": policy.strategy,
        "groups": [[group.group_id, group.rule.mode, list(group.tools)] for group in policy.groups],
        "probes": probes,
    }


def _inspect_app_kwargs(kwargs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = kwargs["prompt"]
    registry = getattr(prompt, "_tool_registry", None)
    if registry is None:
        raise SnapshotHarnessError("build_agentic_app 收到的 prompt 不持有工具注册表，无法采集")
    tools = list(kwargs.get("tools") or [])
    openai_tools = registry.list_tools()
    registration_order = [entry["function"]["name"] for entry in openai_tools]
    per_tool: dict[str, Any] = {}
    for name in registration_order:
        spec = registry.get(name)
        payload = spec.to_openai_tool()
        per_tool[name] = {
            "schema_sha256": _sha16(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            "schema_chars": len(json.dumps(payload, ensure_ascii=False)),
            "timeout_seconds": spec.timeout_seconds,
            "has_guard": spec.guard is not None,
            "execute": _qualname(spec.execute),
        }
    prompt_text = str(prompt.assemble_prompt({}).content)
    normalized = _normalized_system_prompt(prompt_text)
    surface = {
        "registration_order": registration_order,
        "model_tool_order": [tool.name for tool in tools],
        "tools": per_tool,
        "schema_chars_total": len(json.dumps(openai_tools, ensure_ascii=False)),
        "tool_guide": _segment(prompt_text, _TOOL_GUIDE_HEADER),
        "system_prompt": {"chars": len(normalized), "sha256": _sha16(normalized)},
        "principal": _principal_view(kwargs["request_context"].principal),
        "registry_principal": _principal_view(registry._resolve_principal()),
        "exposure": _exposure_view(kwargs.get("exposure")),
    }
    cache = kwargs.get("cache_service")
    lifecycle = {
        "app_kwargs": sorted(kwargs),
        "max_tool_turns": kwargs.get("max_tool_turns"),
        "checkpointer": type(kwargs.get("checkpointer")).__name__,
        "cache_service": None
        if cache is None
        else {
            "type": type(cache).__name__,
            # 只记是否非空：manifest 摘要含解释器路径与临时文件路径，原样入基线会让快照
            # 在不同机器上不一致（与 _mcp_view 的 manifest_hash_nonempty 同做法）
            "mcp_server_manifest_nonempty": bool(getattr(cache, "_mcp_server_manifest", "")),
        },
        "tools_bound_to_prompt_registry": all(
            getattr(tool, "registry", None) is registry for tool in tools
        ),
        "chat_model_principal": _model_principal(kwargs.get("model")),
        "probes": _probes(registry),
    }
    return surface, lifecycle


# ---------------------------------------------------------------------------
# 每个 profile 的驱动
# ---------------------------------------------------------------------------


def _fixture_retriever() -> Any:
    from water_agent.retrieval.loader import load_clauses_jsonl
    from water_agent.retrieval.trivium import TriviumRetriever

    return TriviumRetriever(db_path=None, fallback_clauses=load_clauses_jsonl(FIXTURE_CLAUSES))


def _prepare_drawings(directory: Path) -> Path:
    from water_agent.drawing.synth import build_synthetic_drawing

    directory.mkdir(parents=True, exist_ok=True)
    build_synthetic_drawing(directory, drawing_id="P-01")
    return directory


def _write_mcp_config(path: Path, profile: Profile, http_port: int | None = None) -> Path:
    from water_agent.mcp.config import MCP_CONFIG_VERSION

    path.parent.mkdir(parents=True, exist_ok=True)
    if profile.mcp == "invalid":
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        return path
    servers: list[dict[str, Any]] = []
    if profile.mcp == "http_fake":
        if http_port is None:
            raise SnapshotHarnessError("http_fake profile 需要先启动假 HTTP MCP server")
        servers.append(
            {
                "server_id": profile.mcp_server_id,
                "transport": "streamable_http",
                "url": f"http://127.0.0.1:{http_port}/mcp",
                "allow_private_network": True,
                "allow": list(FAKE_MCP_ALLOW),
            }
        )
    if profile.mcp in {"fake", "confirm_skip", "ready_flag"}:
        if not FAKE_MCP_SERVER.is_file():
            raise SnapshotHarnessError(f"缺少 fake MCP server：{FAKE_MCP_SERVER}")
        servers.append(
            {
                "server_id": profile.mcp_server_id,
                "command": sys.executable,
                "args": [str(FAKE_MCP_SERVER), "--mode", "ok"],
                "allow": list(FAKE_MCP_ALLOW),
                "human_confirmation": profile.mcp != "fake",
            }
        )
    payload = {
        "version": MCP_CONFIG_VERSION,
        "confirmation_handler_ready": profile.mcp == "ready_flag",
        "servers": servers,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _apply_profile_environment(profile: Profile, workdir: Path, http_port: int | None = None) -> Path | None:
    """按 profile 设置环境变量，返回要通过参数传入的图纸根（只有 drawing=arg 时非空）。"""

    for key in (
        "DRAWING_ROOT",
        "WATER_AGENT_MCP_CONFIG",
        "DEEPSEEK_API_KEY",
        "WATER_AGENT_SKILLS",
        "WATER_AGENT_LLM_BASE_URL",
        "WATER_AGENT_LLM_MODEL",
        "WATER_AGENT_LLM_API_KEY",
        "WATER_AGENT_LLM_COMPATIBILITY_REPORT",
    ):
        os.environ.pop(key, None)
    drawing_arg: Path | None = None
    if profile.drawing in {"arg", "env"}:
        drawings = _prepare_drawings(workdir / "drawings")
        if profile.drawing == "env":
            os.environ["DRAWING_ROOT"] = str(drawings)
        else:
            drawing_arg = drawings
    if profile.mcp != "disabled":
        os.environ["WATER_AGENT_MCP_CONFIG"] = str(
            _write_mcp_config(workdir / "mcp_servers.json", profile, http_port)
        )
    if profile.web:
        os.environ["DEEPSEEK_API_KEY"] = _PLACEHOLDER_API_KEY
    if profile.skills:
        os.environ["WATER_AGENT_SKILLS"] = "on"
    if profile.web_search:
        os.environ["WATER_AGENT_WEB_SEARCH"] = "on"
        os.environ["WATER_AGENT_WEB_SEARCH_PROVIDER"] = "searxng"
        os.environ["WATER_AGENT_WEB_SEARCH_BASE_URL"] = WEB_SEARCH_BASE_URL
    if profile.pick_model:
        from water_agent.generation.compatibility import (
            ENDPOINT_COMPATIBILITY_CHECKS,
            ENDPOINT_PROBE_VERSION,
            CompatibilityReport,
            pair_report_path,
            write_report,
        )
        from water_agent.generation.endpoints import DEFAULT_CHAT_BASE_URL
        from water_agent.generation.model_catalog import CatalogEntry, write_catalog

        write_catalog([CatalogEntry(id=PICKED_MODEL, label="快速模型", tier="fast", thinking="off")])
        write_report(
            pair_report_path(DEFAULT_CHAT_BASE_URL, PICKED_MODEL),
            CompatibilityReport(
                version=ENDPOINT_PROBE_VERSION,
                status="compatible",
                model=PICKED_MODEL,
                endpoint=DEFAULT_CHAT_BASE_URL,
                checks={name: "passed" for name in ENDPOINT_COMPATIBILITY_CHECKS},
                schema_tokens=1000,
                evidence={"probe_version": ENDPOINT_PROBE_VERSION},
            ),
        )
        os.environ["DEEPSEEK_API_KEY"] = _PLACEHOLDER_API_KEY
    if profile.endpoint:
        from water_agent.generation.compatibility import (
            ENDPOINT_COMPATIBILITY_CHECKS,
            ENDPOINT_PROBE_VERSION,
            CompatibilityReport,
            write_report,
        )

        report = write_report(
            workdir / "llm_compatibility_report.json",
            CompatibilityReport(
                version=ENDPOINT_PROBE_VERSION,
                status="compatible",
                model=CUSTOM_ENDPOINT_MODEL,
                endpoint=CUSTOM_ENDPOINT_BASE_URL,
                checks={name: "passed" for name in ENDPOINT_COMPATIBILITY_CHECKS},
                schema_tokens=1000,
                evidence={"probe_version": ENDPOINT_PROBE_VERSION},
            ),
        )
        os.environ["WATER_AGENT_LLM_BASE_URL"] = CUSTOM_ENDPOINT_BASE_URL
        os.environ["WATER_AGENT_LLM_MODEL"] = CUSTOM_ENDPOINT_MODEL
        os.environ["WATER_AGENT_LLM_API_KEY"] = _PLACEHOLDER_API_KEY
        os.environ["WATER_AGENT_LLM_COMPATIBILITY_REPORT"] = str(report)
    return drawing_arg


def _session_args(profile: Profile, workdir: Path, drawing_arg: Path | None) -> argparse.Namespace:
    if profile.web:
        from water_agent.webapp.run_manager import WebAgenticSessionFactory

        factory = WebAgenticSessionFactory(
            None,  # type: ignore[arg-type]  # 只用 _args()，不碰数据库
            None,  # type: ignore[arg-type]
            trace_dir=workdir / "traces",
            session_dir=workdir / "sessions",
            mock_generator=False,
        )
        return factory._args()
    if profile.drawing == "off":
        drawing_root: str | None = ""
    elif profile.drawing == "arg":
        drawing_root = str(drawing_arg)
    else:
        drawing_root = None
    return argparse.Namespace(
        db_path=None,
        dimensions=None,
        rerank="local",
        rerank_model=None,
        rerank_base_url=None,
        rerank_api_key=None,
        top_k=5,
        recall_top_k=20,
        hybrid_alpha=0.7,
        # 端点 profile 不给显式值：交给 resolve_chat_endpoint 按（profile 设好的）运维环境变量解析
        model=None if profile.endpoint else (PICKED_MODEL if profile.pick_model else "deepseek-chat"),
        base_url=None if profile.endpoint else "https://api.deepseek.com",
        api_key_env=None if profile.endpoint else "DEEPSEEK_API_KEY",
        api_key=None,
        mock_generator=not (profile.endpoint or profile.pick_model),
        session_dir=str(workdir / "sessions"),
        trace_dir=str(workdir / "traces"),
        stream=False,
        drawing_root=drawing_root,
    )


def _web_request_context(thread_id: str) -> Any:
    from water_agent.runtime.governance.contracts import Principal, RequestContext
    from water_agent.webapp.auth.dependencies import _MEMBER_CAPABILITIES, TENANT_ID

    return RequestContext(
        principal=Principal.authenticated(
            subject="user:tool-assembly-snapshot",
            auth_domain=TENANT_ID,
            capabilities=_MEMBER_CAPABILITIES,
        ),
        request_id="req-tool-assembly-snapshot",
        thread_id=thread_id,
    )


def _drive_session(profile: Profile, workdir: Path, state: _ProfileState) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    from water_agent.runtime.agentic import AgenticSession

    drawing_arg = _apply_profile_environment(profile, workdir, state.http_port)
    thread_id = f"snap-{profile.name}"
    args = _session_args(profile, workdir, drawing_arg)
    stack = _RecordingStack(state.stack_ops)
    try:
        with stack:
            AgenticSession(
                args,
                stack=stack,
                thread_id=thread_id,
                request_context=_web_request_context(thread_id) if profile.web else None,
                checkpointer=None if profile.cli_checkpointer else MemorySaver(),
                retriever=_fixture_retriever(),
                mcp_enabled=profile.mcp != "disabled",
                web_search=profile.web_search,
            )
    except SnapshotHarnessError:
        raise
    except Exception as exc:  # noqa: BLE001 — 抛不抛、抛什么都是被记录的行为
        state.error = exc


def _cleanup_runner_mcp_cache(runner_module: Any) -> None:
    """S2 统一 runner 语义后这个缓存会被删掉；不存在就什么都不做。"""

    cleanup = getattr(runner_module, "_cleanup_mcp_assembly_cache", None)
    if callable(cleanup):
        cleanup()


def _drive_runner(profile: Profile, workdir: Path, state: _ProfileState) -> None:
    import water_agent.harness.runner as runner_module

    _apply_profile_environment(profile, workdir, state.http_port)
    _cleanup_runner_mcp_cache(runner_module)
    unused_eval = workdir / "unused.jsonl"
    unused_eval.write_text("", encoding="utf-8")
    runner = runner_module.HarnessRunner(
        eval_path=unused_eval,
        report_dir=workdir / "reports",
        trace_dir=workdir / "traces",
        fixture_path=FIXTURE_CLAUSES,
    )
    runner._real_retriever = _fixture_retriever()
    runner._real_chat_model = _PlaceholderChatModel()  # type: ignore[assignment]
    case = SimpleNamespace(id="tool-assembly-snapshot", query=PROBE_QUERY)
    try:
        runner._run_real_agentic_case(case)
    except _CaptureComplete:
        pass
    except SnapshotHarnessError:
        raise
    except Exception as exc:  # noqa: BLE001 — 抛不抛、抛什么都是被记录的行为
        state.error = exc


def _alive_clients(assemblies: Sequence[Any]) -> int:
    alive = 0
    for assembly in assemblies:
        for session in getattr(assembly, "sessions", ()):
            client = getattr(session, "client", None)
            if client is not None and getattr(client, "connected", False):
                alive += 1
    return alive


def _mcp_view(assemblies: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "exposed": list(assembly.exposed_tool_names),
            "skipped_servers": [[skip.server_id, skip.code] for skip in assembly.skipped_servers],
            "skipped_tools": [
                [session.result.server_id, skip.remote_tool, skip.code]
                for session in assembly.sessions
                for skip in session.result.skipped
            ],
            "manifest_hash_nonempty": bool(assembly.manifest_hash),
        }
        for assembly in assemblies
    ]


def _error_view(error: BaseException | None) -> dict[str, Any] | None:
    if error is None:
        return None
    return {"type": type(error).__name__, "code": getattr(error, "code", None)}


def _start_fake_http_server() -> tuple[Any, int]:
    """起假 HTTP MCP server（明文回环，不需要证书；进程内线程，不拉子进程）；返回 (句柄, 端口)。"""
    if not FAKE_MCP_HTTP_SERVER.is_file():
        raise SnapshotHarnessError(f"缺少 fake HTTP MCP server：{FAKE_MCP_HTTP_SERVER}")
    from water_agent.harness.mcp_http_eval import fake_module

    module = fake_module()
    args = module.build_parser().parse_args(["--mode", "json", "--plain"])
    running = module.start_servers(args)
    return running, int(running.mcp_port)


def _run_profile(profile: Profile, workdir: Path) -> dict[str, Any]:
    from water_agent.graphs import agent_rag
    from water_agent.mcp import wiring
    from water_agent.observability.json_trace import JsonTraceRecorder
    from water_agent.tools.registry import ToolRegistry

    # 先导入两条装配路径，让它们对 build_agentic_app / open_gateway_sessions 的绑定已经存在
    for module_name in ("water_agent.harness.runner", "water_agent.runtime.agentic"):
        importlib.import_module(module_name)

    workdir.mkdir(parents=True, exist_ok=True)
    state = _ProfileState(side=profile.side)
    original_build = agent_rag.build_agentic_app
    original_open = wiring.open_gateway_sessions
    original_record = JsonTraceRecorder.record
    original_register = ToolRegistry.register

    def capture(*args: Any, **kwargs: Any) -> Any:
        state.captures += 1
        state.chat_model = kwargs.get("model")
        state.trace_calls_at_capture = list(state.trace_calls)
        state.surface, state.capture_lifecycle = _inspect_app_kwargs(kwargs)
        if profile.side == "runner":
            raise _CaptureComplete()
        return original_build(*args, **kwargs)

    def spy_open(*args: Any, **kwargs: Any) -> Any:
        assembly = original_open(*args, **kwargs)
        state.assemblies.append(assembly)
        return assembly

    def spy_record(self: Any, node: str, event: str, *args: Any, **kwargs: Any) -> Any:
        state.trace_calls.append({"node": node, "event": event, "landed": bool(self.started)})
        return original_record(self, node, event, *args, **kwargs)

    def faulty_register(self: Any, spec: Any) -> None:
        if spec.name.startswith("mcp_"):
            raise ValueError("tool-assembly-snapshot：注入的 MCP 工具注册故障")
        original_register(self, spec)

    driver: Callable[[Profile, Path, _ProfileState], None] = (
        _drive_session if profile.side == "session" else _drive_runner
    )
    http_server: Any = None
    if profile.mcp == "http_fake":
        http_server, state.http_port = _start_fake_http_server()
    with ExitStack() as patches:
        patches.enter_context(_patched_bindings(original_build, capture))
        patches.enter_context(_patched_bindings(original_open, spy_open))
        patches.enter_context(_patched_attribute(JsonTraceRecorder, "record", spy_record))
        if profile.fault == "mcp_register":
            patches.enter_context(_patched_attribute(ToolRegistry, "register", faulty_register))
        alive_after = 0
        try:
            driver(profile, workdir, state)
            alive_after = _alive_clients(state.assemblies)
        finally:
            if profile.side == "runner":
                import water_agent.harness.runner as runner_module

                _cleanup_runner_mcp_cache(runner_module)
            for assembly in state.assemblies:  # 防御：不管现状如何，快照本身不留子进程
                assembly.close()
            if http_server is not None:
                http_server.stop()

    if state.error is None and state.captures != 1:
        raise SnapshotHarnessError(
            f"profile {profile.name}：装配没有抛异常，却捕获到 {state.captures} 次 "
            "build_agentic_app 调用（应恰好 1 次）——捕获点移位，快照不可信"
        )
    lifecycle: dict[str, Any] = {
        "captures": state.captures,
        "exception": _error_view(state.error),
        "trace_record_calls": state.trace_calls_at_capture
        if state.trace_calls_at_capture is not None
        else state.trace_calls,
        "stack": list(state.stack_ops),
        "mcp": _mcp_view(state.assemblies),
        "mcp_clients_alive_after_scope": alive_after,
        "app": state.capture_lifecycle,
    }
    if profile.endpoint:
        # 只给端点 profile 记（其余 profile 的 lifecycle 逐字不变）：chat model 实际指向哪里
        lifecycle["chat_model_endpoint"] = _chat_model_endpoint(state.chat_model)
    if profile.pick_model:
        # 同上，只给选模型 profile 记：所选模型实际带的 extra_body（thinking=off 才附加关闭思考）
        view = _chat_model_endpoint(state.chat_model, picked=PICKED_MODEL)
        if view is not None:
            view["extra_body"] = getattr(getattr(state.chat_model, "inner", None), "extra_body", None)
        lifecycle["chat_model_endpoint"] = view
    return {
        "side": profile.side,
        "description": profile.description,
        "surface": state.surface,
        "lifecycle": lifecycle,
    }


def _chat_model_endpoint(model: Any, *, picked: str | None = None) -> dict[str, Any] | None:
    if model is None:
        return None
    from water_agent.generation.compatibility import endpoint_verification
    from water_agent.generation.endpoints import resolve_chat_endpoint

    inner = getattr(model, "inner", None)
    return {
        "wrapper": type(model).__name__,
        "base_url": getattr(inner, "openai_api_base", None),
        "model": getattr(inner, "model_name", None),
        # 选了模型时按所选模型验（逐对报告），否则按全局解析
        "compatibility": endpoint_verification(resolve_chat_endpoint(model=picked)).status,
    }


def current_versions() -> dict[str, str]:
    from water_agent.mcp.config import MCP_CONFIG_VERSION
    from water_agent.prompting.assembler import PROMPT_TEMPLATE_VERSION
    from water_agent.runtime.governance.inventory import TOOL_POLICY_VERSION

    return {
        "MCP_CONFIG_VERSION": MCP_CONFIG_VERSION,
        "PROMPT_TEMPLATE_VERSION": PROMPT_TEMPLATE_VERSION,
        "TOOL_POLICY_VERSION": TOOL_POLICY_VERSION,
    }


def _info() -> dict[str, Any]:
    """只供人读，不参与比较。

    不记 git HEAD：本模块不起子进程（``tests/test_dwg_upload.py`` 的子进程调用点白名单），
    采集时的提交号写进该步决策记录。
    """

    from importlib.metadata import PackageNotFoundError, version

    def _pkg(name: str) -> str | None:
        try:
            return version(name)
        except PackageNotFoundError:
            return None

    return {
        "python": platform.python_version(),
        "langgraph": _pkg("langgraph"),
        "langchain_core": _pkg("langchain-core"),
    }


def collect_snapshot(profiles: Sequence[Profile] | None = None) -> dict[str, Any]:
    """采集快照。``profiles`` 给出时只跑这些（反证测试用），否则跑全部 :data:`PROFILES`。"""

    selected = tuple(profiles) if profiles is not None else PROFILES
    names = [profile.name for profile in selected]
    if len(set(names)) != len(names):
        raise SnapshotHarnessError(f"profile 名重复：{names}")
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="wa-tool-snapshot-") as tmp:
        root = Path(tmp)
        with hermetic_environment(root):
            for profile in selected:
                results[profile.name] = _run_profile(profile, root / profile.name)
    return {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "generated_by": GENERATED_BY,
        "versions": current_versions(),
        "profiles": results,
        "info": _info(),
    }


# ---------------------------------------------------------------------------
# 比较与写入守卫
# ---------------------------------------------------------------------------

#: 不参与比较的顶层键。
NON_COMPARED_KEYS = frozenset({"info"})


def comparable(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key not in NON_COMPARED_KEYS}


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 120 else text[:117] + "..."


def _diff(base: Any, head: Any, pointer: str, out: list[tuple[str, Any, Any]]) -> None:
    if isinstance(base, dict) and isinstance(head, dict):
        for key in sorted(set(base) | set(head)):
            child = f"{pointer}/{_escape_pointer(str(key))}"
            if key not in base:
                out.append((child, "<absent>", head[key]))
            elif key not in head:
                out.append((child, base[key], "<absent>"))
            else:
                _diff(base[key], head[key], child, out)
        return
    if isinstance(base, list) and isinstance(head, list) and len(base) == len(head):
        for index, (left, right) in enumerate(zip(base, head, strict=True)):
            _diff(left, right, f"{pointer}/{index}", out)
        return
    if base != head:
        out.append((pointer or "/", base, head))


def diff_entries(base: dict[str, Any], head: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """返回 (JSON pointer, 基线值, 新值) 列表；``info`` 不参与比较。"""

    out: list[tuple[str, Any, Any]] = []
    _diff(comparable(base), comparable(head), "", out)
    return out


def diff_snapshots(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    return [
        f"{pointer}: {_short(old)} -> {_short(new)}" for pointer, old, new in diff_entries(base, head)
    ]


_SURFACE_POINTER = re.compile(r"^/profiles/([^/]+)/surface(/|$)")
#: surface 有变化时必须递进其中至少一个（工具集 / capability 映射 → TOOL_POLICY_VERSION；
#: 提示文案或分段 → PROMPT_TEMPLATE_VERSION）。
SURFACE_VERSION_KEYS = ("TOOL_POLICY_VERSION", "PROMPT_TEMPLATE_VERSION")


def write_guard_violations(
    base: dict[str, Any], head: dict[str, Any], *, reason: str | None
) -> list[str]:
    """``--write`` 前的守卫。返回空列表才允许写。

    - surface 的**值**变了（两边都采到了 surface）而 ``SURFACE_VERSION_KEYS`` 一个都没变
      → 拒绝：模型可见面变了却不递进版本号，旧缓存键与旧结论都会被当成仍然有效。
    - 其余任何变化（lifecycle、某个 profile 从采到变成抛错或反过来）→ 必须给 ``reason``，
      并在决策记录里贴 ``--diff`` 的输出。
    """

    entries = diff_entries(base, head)
    if not entries:
        return []
    violations: list[str] = []
    base_profiles = base.get("profiles", {})
    head_profiles = head.get("profiles", {})
    surface_value_changes: list[str] = []
    for pointer, _old, _new in entries:
        match = _SURFACE_POINTER.match(pointer)
        if match is None:
            continue
        name = match.group(1).replace("~1", "/").replace("~0", "~")
        if (base_profiles.get(name) or {}).get("surface") is not None and (
            head_profiles.get(name) or {}
        ).get("surface") is not None:
            surface_value_changes.append(pointer)
    versions_bumped = any(
        base.get("versions", {}).get(key) != head.get("versions", {}).get(key)
        for key in SURFACE_VERSION_KEYS
    )
    if surface_value_changes and not versions_bumped:
        violations.append(
            "模型可见面（surface）变了，但 TOOL_POLICY_VERSION / PROMPT_TEMPLATE_VERSION 都没递进："
            + ", ".join(surface_value_changes[:5])
            + (" …" if len(surface_value_changes) > 5 else "")
        )
    if not (reason or "").strip():
        violations.append("快照有变化，必须用 --reason 说明原因，并把 --diff 的输出贴进决策记录")
    return violations


def dump_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_snapshot(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("generated_by") != GENERATED_BY:
        raise SnapshotHarnessError(f"{path} 不是本工具生成的装配快照")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="与基线比对：一致 exit 0，有差异 exit 1")
    mode.add_argument("--diff", action="store_true", help="打印与基线的差异")
    mode.add_argument("--write", action="store_true", help="经写入守卫后覆盖基线")
    parser.add_argument("baseline", nargs="?", default=str(DEFAULT_BASELINE))
    parser.add_argument("--reason", default=None, help="--write 时说明快照为什么变")
    args = parser.parse_args(argv)
    baseline_path = Path(args.baseline)

    try:
        head = collect_snapshot()
    except SnapshotHarnessError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2

    base: dict[str, Any] | None = None
    if baseline_path.exists():
        try:
            base = load_snapshot(baseline_path)
        except (OSError, ValueError, SnapshotHarnessError) as exc:
            print(f"!! 无法读取基线 {baseline_path}：{exc}", file=sys.stderr)
            return 2

    if args.write:
        if base is not None:
            if not diff_entries(base, head):
                print(f"装配快照无变化，未改写 {baseline_path}")
                return 0
            violations = write_guard_violations(base, head, reason=args.reason)
            if violations:
                for line in violations:
                    print(f"!! {line}", file=sys.stderr)
                return 1
            for line in diff_snapshots(base, head):
                print(line)
        if args.reason:
            head["info"]["last_write_reason"] = args.reason
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(dump_snapshot(head), encoding="utf-8")
        print(f"已写入 {baseline_path}（{len(head['profiles'])} 个 profile）")
        return 0

    if base is None:
        print(f"!! 基线不存在：{baseline_path}（首次采集用 --write）", file=sys.stderr)
        return 2
    lines = diff_snapshots(base, head)
    if not lines:
        print(f"装配快照与基线一致（{len(head['profiles'])} 个 profile）")
        return 0
    for line in lines:
        print(line)
    if args.check:
        print(f"!! 装配快照与基线不一致：{len(lines)} 处差异", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

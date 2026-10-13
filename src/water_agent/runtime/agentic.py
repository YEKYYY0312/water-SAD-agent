"""Shared AgenticSession assembly for CLI and Web runs.

The Web entry point is deliberately a thin adapter around this module.  Model,
retriever, MCP, ToolRegistry, ProviderGateway and GuardrailPipeline assembly is
kept here so the CLI and Web cannot silently grow different agent runtimes.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from water_agent.domain.models import RetrievedClause
from water_agent.generation.chat_model import build_chat_model
from water_agent.generation.compatibility import (
    PROBE_HINT,
    endpoint_verification,
    require_verified_endpoint,
)
from water_agent.generation.endpoints import (
    EndpointConfigError,
    chat_env_configured,
    resolve_chat_endpoint,
)
from water_agent.graphs.agent_rag import (
    PER_TURN_RESET,
    _extract_validation_signal,
    astream_agentic,
    build_agentic_app,
    stream_agentic,
)
from water_agent.graphs.confirmation import (
    STATUS_WAITING_CONFIRMATION,
    ConfirmationRuntime,
    pending_confirmation_request,
    public_confirmation_request,
)
from water_agent.ingestion.version_registry import get_version_registry
from water_agent.mcp.http_client import mcp_request_scope
from water_agent.mcp.pool import McpLease
from water_agent.mcp.web_layer import disabled_skill_names
from water_agent.mcp.wiring import load_mcp_gateway
from water_agent.memory.recall import MemoryRecallResult
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.plugins import (
    AssemblyContext,
    DrawingContribution,
    McpContribution,
    McpGatewayPlugin,
    SkillsContribution,
    assemble_tools,
    builtin_plugins,
)
from water_agent.prompting import PromptAssembler
from water_agent.prompting.history import RUNTIME_CONTEXT_PREFIX
from water_agent.prompting.runtime_context import RuntimeContextProjection, default_context_builder
from water_agent.retrieval.base import Retriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.rerank import (
    BailianReranker,
    LocalReranker,
    RerankingRetriever,
    build_bailian_reranker,
)
from water_agent.retrieval.trivium import DEFAULT_INDEX_DIR as TRIVIUM_INDEX_DIR
from water_agent.retrieval.trivium import (
    IndexSelectionError,
    TriviumRetriever,
    resolve_index_path,
)
from water_agent.runtime.cancellation import RunCancelled, cancellation_scope
from water_agent.runtime.governance.binding import (
    default_gateway_with_sink,
    local_request_context,
)
from water_agent.runtime.governance.cache_service import AnswerCacheService
from water_agent.runtime.governance.contracts import RequestContext
from water_agent.session import default_checkpoint_serde, session_db_path

# This module is one level deeper than water_agent/cli.py, so the repository
# root is parents[3] here, not parents[2].
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURE = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
# 索引目录与选择规则归 retrieval.trivium 统一维护（CLI/Web/harness 共用一套）；
# 这里保留别名，历史调用方与测试仍按 agentic.DEFAULT_INDEX_DIR 引用。
DEFAULT_INDEX_DIR = TRIVIUM_INDEX_DIR


#: 预检：对话模型没配齐（缺 Key / 端点配置不成立 / 本地模型缺证据）。
CHAT_MODEL_NOT_CONFIGURED = "WEB_MODEL_NOT_CONFIGURED"
#: 预检：非缺省对话端点没有通过的兼容性报告（P7-S10 硬门槛，缺省 DeepSeek 免检）。
CHAT_MODEL_NOT_VERIFIED = "WEB_MODEL_NOT_VERIFIED"


def chat_model_problem(
    environment: Callable[[str], str | None] = os.getenv,
) -> tuple[str, str] | None:
    """对话模型能否装配；不能时返回 ``(错误码, 说明)``。只看配置，不读出也不回显 Key 值。

    与 ``build_chat_model`` 共用 ``resolve_chat_endpoint``（端点 / 模型 / Key 名 + 三层解析）：两处各自
    判断时曾出现「这里认 _FILE、那里只认 env」，配了文件便得到 424 放行、真跑 503 的矛盾结果。
    缺省端点缺 Key 时说明沿用原文；端点改了却没配专用 Key 时写明缺哪一项（不回落 DeepSeek 的 Key）。
    """

    from water_agent.generation.local_model import local_model_configured, local_model_ready

    if local_model_configured(environment) and not chat_env_configured(environment):
        if local_model_ready(environment):
            return None
        return CHAT_MODEL_NOT_CONFIGURED, "Real agent model is not configured"
    try:
        endpoint = resolve_chat_endpoint(getenv=environment)
    except EndpointConfigError as exc:
        return CHAT_MODEL_NOT_CONFIGURED, f"Real agent model is not configured: {exc}"
    missing = endpoint.missing_key(getenv=environment)
    if missing is None:
        verification = endpoint_verification(endpoint, getenv=environment)
        if verification.usable:
            return None
        unverified = (
            f"Chat endpoint {endpoint.host} / model {endpoint.model} is not verified "
            f"({verification.reason}); {PROBE_HINT}"
        )
        return CHAT_MODEL_NOT_VERIFIED, unverified
    if endpoint.is_default:
        return CHAT_MODEL_NOT_CONFIGURED, "Real agent model is not configured"
    detail = (
        f"Real agent model is not configured: {missing} is required for {endpoint.host} "
        "(keys of other providers are never sent to it)"
    )
    return CHAT_MODEL_NOT_CONFIGURED, detail


def real_llm_configured(environment: Callable[[str], str | None] = os.getenv) -> bool:
    """Inspect only whether the configured real model can be assembled（见 ``chat_model_problem``）。"""

    return chat_model_problem(environment) is None


def embedding_required(args: argparse.Namespace | None = None) -> bool:
    """本次装配是否会构造真实 embedding provider。

    只有"非 mock 生成器"且"确实选出了真实索引"时才需要：没有索引时检索走 fixture
    兜底，不碰 embedding。选索引本身出错（歧义/配置指向不存在）按"需要"处理，让
    预检把问题暴露出来，而不是当作无索引悄悄降级。
    """

    if args is not None and getattr(args, "mock_generator", False):
        return False
    try:
        return _resolve_db_path(args if args is not None else argparse.Namespace()) is not None
    except ValueError:
        return True


def embedding_configured(environment: Callable[[str], str | None] = os.getenv) -> bool:
    """Inspect only whether the embedding key exists，解析顺序与 chat model 一致。

    与 ``embedding.build_embedding_provider`` 共用 ``resolve_secret``（env → _FILE →
    凭据卷）：预检与实际构造不依赖启动回填，也不会因面板清掉了进程环境里的注入值
    而分叉——否则会重演 chat model 那个「预检放行、真跑报错」的老问题。
    """

    from water_agent.config.credentials import resolve_secret

    return resolve_secret("DASHSCOPE_API_KEY", getenv=environment) is not None


def _embedding_for(db_path: Path, *, request_context=None, gateway=None) -> tuple[object, int]:
    """Build the real embedding provider using the dimension stored in the index."""

    dim = None
    if db_path.exists():
        try:
            import triviumdb

            # 只读打开：探测只要 dim()，而 read_write 在只读根文件系统（compose 的
            # read_only: true）上连打开都会失败，静默退成默认 512 维——维度猜错会让
            # 整条检索链路无声劣化，所以这里必须按库的真实模式打开。
            probe = triviumdb.TriviumDB(str(db_path), access_mode="read_only")
            dim = int(probe.dim())
            probe.close()
        except Exception:  # noqa: BLE001 -- preserve the existing fallback behavior
            dim = None
    if dim is None:
        dim = 512
    from water_agent.embedding import build_embedding_provider

    provider = build_embedding_provider(
        dimensions=dim, request_context=request_context, gateway=gateway
    )
    return provider, dim


def _resolve_db_path(args: argparse.Namespace) -> Path | None:
    """Resolve the retrieval index without silently accepting an explicit bad path.

    ``mock_generator`` 只关掉"目录下的默认索引"这一层兜底，不再顺手把显式配置一起
    吞掉。"假模型"与"没索引"是两件事：harness 与单测要的是前者——默认走 fixture
    语料才能保证 ``baseline.json`` 可复现；而容器 smoke 要的正是"假模型 + 真索引"
    这个组合，才能在没有远端模型凭据的部署里跑通真实检索链路。此前这里先短路、
    后才轮到 ``WATER_AGENT_INDEX_PATH``，部署侧配了索引也会被无声忽略。
    """

    explicit = getattr(args, "db_path", None)
    try:
        return resolve_index_path(
            explicit, allow_default=not getattr(args, "mock_generator", False)
        )
    except IndexSelectionError as exc:
        # 既有契约：显式 --db-path 不存在报 ValueError（CLI 据此给用法错误）。
        raise ValueError(str(exc)) from exc


def _load_fixture_clauses() -> list[RetrievedClause]:
    """加载 fallback fixture；文件缺失时降级为空列表而不是让装配崩掉。

    fixture 只是真实库不可用时的兜底语料，它自己缺失**不该**让整个 session 起不来
    （此前镜像没打进 ``data/`` 时，这里抛 FileNotFoundError 直接打死 Web 的每一次
    问答）。降级后 ``TriviumRetriever`` 的 fallback 为空：真实库在位就照常检索，
    真实库也不在位则检索返回空、由上层按"无依据"处理，两者都不会假装成功。
    """

    try:
        return load_clauses_jsonl(DEFAULT_FIXTURE)
    except (FileNotFoundError, NotADirectoryError):
        return []


def _build_retriever(
    args: argparse.Namespace, *, request_context=None, gateway=None
) -> tuple[Retriever, str]:
    """Build the same retriever used by the existing CLI agentic path."""

    fixture = _load_fixture_clauses()
    db_path = _resolve_db_path(args)
    if db_path is None:
        return TriviumRetriever(db_path=None, fallback_clauses=fixture), ""

    dim = getattr(args, "dimensions", None)
    if getattr(args, "mock_generator", False):
        import triviumdb

        probe = triviumdb.TriviumDB(str(db_path), access_mode="read_only")
        dim = dim or int(probe.dim())
        probe.close()
        from water_agent.retrieval.trivium import mock_embed

        embedding_fn = lambda text: mock_embed(text, dim=dim)
        fallback_kind = "mock"
        fallback_reason = "explicit mock_generator"
    else:
        provider, detected = _embedding_for(
            db_path, request_context=request_context, gateway=gateway
        )
        dim = detected if dim is None else dim
        embedding_fn = provider.embed  # type: ignore[attr-defined]
        fallback_kind = "none"
        fallback_reason = "real db"
    retriever = TriviumRetriever(
        db_path=db_path,
        embedding_fn=embedding_fn,
        hybrid_alpha=getattr(args, "hybrid_alpha", 0.7),
        dim=dim,
        fallback_clauses=fixture,
    )
    retriever._fallback_kind = fallback_kind  # type: ignore[attr-defined]
    retriever._fallback_reason = fallback_reason  # type: ignore[attr-defined]
    return retriever, str(db_path)


def _build_reranker(args: argparse.Namespace, *, request_context=None, gateway=None):
    strategy = getattr(args, "rerank", "local")
    if strategy == "local":
        return LocalReranker()
    if strategy == "bailian":
        return build_bailian_reranker(
            api_key=getattr(args, "rerank_api_key", None),
            model=getattr(args, "rerank_model", None) or BailianReranker.DEFAULT_MODEL,
            base_url=getattr(args, "rerank_base_url", None)
            or BailianReranker.DEFAULT_BASE_URL,
            request_context=request_context,
            gateway=gateway,
        )
    return None


def _build_cache_service(
    args: argparse.Namespace, *, mcp_server_manifest: str = "", skill_library_digest: str = ""
) -> AnswerCacheService:
    """``mcp_server_manifest`` 进缓存键（P7-S3，F6）：MCP server 清单 / 白名单 / 命令任一变化，
    旧回答不该再命中。没有 MCP（或全部停用）时为空串，与历史缓存键同键。
    ``skill_library_digest``（P7-S9）同理：已启用的 skill 集合或任一份正文变化，键随之变化。"""
    reranker = None if getattr(args, "rerank", "local") == "none" else args.rerank
    # P7-S10：对话端点进缓存键（缺省 DeepSeek 为空串，与历史键同键）
    endpoint = resolve_chat_endpoint(
        base_url=getattr(args, "base_url", None),
        model=getattr(args, "model", None),
        api_key_env=getattr(args, "api_key_env", None),
    )
    return AnswerCacheService(
        embedding_model="qwen3.7-text-embedding",
        dimensions=getattr(args, "dimensions", None) or 512,
        top_k=getattr(args, "top_k", 5),
        recall_top_k=getattr(args, "recall_top_k", 20),
        reranker=reranker,
        version_registry=get_version_registry(),
        mcp_server_manifest=mcp_server_manifest,
        skill_library_digest=skill_library_digest,
        model_endpoint_digest=endpoint.cache_digest,
    )


def build_cli_checkpointer(session_dir: str | Path, stack: ExitStack):
    """Build the existing SQLite checkpointer used by CLI and legacy flows."""

    db_path = session_db_path(session_dir)
    try:
        # mkdir 必须在 try 内：镜像把 /app/data 打成只读（Dockerfile 的 chmod -R a-w）
        # 且不收 data/sessions，所以容器里 `--mode agentic` 会在建目录这一步就抛
        # OSError(Errno 30) —— 装配还没开始就死，和"sqlite 不可用"完全同类的故障，
        # 却走了不同结局。并进同一条降级路径：仍然照实写 stderr，不静默。
        db_path.parent.mkdir(parents=True, exist_ok=True)
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        stack.callback(conn.close)
        return SqliteSaver(conn, serde=default_checkpoint_serde())
    except Exception as exc:  # noqa: BLE001 -- retain the documented in-memory fallback
        import sys

        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "sqlite checkpointer 不可用，本轮改用 MemorySaver（进程内态，不落盘）："
                        f"{exc}"
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()


class _ScriptedAgenticModel(BaseChatModel):
    """Offline deterministic model retained for CLI and integration tests."""

    @property
    def _llm_type(self) -> str:
        return "scripted-agentic"

    @staticmethod
    def _latest_query(messages) -> str:
        for message in reversed(messages):
            if (
                isinstance(message, HumanMessage)
                and isinstance(message.content, str)
                and not message.content.startswith(RUNTIME_CONTEXT_PREFIX)
            ):
                return message.content
        return ""

    @staticmethod
    def _latest_hits(messages) -> list[dict]:
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and message.name == "retrieve_clauses":
                payload = json.loads(message.content) if isinstance(message.content, str) else {}
                return payload.get("hits", []) or []
        return []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        query = self._latest_query(messages)
        since_human = []
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                break
            since_human.append(message)
        tool_names = {
            message.name for message in since_human if isinstance(message, ToolMessage)
        }
        if "retrieve_clauses" not in tool_names:
            call = {
                "name": "retrieve_clauses",
                "args": {"query": query, "top_k": 5},
                "id": f"c-retrieve-{uuid4().hex[:6]}",
            }
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))]
            )
        hits = self._latest_hits(messages)
        if hits and "cite_clauses" not in tool_names:
            call = {
                "name": "cite_clauses",
                "args": {"article_ids": [hits[0].get("source_id", "")]},
                "id": f"c-cite-{uuid4().hex[:6]}",
            }
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))]
            )
        if hits:
            top = hits[0]
            answer = (
                f"根据规范检索结果：{top.get('title', '')}"
                f"（{top.get('source_id', '')}）——{top.get('text', '')}"
            )
        else:
            answer = "未能在规范条款库中检索到相关内容。"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer))])


def _latest_runtime_context(messages: list[Any]) -> str | None:
    """会话里最近一次注入的运行时上下文正文（没有则 None）。"""
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if (
            isinstance(message, HumanMessage)
            and isinstance(content, str)
            and content.startswith(RUNTIME_CONTEXT_PREFIX)
        ):
            return content[len(RUNTIME_CONTEXT_PREFIX) :].lstrip("\n")
    return None


def _initial_messages_with_runtime_context(
    query: str,
    projection: RuntimeContextProjection,
    *,
    session_refs: list[str] | None = None,
    history: list[Any] | None = None,
) -> list[HumanMessage]:
    """本轮的输入消息：问题本身，外加"变化才注入"的运行时上下文。

    ``history`` 是这条 thread 已有的消息：上下文与会话里最近一次注入的相同就不再注入。
    此前只靠 ``projection`` 的进程内哈希判断"变没变"，而 Web 每个 run 新建 session、
    projection 也随之新建——每一轮都重新注入一遍，6 轮对话 prompt 里就有 6 份相同的前言
    （review 2026-09-30）。
    """
    context = default_context_builder(session_refs=session_refs)
    rendered = projection.render(context)
    if history is not None and _latest_runtime_context(history) == rendered:
        # 会话里已有同样的前言：记下哈希，让同一 session 的下一轮也走"未变化"分支
        projection.project(context)
        context_text = None
    else:
        context_text = projection.project(context)
    messages: list[HumanMessage] = [HumanMessage(content=query)]
    if context_text:
        messages.insert(0, HumanMessage(content=f"{RUNTIME_CONTEXT_PREFIX}\n{context_text}"))
    return messages


class AgenticSession:
    """One thread, one governed registry and one checkpoint-backed Agentic app.

    ``model`` / ``retriever`` / ``mcp_enabled`` 是评测与测试用的装配注入点：给出时替换
    按 ``args`` 构建的模型与检索器、关掉 MCP 装配，其余（ToolRegistry、cite 可追溯检查、
    提示装配、护栏、引用校验回流、checkpoint）仍走同一套生产装配——mock-agentic
    评测测的就是生产运行时本身，而不是另起一套。

    ``mcp_lease``（P7-S5）：Web 从进程内连接池借来的 MCP 连接。给了就借用它的装配（不打开、
    不关闭进程），并把 ``release`` 登记到 ``stack``；不给就照旧按配置现开现关（CLI）。

    ``confirmation``（P7-S7）：本入口接了人工确认门（只有 Web 会话工厂传 ``True``）。为 ``True`` 时
    要求确认的 MCP server 照常暴露（工具以延迟工具出现），图里接入 ``confirmation_gate``；作答停在
    确认门时返回 ``status="waiting_confirmation"`` 与 ``confirmation`` 请求，由 :meth:`aresume` 继续。
    为 ``False`` 时装配里出现要求确认的工具即报错（没有确认门就不能有延迟工具）。
    """

    #: RunManager 只给声明支持的会话传 ``attachment_context`` / ``on_draft``（AgentSessionFactory 是协议）。
    supports_attachment_context = True
    supports_draft_stream = True

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        stack: ExitStack,
        thread_id: str | None = None,
        request_context: RequestContext | None = None,
        checkpointer: Any | None = None,
        model: BaseChatModel | None = None,
        retriever: Retriever | None = None,
        mcp_enabled: bool = True,
        mcp_lease: McpLease | None = None,
        confirmation: bool = False,
    ) -> None:
        self.args = args
        self.confirmation_enabled = confirmation
        #: 恢复确认时调用方交回的本轮召回结果（只在 aresume 期间非空，见 ConfirmationRuntime.turn_contexts）
        self._resume_contexts: dict[str, Any] = {}
        self.stack = stack
        self.thread_id = thread_id or uuid4().hex
        self.request_context = request_context or local_request_context(
            thread_id=self.thread_id, request_id=f"req-{self.thread_id[:12]}"
        )
        self.recorder = JsonTraceRecorder(Path(args.trace_dir))
        self.gateway = default_gateway_with_sink(self.recorder.record_provider_event)
        api_key = getattr(args, "api_key", None)
        from water_agent.generation.local_model import local_model_configured, local_model_ready

        if local_model_configured() and not local_model_ready():
            raise ValueError(
                "local model is enabled but lacks complete compatibility evidence; "
                "run the Step 46 compatibility probe before starting an Agent session"
            )
        if model is not None:
            self.model: BaseChatModel = model
        elif args.mock_generator:
            self.model = _ScriptedAgenticModel()
        else:
            # 端点 / 模型 / Key 名的唯一来源（P7-S10）：args 里的值是显式覆盖（CLI），None 即按部署配置
            endpoint = resolve_chat_endpoint(
                base_url=getattr(args, "base_url", None),
                model=getattr(args, "model", None),
                api_key_env=getattr(args, "api_key_env", None),
            )
            # 换对话模型的硬门槛（P7-S10）：非缺省端点没有通过的兼容性报告就不装配（铁律②依赖模型真的
            # 用 function calling 调 cite_clauses）；缺省 DeepSeek 免检
            require_verified_endpoint(endpoint)
            try:
                self.model = build_chat_model(
                    model=endpoint.model,
                    base_url=endpoint.base_url,
                    api_key_env=endpoint.api_key_name,
                    api_key=api_key,
                    gateway=self.gateway,
                    request_context_factory=lambda: self.request_context,
                )
            except EndpointConfigError:
                raise
            except ValueError as exc:
                raise ValueError(
                    f"{exc}。agentic 真实模型链路需要 API Key：export {endpoint.api_key_name}=sk-...，"
                    "或用 --mock-generator 走离线占位链路（不连真实模型，不要求 Key）"
                ) from exc

        if retriever is not None:
            self.retriever, self.db_path = retriever, ""
        else:
            self.retriever, self.db_path = _build_retriever(
                args, request_context=self.request_context, gateway=self.gateway
            )
        reranker = _build_reranker(
            args, request_context=self.request_context, gateway=self.gateway
        )
        tool_retriever: Retriever = (
            RerankingRetriever(self.retriever, reranker, recall_top_k=args.recall_top_k)
            if reranker is not None
            else self.retriever
        )

        # 工具装配单入口（P7-S2，docs/design/16-p7-plugin-mcp.md §3）。坏的 MCP 配置在这里、
        # 任何子进程被拉起之前就抛出 MCP_CONFIG_INVALID；MCP 插件打开装配后立即把关闭登记到
        # stack（先于下面的 checkpointer 打开，后于它关闭）。
        # 引用可追溯性检查（Step 8，2026-09-16）的可用条款号取自本 thread 的会话消息（与引用
        # 校验器同源）：Web 每个 run 新建 session，只靠进程内闭包时第二问引用第一问检索到的
        # 条款会被当场拒绝（review 2026-09-30）。
        if mcp_lease is not None:
            if not mcp_enabled:
                raise ValueError("mcp_enabled=False 与 mcp_lease 不能同时给出")
            # 幂等：调用方（Web 会话工厂）通常已在借到时登记过一次
            stack.callback(mcp_lease.release)
            mcp_plugin = McpGatewayPlugin.borrowing(mcp_lease.assembly)
        else:
            mcp_plugin = McpGatewayPlugin.owning(
                load_mcp_gateway() if mcp_enabled else None,
                stack=stack,
                confirmation_ready=confirmation,
            )
        assembly = assemble_tools(
            AssemblyContext(
                retriever=tool_retriever,
                cite_context_source=self._thread_retrieved_source_ids,
                drawing_root=getattr(args, "drawing_root", None),
            ),
            base_principal=self.request_context.principal,
            trace=self.recorder,
            plugins=builtin_plugins(mcp=mcp_plugin, skills_disabled=disabled_skill_names()),
        )
        self.request_context = self.request_context.model_copy(
            update={"principal": assembly.principal}
        )
        self.mcp_assembly = assembly.contribution(McpContribution).assembly
        self.drawing_root_status = assembly.contribution(DrawingContribution).resolution
        #: P7-S9：本次装配的 skill 库（开关 off 时为空库）。提示装配与外层图共用这一个实例。
        self.skill_library = assembly.contribution(SkillsContribution).library
        self.mcp_tool_names = assembly.registered("mcp_gateway")
        registry = assembly.registry
        tools = assembly.tools
        self.registry = registry
        #: 要求人工确认、在 ToolNode 里是延迟工具的工具名（P7-S7）
        self.confirmation_tools = assembly.confirmation_tools
        if assembly.confirmation_tools and not confirmation:
            raise ValueError(
                "装配里有要求人工确认的工具，但本入口没有接确认门（confirmation=False）："
                f"{sorted(assembly.confirmation_tools)}"
            )
        # 只在接了确认门时才把它交给图（不传时 build_agentic_app 的入参与此前逐字相同）
        confirmation_kwargs: dict[str, Any] = (
            {
                "confirmation": ConfirmationRuntime(
                    registry=registry,
                    principal=assembly.principal,
                    tools=assembly.confirmation_tools,
                    request_context=self.request_context,
                    turn_contexts=lambda: self._resume_contexts,
                )
            }
            if confirmation
            else {}
        )
        # P7-S9：只在库里有已启用的 skill 时才交给图（开关 off 时 build_agentic_app 的入参与此前逐字相同，
        # 装配快照的 app_kwargs 被冻结）
        if self.skill_library.enabled:
            confirmation_kwargs["skill_library"] = self.skill_library
        self.projection = RuntimeContextProjection()
        self.config = {"configurable": {"thread_id": self.thread_id}}
        saver = checkpointer if checkpointer is not None else build_cli_checkpointer(args.session_dir, stack)
        self.app = build_agentic_app(
            model=self.model,
            tools=tools,
            checkpointer=saver,
            prompt=PromptAssembler(
                tool_registry=registry, exposure=assembly.exposure, skill_library=self.skill_library
            ),
            trace=self.recorder,
            request_context=self.request_context,
            exposure=assembly.exposure,
            cache_service=(
                _build_cache_service(
                    args,
                    mcp_server_manifest=self.mcp_assembly.manifest_hash,
                    skill_library_digest=self.skill_library.digest(),
                )
                if not args.mock_generator
                else None
            ),
            **confirmation_kwargs,
        )

    def _thread_retrieved_source_ids(self) -> set[str]:
        """本 thread 历来 ``retrieve_clauses`` 返回过的 source_id（读 checkpoint，不读进程内态）。

        读不到（checkpoint 不可用、首轮尚无记录）就返回空集合：cite 工具此时只用本 session
        内累积的检索结果判定，不会放宽——空集合不是"全部放行"，见 cite 工具的并集语义。
        """
        context_ids, _, _ = _extract_validation_signal({"messages": self._thread_messages()})
        return set(context_ids)

    def start_trace(self, run_id: str, query: str) -> None:
        """开启本次 run 的 trace，并在开启的那一次补记 MCP 装配摘要（P7-S3，F13 的后续）。

        装配发生在会话构造时、trace 还没开始；此前 runner 在那时写事件，recorder 尚未
        start，事件全被吞掉。这里在 trace 真正开始后再写，没有任何 MCP 活动时不写。
        """
        if self.recorder.started:
            return
        self.recorder.start(run_id=run_id, trace_id=uuid4().hex, query=query)
        if self.mcp_assembly.has_activity:
            self.recorder.record(
                node="mcp", event="mcp_assembly", data=self.mcp_assembly.trace_summary()
            )
        if self.skill_library.active:
            # P7-S9：只有名字、来源与错误码（不含正文、不含运维目录路径）；开关 off 时不写
            self.recorder.record(
                node="skills", event="skill_library", query="", data=self.skill_library.trace_summary()
            )

    async def aask(
        self,
        query: str,
        *,
        run_id: str | None = None,
        memory_context: MemoryRecallResult | None = None,
        upload_context: Any | None = None,
        attachment_context: Any | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[str, Any], None] | None = None,
        on_draft: Callable[[str, dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """异步作答（Web 与流式路径）。

        ``on_token`` 收本轮已过输出护栏的草稿文本；``on_draft`` 收草稿事件（delta/retract/
        closed），草稿可撤回，最终回答以返回值的 ``answer`` 为准（见 ``agent_rag.DraftPump``）。
        """
        effective_run_id = run_id or uuid4().hex
        self.start_trace(effective_run_id, query)

        def checked_token(token: str) -> None:
            if cancel_check is not None and cancel_check():
                raise RunCancelled()
            if on_token is not None:
                on_token(token)

        def checked_event(mode: str, payload: Any) -> None:
            if cancel_check is not None and cancel_check():
                raise RunCancelled()
            if on_event is not None:
                on_event(mode, payload)

        initial_messages = self._initial_messages(query)
        # 取消检查经 ContextVar 下传到模型调用与工具执行（runtime/cancellation.py）：此前只在
        # 事件回调里检查，内层工具循环取消后照样把剩下的模型/工具调用跑完。
        # 远程 MCP（Streamable HTTP）的 tools/call 按调用者主体做能力校验 / 限流：同样经 ContextVar 下传
        with cancellation_scope(cancel_check), mcp_request_scope(self.request_context):
            result = await astream_agentic(
                self.app,
                query,
                self.config,
                on_token=checked_token if on_token is not None else None,
                on_event=checked_event,
                on_draft=on_draft,
                messages=initial_messages,
                memory_context=memory_context,
                upload_context=upload_context,
                attachment_context=attachment_context,
                request_context=self.request_context,
            )
        if cancel_check is not None and cancel_check():
            raise RunCancelled()
        return self._turn_payload(result, query=query, streamed=on_token is not None)

    async def aresume(
        self,
        decision: dict[str, Any],
        *,
        run_id: str | None = None,
        memory_context: MemoryRecallResult | None = None,
        upload_context: Any | None = None,
        attachment_context: Any | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[str, Any], None] | None = None,
        on_draft: Callable[[str, dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """对挂起的确认请求给出决定，继续同一轮（P7-S7，Web 恢复路径）。

        ``decision`` 原样交给确认门（``{"confirmation_id", "approved", "reason"}``）；非法值不会让
        会话出错，而是带 ``error`` 再次挂起（返回值仍是 ``waiting_confirmation``）。事件、草稿与
        取消语义与 :meth:`aask` 相同。

        三个 ``*_context`` 是这一轮挂起前的召回结果（同进程恢复时由调用方原样交回），经
        ``ConfirmationRuntime.turn_contexts`` 交给 agent_loop，不写进状态。没交回而本轮原本有召回时
        （进程重启过），本轮剩下的部分不带它们作答，``warnings`` 与 trace 如实记一条
        ``turn_recall_context_unavailable``——降级必须可见。
        """
        if not self.confirmation_enabled:
            raise ValueError("本会话没有接人工确认门（confirmation=False），不能恢复确认")
        self.start_trace(run_id or uuid4().hex, "")
        self._resume_contexts = {
            key: value
            for key, value in (
                ("memory_context", memory_context),
                ("upload_context", upload_context),
                ("attachment_context", attachment_context),
            )
            if value is not None
        }

        def checked_token(token: str) -> None:
            if cancel_check is not None and cancel_check():
                raise RunCancelled()
            if on_token is not None:
                on_token(token)

        def checked_event(mode: str, payload: Any) -> None:
            if cancel_check is not None and cancel_check():
                raise RunCancelled()
            if on_event is not None:
                on_event(mode, payload)

        try:
            with cancellation_scope(cancel_check), mcp_request_scope(self.request_context):
                result = await astream_agentic(
                    self.app,
                    "",
                    self.config,
                    on_token=checked_token if on_token is not None else None,
                    on_event=checked_event,
                    on_draft=on_draft,
                    request_context=self.request_context,
                    resume=decision,
                )
        finally:
            self._resume_contexts = {}
        if cancel_check is not None and cancel_check():
            raise RunCancelled()
        return self._turn_payload(
            result, query=str(result.get("query") or ""), streamed=on_token is not None
        )

    def pending_confirmation(self) -> dict[str, Any] | None:
        """本 thread 挂起在确认门的请求（读 checkpoint 的 interrupts，不只看 ``next``）。

        没有接确认门时恒为 ``None``。读 checkpoint 出错时**上抛**：把挂起的会话报成"没挂起"，
        调用方会把 run 当成失败收口，而会话其实还停在确认门。
        """
        if not self.confirmation_enabled:
            return None
        snapshot = self.app.get_state(self.config)
        return pending_confirmation_request(getattr(snapshot, "interrupts", ()) or ())

    def _turn_payload(self, state: dict[str, Any], *, query: str, streamed: bool) -> dict[str, Any]:
        payload = self._payload(state, query=query, streamed=streamed)
        pending = self.pending_confirmation()
        if pending is not None:
            payload.update(
                status=STATUS_WAITING_CONFIRMATION,
                answer="",
                confirmation=public_confirmation_request(pending),
            )
        return payload

    def ask(self, query: str) -> dict[str, Any]:
        """同步作答（CLI）。``--stream`` 时逐句打印已过护栏的草稿。

        CLI 的会话 checkpointer 是同步 ``SqliteSaver``：LangGraph 拒绝经由它的 async 方法驱动
        图，所以流式也走同步 ``stream_agentic``（此前走 ``aask``，``--mode agentic --stream``
        接真实模型必然 ``NotImplementedError``）。被撤回的草稿在终端里如实标出，最终回答以
        payload 的 ``answer`` 为准。
        """
        # 先开 trace（与 aask 对齐）：此前同步路径由图的首个节点开启 trace，装配摘要无处可写
        self.start_trace(uuid4().hex, query)
        streamed = bool(getattr(self.args, "stream", False))
        if not streamed:
            # 显式清空两个召回段：CLI 不做召回，但同一 thread 可能被 Web 用过，
            # 不清空就会读到 checkpoint 里上一轮残留的值（见 astream_agentic 的说明）。
            with mcp_request_scope(self.request_context):
                state = self.app.invoke(
                    {
                        "messages": self._initial_messages(query),
                        "query": query,
                        "memory_context": None,
                        "upload_context": None,
                        "attachment_context": None,
                        **PER_TURN_RESET,
                    },
                    config=self.config,
                )
            return self._payload(state, query=query, streamed=False)

        def on_draft(kind: str, payload: dict[str, Any]) -> None:
            if kind == "draft_delta":
                print(payload.get("text", ""), end="", flush=True)
            elif kind == "draft_retract":
                print("\n[以上草稿已撤回]", flush=True)

        with mcp_request_scope(self.request_context):
            state = stream_agentic(
                self.app,
                query,
                self.config,
                on_draft=on_draft,
                messages=self._initial_messages(query),
                request_context=self.request_context,
            )
        print(flush=True)
        return self._payload(state, query=query, streamed=True)

    def _thread_values(self) -> dict[str, Any]:
        """本 thread 已持久化的状态；读不到（首轮、checkpoint 不可用）就当作空会话。"""
        try:
            snapshot = self.app.get_state(self.config)
        except Exception:  # noqa: BLE001 — 读不到历史只影响"变化才注入"等判断，不影响作答
            return {}
        values = getattr(snapshot, "values", None) or {}
        return dict(values)

    def _thread_messages(self) -> list[Any]:
        """本 thread 已持久化的消息；读不到（首轮、checkpoint 不可用）就当作空会话。"""
        return list(self._thread_values().get("messages", []) or [])

    def _initial_messages(self, query: str) -> list[Any]:
        return _initial_messages_with_runtime_context(
            query,
            self.projection,
            session_refs=[self.thread_id],
            history=self._thread_messages(),
        )

    def _payload(self, state: dict[str, Any], *, query: str, streamed: bool) -> dict[str, Any]:
        citations = [
            {
                "source_id": c.source_id,
                "spec_version": c.spec_version,
                "version_status": c.version_status,
                "declared_by": c.declared_by,
            }
            for c in state.get("citations", [])
        ]
        payload: dict[str, Any] = {
            "ok": True,
            "query": query,
            "answer": state.get("answer", ""),
            "status": state.get("status", ""),
            "citations": citations,
            "errors": state.get("errors", []),
            "warnings": state.get("warnings", []),
            "retry_count": state.get("retry_count", 0),
            "intent": "agentic",
            "streamed": streamed,
            "session_id": self.thread_id,
            "retriever_fallback": bool(getattr(self.retriever, "is_fallback", True)),
            "db_path": self.db_path,
        }
        runtime_status = getattr(self.retriever, "runtime_status", None)
        if isinstance(runtime_status, dict):
            payload["retriever_runtime_status"] = runtime_status
            payload["db_error"] = runtime_status.get("db_error")
        else:
            payload["db_error"] = getattr(self.retriever, "db_error", None)
        return payload


def build_agentic_session(
    args: argparse.Namespace,
    *,
    stack: ExitStack,
    thread_id: str | None = None,
    request_context: RequestContext | None = None,
    checkpointer: Any | None = None,
    mcp_lease: McpLease | None = None,
    confirmation: bool = False,
) -> AgenticSession:
    """Single assembly function used by CLI and Web adapters."""

    return AgenticSession(
        args,
        stack=stack,
        thread_id=thread_id,
        request_context=request_context,
        checkpointer=checkpointer,
        mcp_lease=mcp_lease,
        confirmation=confirmation,
    )


__all__ = [
    "AgenticSession",
    "RunCancelled",
    "_ScriptedAgenticModel",
    "_build_cache_service",
    "_build_chat_checkpointer",
    "_build_reranker",
    "_build_retriever",
    "_initial_messages_with_runtime_context",
    "build_agentic_session",
    "build_cli_checkpointer",
    "chat_model_problem",
    "embedding_configured",
    "embedding_required",
    "real_llm_configured",
]

# Compatibility alias retained for CLI's existing private import surface.
_build_chat_checkpointer = build_cli_checkpointer

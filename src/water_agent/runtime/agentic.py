"""Shared AgenticSession assembly for CLI and Web runs.

The Web entry point is deliberately a thin adapter around this module.  Model,
retriever, MCP, ToolRegistry, ProviderGateway and GuardrailPipeline assembly is
kept here so the CLI and Web cannot silently grow different agent runtimes.
"""

from __future__ import annotations

import argparse
import asyncio
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
from water_agent.generation.chat_model import DEFAULT_API_KEY_ENV, build_chat_model
from water_agent.graphs.agent_rag import PER_TURN_RESET, astream_agentic, build_agentic_app
from water_agent.ingestion.version_registry import get_version_registry
from water_agent.mcp.wiring import (
    load_mcp_gateway,
    open_gateway_sessions,
    principal_with_mcp,
    register_mcp_tools,
)
from water_agent.memory.recall import MemoryRecallResult
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.prompting import PromptAssembler
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
from water_agent.runtime.governance.binding import (
    default_gateway_with_sink,
    local_request_context,
)
from water_agent.runtime.governance.cache_service import AnswerCacheService
from water_agent.runtime.governance.contracts import Principal, RequestContext
from water_agent.session import default_checkpoint_serde, session_db_path
from water_agent.tools.calc_velocity import calc_velocity_spec
from water_agent.tools.ds_adapters import tool_spec_to_base_tool
from water_agent.tools.material_stats import material_stats_spec
from water_agent.tools.read_drawing import maybe_build_read_drawing_tool
from water_agent.tools.registry import ToolRegistry
from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

# This module is one level deeper than water_agent/cli.py, so the repository
# root is parents[3] here, not parents[2].
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURE = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
# 索引目录与选择规则归 retrieval.trivium 统一维护（CLI/Web/harness 共用一套）；
# 这里保留别名，历史调用方与测试仍按 agentic.DEFAULT_INDEX_DIR 引用。
DEFAULT_INDEX_DIR = TRIVIUM_INDEX_DIR


def real_llm_configured(environment: Callable[[str], str | None] = os.getenv) -> bool:
    """Inspect only whether the configured real-model key exists."""

    from water_agent.config.credentials import resolve_secret
    from water_agent.generation.local_model import local_model_configured, local_model_ready

    if local_model_configured(environment):
        return local_model_ready(environment)
    # 与 build_chat_model 共用同一解析顺序（env → _FILE → 凭据卷）。两处各自
    # 判断时曾出现「这里认 _FILE、那里只认 env」，配了文件便得到 424 放行、
    # 真跑 503 的矛盾结果。
    return resolve_secret("DEEPSEEK_API_KEY", getenv=environment) is not None


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

    ``embedding.build_embedding_provider`` 直读 ``os.environ``，靠 webapp 启动时的
    ``load_credentials_into_environ()`` 把 ``_FILE``/凭据卷回填进来；这里改用
    ``resolve_secret`` 是为了让预检与实际构造**在回填之前也一致**，否则会重演
    chat model 那个「预检放行、真跑报错」的老问题。
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


def _build_cache_service(args: argparse.Namespace) -> AnswerCacheService:
    reranker = None if getattr(args, "rerank", "local") == "none" else args.rerank
    return AnswerCacheService(
        embedding_model="qwen3.7-text-embedding",
        dimensions=getattr(args, "dimensions", None) or 512,
        top_k=getattr(args, "top_k", 5),
        recall_top_k=getattr(args, "recall_top_k", 20),
        reranker=reranker,
        version_registry=get_version_registry(),
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
                and not message.content.startswith("[运行时上下文]")
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


def _initial_messages_with_runtime_context(
    query: str,
    projection: RuntimeContextProjection,
    *,
    session_refs: list[str] | None = None,
) -> list[HumanMessage]:
    context = default_context_builder(session_refs=session_refs)
    context_text = projection.project(context)
    messages: list[HumanMessage] = [HumanMessage(content=query)]
    if context_text:
        messages.insert(0, HumanMessage(content=f"[运行时上下文]\n{context_text}"))
    return messages


class RunCancelled(RuntimeError):
    """Raised by cooperative Agentic streaming when a Web run is cancelled."""


class AgenticSession:
    """One thread, one governed registry and one checkpoint-backed Agentic app."""

    #: RunManager 只给声明支持的会话传 ``attachment_context``（AgentSessionFactory 是协议）。
    supports_attachment_context = True

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        stack: ExitStack,
        thread_id: str | None = None,
        request_context: RequestContext | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.args = args
        self.stack = stack
        self.thread_id = thread_id or uuid4().hex
        self.request_context = request_context or local_request_context(
            thread_id=self.thread_id, request_id=f"req-{self.thread_id[:12]}"
        )
        self.recorder = JsonTraceRecorder(Path(args.trace_dir))
        self.gateway = default_gateway_with_sink(self.recorder.record_provider_event)
        api_key_env = getattr(args, "api_key_env", DEFAULT_API_KEY_ENV)
        api_key = getattr(args, "api_key", None)
        from water_agent.generation.local_model import local_model_configured, local_model_ready

        if local_model_configured() and not local_model_ready():
            raise ValueError(
                "local model is enabled but lacks complete compatibility evidence; "
                "run the Step 46 compatibility probe before starting an Agent session"
            )
        if args.mock_generator:
            self.model: BaseChatModel = _ScriptedAgenticModel()
        else:
            try:
                self.model = build_chat_model(
                    model=args.model,
                    base_url=args.base_url,
                    api_key_env=api_key_env,
                    api_key=api_key,
                    gateway=self.gateway,
                    request_context_factory=lambda: self.request_context,
                )
            except ValueError as exc:
                raise ValueError(
                    f"{exc}。agentic 真实模型链路需要 API Key：export {api_key_env}=sk-...，"
                    "或用 --mock-generator 走离线占位链路（不连真实模型，不要求 Key）"
                ) from exc

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

        base_principal = self.request_context.principal
        mcp_config = load_mcp_gateway()
        mcp_assembly = open_gateway_sessions(mcp_config)
        self.mcp_assembly = mcp_assembly
        stack.callback(mcp_assembly.close)
        capabilities = (
            principal_with_mcp(base_principal.capabilities)
            if mcp_assembly.exposed_tool_names
            else base_principal.capabilities
        )
        principal = Principal(
            kind=base_principal.kind,
            subject=base_principal.subject,
            capabilities=capabilities,
            auth_domain=base_principal.auth_domain,
        )
        self.request_context = self.request_context.model_copy(update={"principal": principal})

        registry = ToolRegistry(principal=principal, trace=self.recorder)

        # 启用引用可追溯性检查（Step 8，2026-09-16）
        from water_agent.tools.cite import build_cite_clauses_tool_with_context_check
        cite_tool_spec, context_updater = build_cite_clauses_tool_with_context_check()

        registry.register(build_retrieve_clauses_tool(tool_retriever, context_updater=context_updater))
        registry.register(cite_tool_spec)
        registry.register(calc_velocity_spec)
        registry.register(material_stats_spec)
        drawing_tool, self.drawing_root_status = maybe_build_read_drawing_tool(
            getattr(args, "drawing_root", None)
        )
        if drawing_tool is not None:
            registry.register(drawing_tool)
        self.mcp_tool_names = register_mcp_tools(mcp_assembly, registry)
        tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]
        self.registry = registry
        self.projection = RuntimeContextProjection()
        self.config = {"configurable": {"thread_id": self.thread_id}}
        saver = checkpointer if checkpointer is not None else build_cli_checkpointer(args.session_dir, stack)
        self.app = build_agentic_app(
            model=self.model,
            tools=tools,
            checkpointer=saver,
            prompt=PromptAssembler(tool_registry=registry),
            trace=self.recorder,
            request_context=self.request_context,
            cache_service=_build_cache_service(args) if not args.mock_generator else None,
        )

    def start_trace(self, run_id: str, query: str) -> None:
        if not self.recorder.started:
            self.recorder.start(run_id=run_id, trace_id=uuid4().hex, query=query)

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
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
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

        initial_messages = _initial_messages_with_runtime_context(
            query, self.projection, session_refs=[self.thread_id]
        )
        result = await astream_agentic(
            self.app,
            query,
            self.config,
            on_token=checked_token,
            on_event=checked_event,
            messages=initial_messages,
            memory_context=memory_context,
            upload_context=upload_context,
            attachment_context=attachment_context,
        )
        if cancel_check is not None and cancel_check():
            raise RunCancelled()
        return self._payload(result, query=query, streamed=on_token is not None)

    def ask(self, query: str) -> dict[str, Any]:
        streamed = bool(getattr(self.args, "stream", False)) and not self.args.mock_generator
        if not streamed:
            # The CLI keeps the synchronous graph invocation it has always used:
            # the session checkpointer is a sync SqliteSaver, which LangGraph
            # refuses to drive through its async methods.  Only the streaming
            # and Web paths need ``aask``.
            # 显式清空两个召回段：CLI 不做召回，但同一 thread 可能被 Web 用过，
            # 不清空就会读到 checkpoint 里上一轮残留的值（见 astream_agentic 的说明）。
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
        payload = asyncio.run(
            self.aask(query, on_token=lambda token: print(token, end="", flush=True))
        )
        print(flush=True)
        return payload

    def _initial_messages(self, query: str) -> list[Any]:
        return _initial_messages_with_runtime_context(
            query, self.projection, session_refs=[self.thread_id]
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
) -> AgenticSession:
    """Single assembly function used by CLI and Web adapters."""

    return AgenticSession(
        args,
        stack=stack,
        thread_id=thread_id,
        request_context=request_context,
        checkpointer=checkpointer,
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
    "embedding_configured",
    "embedding_required",
    "real_llm_configured",
]

# Compatibility alias retained for CLI's existing private import surface.
_build_chat_checkpointer = build_cli_checkpointer

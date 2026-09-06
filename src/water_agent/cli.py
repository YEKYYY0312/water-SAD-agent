"""生产问答 CLI + 交互式问答循环。

生产链路：TriviumDB 混合检索召回 → LocalReranker 重排 → top_k=5 → LLM 生成。
默认真实 LLM（DeepSeek 官方 Claude 兼容）；--mock-generator 可用确定性生成器自测链路。
--mode agentic：Step 6 起模型驱动的 Agentic RAG 外层循环；Step 7 接真实 DeepSeek
function calling（无 Key 属结构化错误而非静默降级），--stream 可流式打印 token。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from water_agent.domain.citation import CitationClaim
from water_agent.domain.state import default_state
from water_agent.embedding import build_embedding_provider
from water_agent.generation.base import Generator
from water_agent.generation.chat_model import DEFAULT_API_KEY_ENV, build_chat_model
from water_agent.generation.llm import DEFAULT_SYSTEM_PROMPT, build_llm_generator
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.agent_rag import astream_agentic, build_agentic_app
from water_agent.graphs.calc import build_calc_graph, detect_intent
from water_agent.graphs.rag import build_rag_graph
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.prompting import PromptAssembler
from water_agent.prompting.runtime_context import (
    RuntimeContextProjection,
    default_context_builder,
)
from water_agent.retrieval.base import Retriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.rerank import (
    BailianReranker,
    LocalReranker,
    RerankingRetriever,
    build_bailian_reranker,
)
from water_agent.retrieval.trivium import TriviumRetrievalError, TriviumRetriever
from water_agent.session import (
    DEFAULT_SESSION_DIR,
    new_session_id,
    save_session_meta,
    session_db_path,
)
from water_agent.tools.calc_velocity import calc_velocity_spec
from water_agent.tools.cite import build_cite_clauses_tool
from water_agent.tools.ds_adapters import tool_spec_to_base_tool
from water_agent.tools.material_stats import material_stats_spec
from water_agent.tools.registry import ToolRegistry
from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
DEFAULT_TRACE_DIR = REPO_ROOT / "data" / "traces"
DEFAULT_INDEX_DIR = REPO_ROOT / "data" / "indexes"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="water_agent 生产问答 CLI")
    p.add_argument("query", nargs="?", default="", help="给排水问题；留空配合 --interactive 进入问答循环")
    p.add_argument("--db-path", default=None, help="TriviumDB 索引路径（默认用 data/indexes 下最新 512/256 库）")
    p.add_argument("--dimensions", type=int, default=None, choices=[256, 512, 768, 1024], help="embedding 维度（对齐 db 库维度）")
    p.add_argument("--rerank", choices=["local", "bailian", "none"], default="local", help="rerank 策略")
    p.add_argument("--rerank-model", default=None, help="百炼 rerank 模型名（默认 gte-rerank）")
    p.add_argument("--rerank-base-url", default=None, help="百炼 rerank base_url（默认原生 DashScope text-rerank 端点）")
    p.add_argument("--rerank-api-key", default=None, help="百炼 rerank API Key（默认读 DASHSCOPE_API_KEY）")
    p.add_argument("--top-k", type=int, default=5, help="rerank 后返回条数")
    p.add_argument("--recall-top-k", type=int, default=20, help="混合检索召回的候选条数")
    p.add_argument("--hybrid-alpha", type=float, default=0.7)
    p.add_argument("--model", default="deepseek-chat", help="LLM 模型名")
    p.add_argument("--base-url", default="https://api.deepseek.com", help="LLM OpenAI 兼容 base_url")
    p.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help="真实模型 API Key 所在环境变量名（--mode agentic 用，默认 DEEPSEEK_API_KEY）",
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="--mode agentic 真实模型时流式打印 token（stream_mode=messages；配合 --json 时结构信息进 stderr）",
    )
    p.add_argument("--mock-generator", action="store_true", help="用确定性 MockGenerator 代替真实 LLM（自测链路用）")
    p.add_argument("--interactive", action="store_true", help="进入交互式多轮会话（输入 exit 退出）")
    p.add_argument("--resume", default=None, help="用现有 thread_id 恢复历史会话")
    p.add_argument(
        "--session-dir",
        default=str(DEFAULT_SESSION_DIR),
        help="会话库目录（默认 data/sessions，单库 sessions.sqlite）",
    )
    p.add_argument(
        "--mode",
        choices=["auto", "calc", "legacy", "agentic"],
        default="auto",
        help="意图路由：auto 自动识别（计算意图走 calc 子图，其余走 legacy RAG）；"
        "calc 强制走计算子图；legacy 强制走旧 RAG 路径（默认 auto，legacy 行为不变）；"
        "agentic 走模型驱动的 Agentic RAG 外层循环（Step 6 特性开关，默认关闭）",
    )
    p.add_argument(
        "--flow",
        choices=["qa", "docgen"],
        default="qa",
        help="qa 问答（默认）；docgen 计算书生成：gather→draft→人审(interrupt)→finalize，--resume 恢复挂起审批",
    )
    p.add_argument(
        "--doc-type",
        choices=["auto", "calculation_book", "design_note"],
        default="auto",
        help="docgen 文档类型：auto（默认，计算意图→计算书，其余→设计说明）/ calculation_book 计算书 / design_note 设计说明",
    )
    p.add_argument("--trace-dir", default=str(DEFAULT_TRACE_DIR), help="Trace 输出目录")
    p.add_argument("--json", action="store_true", help="以 JSON 输出（单次模式）")
    return p


def _embedding_for(db_path: Path) -> tuple[object, int]:
    """按 db-path 推断维度并构建真实 embedding provider。"""
    dim = None
    if db_path.exists():
        try:
            import triviumdb

            probe = triviumdb.TriviumDB(str(db_path))
            dim = int(probe.dim())
            probe.close()
        except Exception:  # noqa: BLE001 — triviumdb 打开失败时降级到默认维度
            dim = None
    if dim is None:
        dim = 512
    provider = build_embedding_provider(dimensions=dim)
    return provider, dim


def _resolve_db_path(args: argparse.Namespace) -> Path | None:
    """解析真实索引路径：显式 --db-path 必须存在（不存在响亮失败）；mock 自测不自动加载真实库。"""
    if args.db_path:
        if not Path(args.db_path).exists():
            # 用户显式指定的库不存在：响亮失败，不得静默回落 fixture
            raise ValueError(f"--db-path 不存在：{args.db_path}（请检查路径，或去掉 --db-path 使用 data/indexes 下的默认库）")
        return Path(args.db_path)
    if args.mock_generator:
        # mock 自测不自动加载真实库：mock_embed 与真实嵌入空间不匹配，直接回落 fixture
        return None
    candidates = sorted(DEFAULT_INDEX_DIR.glob("*.db")) if DEFAULT_INDEX_DIR.exists() else []
    return candidates[-1] if candidates else None


def _build_retriever(args: argparse.Namespace) -> tuple[Retriever, str]:
    """装配检索器（不含 rerank）。返回 (retriever, db_path_or_empty)。

    legacy 图与 agentic 工具共用：真实库需真实 embedding（DASHSCOPE_API_KEY），
    ``--mock-generator`` 时用 mock_embed 或直接 fixture 回落，不要求任何 Key。
    """
    fixture = load_clauses_jsonl(DEFAULT_FIXTURE)
    db_path = _resolve_db_path(args)
    if db_path is None:
        retriever = TriviumRetriever(db_path=None, fallback_clauses=fixture)
        return retriever, ""

    dim = args.dimensions
    if args.mock_generator:
        # mock 自测：用确定性 mock_embed（与库同维），不建真实 provider、不要求 API Key
        import triviumdb

        probe = triviumdb.TriviumDB(str(db_path))
        dim = dim or int(probe.dim())
        probe.close()
        from water_agent.retrieval.trivium import mock_embed

        embedding_fn = lambda text: mock_embed(text, dim=dim)
        fallback_kind = "mock"
        fallback_reason = "explicit mock_generator"
    else:
        # 真实库需要真实 embedding 做 query 向量（维度须与库一致）
        provider, detected = _embedding_for(db_path)
        if args.dimensions is None:
            dim = detected
        embedding_fn = provider.embed  # type: ignore[attr-defined]
        fallback_kind = "none"
        fallback_reason = "real db"
    retriever = TriviumRetriever(
        db_path=db_path,
        embedding_fn=embedding_fn,
        hybrid_alpha=args.hybrid_alpha,
        dim=dim,
        fallback_clauses=fixture,
    )
    retriever._fallback_kind = fallback_kind  # type: ignore[attr-defined]
    retriever._fallback_reason = fallback_reason  # type: ignore[attr-defined]
    return retriever, str(db_path)


def _build_reranker(args: argparse.Namespace):
    if args.rerank == "local":
        return LocalReranker()
    if args.rerank == "bailian":
        return build_bailian_reranker(
            api_key=args.rerank_api_key,
            model=args.rerank_model or BailianReranker.DEFAULT_MODEL,
            base_url=args.rerank_base_url or BailianReranker.DEFAULT_BASE_URL,
        )
    return None


def build_app(*, args: argparse.Namespace, trace: JsonTraceRecorder | None = None) -> tuple[object, Retriever, str]:
    """装配 检索(含 rerank) + 生成 + legacy RAG 图。返回 (app, retriever, db_path_or_empty).

    ``trace`` 传入图内 recorder，使检索/生成/引用校验等事件可被记录。
    """
    retriever, db_path = _build_retriever(args)

    generator: Generator
    if args.mock_generator:
        generator = MockGenerator()
    else:
        # Step 8：legacy 真实模型路径的系统提示也换用装配器（行为兼容：核心规则一致，
        # 额外注入版本注册表快照；--mock-generator 走 MockGenerator，不受影响）。
        assembler = PromptAssembler()
        sys_content = assembler.assemble_prompt().content
        system_prompt = sys_content if isinstance(sys_content, str) else DEFAULT_SYSTEM_PROMPT
        generator = build_llm_generator(
            model=args.model,
            base_url=args.base_url,
            system_prompt=system_prompt,
        )

    app = build_rag_graph(
        retriever,
        generator,
        trace=trace,
        reranker=_build_reranker(args),
        top_k=args.top_k,
        recall_top_k=args.recall_top_k,
    )
    return app, retriever, db_path


def run_calc_once(query: str) -> dict:
    """走水力计算确定性子图（参数解析用规则替身，数值计算用真工具）。

    计算意图的确定性入口；真实 LLM 参数理解在 Step 7 接入。
    """
    app = build_calc_graph()
    state: dict = {
        "query": query.strip(),
        "intent": "calc",
        "formula": None,
        "params": {},
        "calc_result": None,
        "missing_inputs": [],
        "assumptions": [],
        "errors": [],
        "candidates": [],
        "answer": "",
        "status": "running",
        "step_count": 0,
    }
    result = app.invoke(state)
    calc_result = result.get("calc_result")
    return {
        "query": query,
        "answer": result.get("answer", ""),
        "status": result.get("status", ""),
        "intent": "calc",
        "formula": result.get("formula"),
        "params": result.get("params", {}),
        "missing_inputs": result.get("missing_inputs", []),
        "assumptions": result.get("assumptions", []),
        "errors": result.get("errors", []),
        "candidates": result.get("candidates", []),
        "result_values": dict(calc_result.values) if calc_result is not None else {},
        "spec_record": "公式口径：docs/design/09-hydraulics-formulas.md（ADR-01：公式显式指定）",
    }


class _ScriptedAgenticModel(BaseChatModel):
    """离线占位模型：第一轮 retrieve_clauses，第二轮 cite_clauses 声明首条命中，第三轮作答。

    查询从消息历史里最近一条非"运行时上下文"的 HumanMessage 取，因此同一实例可跨轮复用
    （交互式多轮会话）。未接真实 DeepSeek function calling 时用此 fake 顶替。
    """

    @property
    def _llm_type(self) -> str:
        return "scripted-agentic"

    @staticmethod
    def _latest_query(messages) -> str:
        for m in reversed(messages):
            if isinstance(m, HumanMessage) and isinstance(m.content, str) and not m.content.startswith("[运行时上下文]"):
                return m.content
        return ""

    @staticmethod
    def _latest_hits(messages) -> list[dict]:
        for m in reversed(messages):
            if isinstance(m, ToolMessage) and m.name == "retrieve_clauses":
                payload = json.loads(m.content) if isinstance(m.content, str) else {}
                return payload.get("hits", []) or []
        return []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        query = self._latest_query(messages)
        # 本轮（最近一条 HumanMessage 之后）已有的工具结果
        since_human = []
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                break
            since_human.append(m)
        tool_names = {m.name for m in since_human if isinstance(m, ToolMessage)}

        if "retrieve_clauses" not in tool_names:
            call = {"name": "retrieve_clauses", "args": {"query": query, "top_k": 5}, "id": f"c-retrieve-{uuid4().hex[:6]}"}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])

        hits = self._latest_hits(messages)
        if hits and "cite_clauses" not in tool_names:
            call = {"name": "cite_clauses", "args": {"article_ids": [hits[0].get("source_id", "")]}, "id": f"c-cite-{uuid4().hex[:6]}"}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])

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
    """按需注入运行时上下文（Step 8）：上下文变化才作为一条用户消息注入。

    首次调用必然注入一次（cwd + 规范版本快照 + 会话引用）；同一 projection 对象在
    上下文未变时 ``project()`` 返回 None，不再重复注入（省 token）。
    """
    context = default_context_builder(session_refs=session_refs)
    context_text = projection.project(context)
    messages: list[HumanMessage] = [HumanMessage(content=query)]
    if context_text:
        messages.insert(0, HumanMessage(content=f"[运行时上下文]\n{context_text}"))
    return messages


class AgenticSession:
    """Agentic RAG 会话：一个 thread_id + 一套 app/工具/投影，可连续 ``ask`` 多轮。

    - ``--mock-generator``：脚本化占位模型 + mock 检索装配（fixture 回落），不要求任何 Key；
    - 默认：真实 DeepSeek function calling（``build_chat_model``）+ 真实检索装配。
      无 Key 抛带指引的结构化错误，绝不静默降级为假模型；
    - 检索工具经 ``RerankingRetriever`` 套 ``--rerank``，与 legacy 图口径一致；
    - 运行时上下文投影跨轮持有：同一会话内上下文不变就不再注入。
    """

    def __init__(self, args: argparse.Namespace, *, stack: ExitStack, thread_id: str | None = None) -> None:
        self.args = args
        api_key_env = getattr(args, "api_key_env", DEFAULT_API_KEY_ENV)
        self.model: BaseChatModel
        if args.mock_generator:
            self.model = _ScriptedAgenticModel()
        else:
            try:
                self.model = build_chat_model(model=args.model, base_url=args.base_url, api_key_env=api_key_env)
            except ValueError as exc:
                raise ValueError(
                    f"{exc}。agentic 真实模型链路需要 API Key：export {api_key_env}=sk-...，"
                    "或用 --mock-generator 走离线占位链路（不连真实模型，不要求 Key）"
                ) from exc
        try:
            retriever, self.db_path = _build_retriever(args)
        except ValueError as exc:
            raise ValueError(
                f"{exc}。真实检索装配失败：需配置 DASHSCOPE_API_KEY（embedding）且索引可用；"
                "或用 --mock-generator 走离线占位链路（fixture 回落，不要求 Key）"
            ) from exc
        self.retriever = retriever
        reranker = _build_reranker(args)
        tool_retriever: Retriever = (
            RerankingRetriever(retriever, reranker, recall_top_k=args.recall_top_k) if reranker is not None else retriever
        )

        registry = ToolRegistry()
        registry.register(build_retrieve_clauses_tool(tool_retriever))
        registry.register(build_cite_clauses_tool())
        registry.register(calc_velocity_spec)
        registry.register(material_stats_spec)
        tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]

        # Step 8：动态系统提示装配（角色/版本快照/公式口径/工具指南）+ 运行时上下文投影
        self.projection = RuntimeContextProjection()
        self.thread_id = thread_id or uuid4().hex
        self.config = {"configurable": {"thread_id": self.thread_id}}
        saver = _build_chat_checkpointer(args.session_dir, stack)
        self.app = build_agentic_app(
            model=self.model,
            tools=tools,
            checkpointer=saver,
            prompt=PromptAssembler(tool_registry=registry),  # callable：装配器按 state 动态生成 SystemMessage
        )

    def ask(self, query: str) -> dict:
        stream = bool(getattr(self.args, "stream", False)) and not self.args.mock_generator
        initial_messages = _initial_messages_with_runtime_context(
            query, self.projection, session_refs=[self.thread_id]
        )
        if stream:
            state = asyncio.run(
                astream_agentic(
                    self.app,
                    query,
                    self.config,
                    on_token=lambda tok: print(tok, end="", flush=True),
                    messages=initial_messages,
                )
            )
            print(flush=True)
        else:
            state = self.app.invoke({"messages": initial_messages, "query": query}, config=self.config)

        citations = [
            {
                "source_id": c.source_id,
                "spec_version": c.spec_version,
                "version_status": c.version_status,
                "declared_by": c.declared_by,
            }
            for c in state.get("citations", [])
        ]
        payload = {
            "ok": True,
            "query": query,
            "answer": state.get("answer", ""),
            "status": state.get("status", ""),
            "citations": citations,
            "errors": state.get("errors", []),
            "retry_count": state.get("retry_count", 0),
            "intent": "agentic",
            "streamed": stream,
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


def run_agentic_once(query: str, *, args: argparse.Namespace) -> dict:
    """单次 agentic 问答（新 thread）：见 ``AgenticSession``。"""
    with ExitStack() as stack:
        return AgenticSession(args, stack=stack).ask(query)

def run_once(query: str, *, args: argparse.Namespace) -> dict:
    mode = getattr(args, "mode", "auto")
    if mode == "agentic":
        return run_agentic_once(query, args=args)
    if mode == "calc" or (mode == "auto" and detect_intent(query) == "calc"):
        return run_calc_once(query)
    recorder = JsonTraceRecorder(Path(args.trace_dir))
    app, retriever, db_path = build_app(args=args, trace=recorder)
    run_id = uuid4().hex
    trace_id = uuid4().hex
    state = default_state(query=query, run_id=run_id, trace_id=trace_id)
    try:
        result: dict = app.invoke(state)  # type: ignore[attr-defined]
    except TriviumRetrievalError as exc:
        recorder.finish("failed", error=str(exc), query=query)
        raise RuntimeError(f"real db retrieval failed: {exc}") from exc
    citations = [c.model_dump(mode="json") | {"source_id": c.source_id} for c in result.get("citations", [])]
    context = [c.model_dump(mode="json") | {"source_id": c.source_id} for c in result.get("context", [])]
    final_trace_id = result.get("trace_id", trace_id)
    payload = {
        "query": query,
        "answer": result.get("answer", ""),
        "status": result.get("status", ""),
        "citations": citations,
        "context": context,
        "errors": result.get("errors", []),
        "run_id": result.get("run_id", run_id),
        "trace_id": final_trace_id,
        "trace_path": str(Path(args.trace_dir) / f"{final_trace_id}.json"),
        "retriever_fallback": bool(getattr(retriever, "is_fallback", True)),
        "retriever_db_error": getattr(retriever, "db_error", None),
        "db_path": db_path,
        "retry_count": result.get("retry_count", 0),
        "step_count": result.get("step_count", 0),
    }
    runtime_status = getattr(retriever, "runtime_status", None)
    if isinstance(runtime_status, dict):
        payload["retriever_runtime_status"] = runtime_status
        payload["db_error"] = runtime_status.get("db_error")
        payload["retriever_fallback_kind"] = runtime_status.get("fallback_kind")
        payload["retriever_fallback_reason"] = runtime_status.get("fallback_reason")
    return payload


def _print_calc_result(payload: dict) -> None:
    print(payload["answer"])
    print(f"\n状态: {payload['status']}")
    if payload["status"] == "missing_inputs":
        print(f"待补充参数: {payload['missing_inputs']}")
    if payload.get("assumptions"):
        print(f"假设/异常: {'；'.join(payload['assumptions'])}")
    if payload.get("candidates"):
        print(f"候选方案: {'；'.join(payload['candidates'])}")
    if payload.get("spec_record"):
        print(payload["spec_record"])



def _print_result(payload: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if payload.get("intent") == "calc":
        _print_calc_result(payload)
        return
    print(payload["answer"])
    if payload["citations"]:
        print("\n引用：")
        for c in payload["citations"]:
            print(f"  - {c['source_id']}  {c.get('title','')}")
    else:
        print("\n引用：无")
    print(f"\n状态: {payload['status']}")
    if payload.get("retriever_runtime_status"):
        status = payload["retriever_runtime_status"]
        print(f"检索运行态: kind={status.get('fallback_kind')} reason={status.get('fallback_reason')} db_error={status.get('db_error')}")
    elif payload["retriever_fallback"]:
        print("检索: fixture 回落（未使用真实 TriviumDB 索引）")
    if payload["errors"]:
        print("错误:", "; ".join(payload["errors"]))
    if payload.get("db_error"):
        print(f"db_error: {payload['db_error']}")
    if payload.get("db_path"):
        print(f"索引: {payload['db_path']}")


def _build_chat_checkpointer(session_dir: str | Path, stack: ExitStack):
    """优先 SqliteSaver 做磁盘持久化；不可用时降级 MemorySaver 并显式提示缺口。

    ``stack`` 用于在会话结束时关闭底层 sqlite 连接（SqliteSaver 自身无 close()）。
    """
    db_path = session_db_path(session_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        from water_agent.session import default_checkpoint_serde

        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        stack.callback(conn.close)
        # 注册 CitationClaim：否则 checkpoint 反序列化被阻止（未来版本报错），--resume 恢复坏引用状态
        return SqliteSaver(conn, serde=default_checkpoint_serde())
    except Exception as exc:  # noqa: BLE001 — 持久化不可用不阻塞本步，但必须记录缺口
        print(
            json.dumps(
                {"ok": False, "error": f"sqlite checkpointer 不可用，本轮改用 MemorySaver（进程内态，不落盘）：{exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
def _run_chat_session(args: argparse.Namespace, query: str = "") -> int:
    """交互式多轮会话：每行输入按 ``--mode`` 真正作答（不再是占位回声）。

    - ``--mode agentic``：同一 ``AgenticSession``（thread_id = 会话 ID）跨轮复用，
      checkpointer 持久化消息历史，``--resume`` 可恢复；
    - 其他模式：每行走 ``run_once``（calc 子图 / legacy RAG），无跨轮记忆。
    """
    session_id = args.resume or new_session_id()
    session_dir = Path(args.session_dir)
    if not args.resume:
        save_session_meta(session_id, session_dir=session_dir, query=(query or "interactive"))
    print(f"会话 ID: {session_id}（模式 {args.mode}）")
    with ExitStack() as stack:
        session: AgenticSession | None = None
        if args.mode == "agentic":
            try:
                session = AgenticSession(args, stack=stack, thread_id=session_id)
            except Exception as exc:  # noqa: BLE001 — 装配失败给结构化提示后退出
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
                return 2
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line.lower() in {"exit", "quit"}:
                break
            try:
                payload = session.ask(line) if session is not None else run_once(line, args=args)
            except Exception as exc:  # noqa: BLE001 — 交互循环保持健壮，任一问题打印后继续
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
                continue
            if args.json:
                print(json.dumps({"session_id": session_id, **payload}, ensure_ascii=False))
                continue
            if payload.get("streamed"):
                print(f"状态: {payload.get('status', '')}")
                continue
            _print_result(payload, json_output=False)
    return 0


def _docgen_rag_args(args: argparse.Namespace) -> argparse.Namespace:
    """docgen 设计依据检索的图选择：auto/calc → legacy（不回路由 calc 子图）；
    agentic → agentic（模型 cite_clauses 声明引用，校验器真判）；legacy → legacy。"""
    mode = args.mode if args.mode in {"agentic", "legacy"} else "legacy"
    return argparse.Namespace(**{**vars(args), "mode": mode})


def _citation_claims_from_rag(rag: dict[str, Any]) -> list[CitationClaim]:
    """把 RAG 问答输出的 citations 归一为 state 层 CitationClaim 列表。"""
    return [
        CitationClaim(
            source_id=c["source_id"],
            spec_version=c.get("spec_version"),
            version_status=c.get("version_status"),
            declared_by=c.get("declared_by", "system"),
        )
        for c in rag.get("citations", [])
    ]


def _gather_docgen_inputs(query: str, args: argparse.Namespace) -> dict:
    """docgen 的 gather：一次 RAG（取引用+完整检索上下文）+ 一次计算（calc 意图）。

    P2 收口（见 docs/design/11-docgen-materials.md §2.1）：
    - 计算结果富化：**数值入参与结果同表**（params 数值键并进 calc_result，模板只渲染
      数值/备注两路，不运算）；公式口径/spec_record/缺参/错误/假设按备注行透出；
    - 设计依据：calc 分支不再无条件返回空引用——对原查询跑一次 legacy/agentic RAG（按 args.mode
      选择图，auto/calc 走 legacy，agentic 保持 agentic），命中的 source_id 汇成 citations；
      0 命中时为空，模板显式写「未声明规范依据」（不编造）；
    - 返回结构显式包含：``citations``（校验后声明）、``calc_result``（计算产物，calc 意图）、
      ``context``（检索命中的完整条款，供审批/渲染直接消费）、``rag_answer`` / ``rag_status``
      （RAG 回答与状态）。**只执行一次 RAG**，下游不再重复检索。
    """
    rag = run_once(query, args=_docgen_rag_args(args))
    gather: dict[str, Any] = {
        "citations": _citation_claims_from_rag(rag),
        "calc_result": None,
        "context": list(rag.get("context", [])),
        "rag_answer": rag.get("answer", ""),
        "rag_status": rag.get("status", ""),
    }
    if detect_intent(query) == "calc":
        calc = run_calc_once(query)
        calc_result = dict(calc.get("result_values") or {})
        # 数值入参（DN150→150、Q=20L/s→20…）与输出同表：计算书必须能看见输入口径
        for key, value in (calc.get("params") or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                calc_result.setdefault(key, value)
        # 缺参/非法信号透出给人审：审批人需要知道为何没有数值、缺的是什么
        if calc.get("missing_inputs"):
            calc_result["missing_inputs"] = "；".join(calc["missing_inputs"])
        if calc.get("errors"):
            calc_result["errors"] = "；".join(calc["errors"])
        if calc.get("assumptions"):
            calc_result["assumptions"] = "；".join(calc["assumptions"])
        if calc.get("formula"):
            calc_result["formula"] = calc["formula"]
        if calc.get("spec_record"):
            calc_result["spec_record"] = calc["spec_record"]
        gather["calc_result"] = calc_result or None
    return gather


def _read_cli_input(prompt: str, ui) -> str:
    """读一行人工输入：提示文本输出到 ``ui``（--json 时 = stderr），stdin 只读值。

    不能依赖 ``input(prompt)``——它的提示会写到 stdout，污染 ``--json`` 的纯 JSON 输出。
    """
    print(prompt, end="", file=ui, flush=True)
    return input()


def run_docgen_flow(args: argparse.Namespace, query: str) -> int:
    """``--flow docgen``：gather → draft → 人审（y/n + 原因）→ finalize；``--resume`` 恢复挂起会话。

    审批记录（approval_log）写入 trace（event=approval_decision），状态由 sqlite checkpointer 持久化。
    """
    from langgraph.types import Command

    from water_agent.graphs.docgen import build_docgen_graph

    session_id = args.resume or new_session_id()
    session_dir = Path(args.session_dir)
    config = {"configurable": {"thread_id": session_id}}
    recorder = JsonTraceRecorder(Path(args.trace_dir))
    recorder.start(run_id=uuid4().hex, trace_id=uuid4().hex, query=query or "docgen")
    # --json 时 stdout 只留最终 JSON，预览/提示走 stderr
    ui = sys.stderr if args.json else sys.stdout
    with ExitStack() as stack:
        saver = _build_chat_checkpointer(session_dir, stack)
        from langgraph.checkpoint.sqlite import SqliteSaver

        if not isinstance(saver, SqliteSaver):
            # 审批挂起依赖持久化：内存态下 --resume 无法跨进程恢复，拒绝静默降级
            print(
                json.dumps(
                    {"ok": False, "error": "docgen 需要持久化会话（sqlite 不可用），拒绝在内存态构建（--resume 无保证）"},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        app = build_docgen_graph(checkpointer=saver)
        snapshot = app.get_state(config) if args.resume else None
        if snapshot is not None and snapshot.next:
            print(f"恢复审批会话 {session_id}", file=ui)
            interrupts = [i for t in snapshot.tasks for i in (getattr(t, "interrupts", ()) or ())]
            result: dict = {"__interrupt__": interrupts, **dict(snapshot.values)} if interrupts else dict(snapshot.values)
            if not interrupts:
                result = app.invoke(None, config=config)
        else:
            if not query:
                print("请提供要生成的文档描述（或用 --resume 恢复挂起的审批会话）。", file=sys.stderr)
                return 2
            save_session_meta(session_id, session_dir=session_dir, query=query, flow="docgen")
            gathered = _gather_docgen_inputs(query, args)
            doc_type = getattr(args, "doc_type", "auto")
            if doc_type == "auto":
                # P2：计算意图 → 计算书；规范问答/设计意图 → 设计说明
                doc_type = "calculation_book" if detect_intent(query) == "calc" else "design_note"
            print(f"文档类型：{doc_type}（人工审批通过后产出正式文档）", file=ui)
            # gather 一次检索的产物显式进图：引用/计算结果/完整检索上下文/检索回答
            initial_state = {
                "query": query,
                "status": "running",
                "draft_rounds": 0,
                "doc_type": doc_type,
                "citations": gathered["citations"],
                "calc_result": gathered["calc_result"],
                "gather_artifacts": {
                    "retrieval_context": gathered["context"],
                    "retrieval_claims": [c.source_id for c in gathered["citations"]],
                    "retrieval_payload": {
                        "answer": gathered["rag_answer"],
                        "status": gathered["rag_status"],
                    },
                },
            }
            result = app.invoke(initial_state, config=config)

        while result.get("__interrupt__"):
            preview = result["__interrupt__"][0].value
            print("\n===== 草稿预览（待审批）=====", file=ui)
            print(preview.get("draft_preview", ""), file=ui)
            print("=============================", file=ui)
            try:
                answer = _read_cli_input("批准生成正式文档？[y/n]: ", ui).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(f"\n审批挂起，稍后用 --resume {session_id} 恢复。", file=ui)
                recorder.finish("waiting", data={"session_id": session_id})
                return 0
            decision: dict[str, Any]
            if answer in {"y", "yes", "是"}:
                decision = {"approved": True}
            else:
                try:
                    reason = _read_cli_input("拒绝原因（将带回草稿重拟）: ", ui).strip()
                except (EOFError, KeyboardInterrupt):
                    reason = ""
                decision = {"approved": False, "reason": reason}
            recorder.record(node="approval", event="approval_decision", status="running", data={**decision, "session_id": session_id})
            result = app.invoke(Command(resume=decision), config=config)

    status = result.get("status", "")
    recorder.finish(status, data={"approval_log": result.get("approval_log", []), "session_id": session_id})
    payload = {
        "ok": status == "succeeded",
        "session_id": session_id,
        "status": status,
        "final_doc": result.get("final_doc"),
        "approval_log": result.get("approval_log", []),
        "gather_artifacts": result.get("gather_artifacts", {}),
        "errors": result.get("errors", []),
        "trace_path": str(Path(args.trace_dir) / f"{recorder.trace_id}.json"),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif status == "succeeded":
        print("\n===== 正式文档 =====")
        print(payload["final_doc"])
        print(f"\n状态: {status}；审批记录 {len(payload['approval_log'])} 条；trace: {payload['trace_path']}")
    else:
        print(f"\n状态: {status}")
        if payload["errors"]:
            print("错误:", "; ".join(payload["errors"]))
    return 0 if status == "succeeded" else 1

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    query = (args.query or "").strip()

    if getattr(args, "flow", "qa") == "docgen":
        try:
            return run_docgen_flow(args, query)
        except Exception as exc:  # noqa: BLE001 — CLI 顶层兜底
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2

    if args.interactive:
        return _run_chat_session(args, query=query)

    if not query:
        print("请提供问题参数，或用 --interactive 进入问答循环。", file=sys.stderr)
        return 2
    try:
        payload = run_once(query, args=args)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "retrieval": "real-db-failed"}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI 顶层兜底，把任意失败转成结构化错误输出
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if payload.get("streamed"):
        # token 已随流打印到 stdout；结构化信息写 stderr，避免污染令牌流
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        else:
            print(f"\n状态: {payload.get('status', '')}", file=sys.stderr)
        return 0
    _print_result(payload, json_output=args.json)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

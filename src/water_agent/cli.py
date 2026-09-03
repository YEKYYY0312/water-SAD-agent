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
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from water_agent.domain.state import default_state
from water_agent.embedding import build_embedding_provider
from water_agent.generation.base import Generator
from water_agent.generation.chat_model import DEFAULT_API_KEY_ENV, build_chat_model
from water_agent.generation.llm import DEFAULT_SYSTEM_PROMPT, build_llm_generator
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.agent_rag import astream_agentic, build_agentic_app
from water_agent.graphs.calc import build_calc_graph, detect_intent
from water_agent.graphs.chat import build_chat_app
from water_agent.graphs.rag import build_rag_graph
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.prompting import PromptAssembler
from water_agent.prompting.runtime_context import (
    RuntimeContextProjection,
    default_context_builder,
)
from water_agent.retrieval.base import Retriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.rerank import BailianReranker, LocalReranker, build_bailian_reranker
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


def build_app(*, args: argparse.Namespace, trace: JsonTraceRecorder | None = None) -> tuple[object, Retriever, str]:
    """装配 检索(含 rerank) + 生成 + RAG 图。返回 (app, retriever, db_path_or_empty).

    ``trace`` 传入图内 recorder，使检索/生成/引用校验等事件可被记录。
    """
    fixture = load_clauses_jsonl(DEFAULT_FIXTURE)
    db_path = None
    if args.db_path:
        if not Path(args.db_path).exists():
            # 用户显式指定的库不存在：响亮失败，不得静默回落 fixture
            raise ValueError(f"--db-path 不存在：{args.db_path}（请检查路径，或去掉 --db-path 使用 data/indexes 下的默认库）")
        db_path = Path(args.db_path)
    elif not args.mock_generator:
        # mock 自测不自动加载真实库：mock_embed 与真实嵌入空间不匹配，直接回落 fixture
        candidates = sorted(DEFAULT_INDEX_DIR.glob("*.db")) if DEFAULT_INDEX_DIR.exists() else []
        if candidates:
            db_path = candidates[-1]

    provider = None
    dim = args.dimensions
    if db_path is not None:
        if args.mock_generator:
            # mock 自测：用确定性 mock_embed（与库同维），不建真实 provider、不要求 API Key
            import triviumdb

            probe = triviumdb.TriviumDB(str(db_path))
            dim = dim or int(probe.dim())
            probe.close()
            from water_agent.retrieval.trivium import mock_embed

            embedding_fn = lambda text: mock_embed(text, dim=dim)
            retriever = TriviumRetriever(
                db_path=db_path,
                embedding_fn=embedding_fn,
                hybrid_alpha=args.hybrid_alpha,
                dim=dim,
                fallback_clauses=fixture,
            )
        else:
            # 真实库需要真实 embedding 做 query 向量（维度须与库一致）
            provider, detected = _embedding_for(db_path)
            if args.dimensions is None:
                dim = detected
            retriever = TriviumRetriever(
                db_path=db_path,
                embedding_fn=provider.embed,
                hybrid_alpha=args.hybrid_alpha,
                dim=dim,
                fallback_clauses=fixture,
            )
    else:
        retriever = TriviumRetriever(db_path=None, fallback_clauses=fixture)

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

    if args.rerank == "local":
        reranker = LocalReranker()
    elif args.rerank == "bailian":
        reranker = build_bailian_reranker(
            api_key=args.rerank_api_key,
            model=args.rerank_model or BailianReranker.DEFAULT_MODEL,
            base_url=args.rerank_base_url or BailianReranker.DEFAULT_BASE_URL,
        )
    else:
        reranker = None
    app = build_rag_graph(
        retriever,
        generator,
        trace=trace,
        reranker=reranker,
        top_k=args.top_k,
        recall_top_k=args.recall_top_k,
    )
    return app, retriever, str(db_path or "")


def run_calc_once(query: str) -> dict:
    """走水力计算确定性子图（参数解析用规则替身，数值计算用真工具）。

    计算意图的确定性入口；真实 LLM 参数理解在 Step 7 接入。
    """
    app = build_calc_graph()
    state = {
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
    """Step 6 占位模型：第一轮发 retrieve_clauses，第二轮根据检索结果给最终回答。

    未接真实 DeepSeek function calling 前用此 fake 顶替（Step 7 接真实）。
    """

    query: str

    @property
    def _llm_type(self) -> str:
        return "scripted-agentic"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # 第一轮：发起检索
        if not any(isinstance(m, ToolMessage) for m in messages):
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "retrieve_clauses",
                                    "args": {"query": self.query, "top_k": 5},
                                    "id": "c-retrieve",
                                }
                            ],
                        )
                    )
                ]
            )
        # 后续轮：从最近一次 retrieve_clauses 结果组织回答
        hits: list[dict] = []
        for m in reversed(messages):
            if isinstance(m, ToolMessage) and m.name == "retrieve_clauses":
                payload = json.loads(m.content) if isinstance(m.content, str) else {}
                hits = payload.get("hits", []) or []
                break
        if hits:
            top = hits[0]
            answer = (
                f"根据规范检索结果：{top.get('title', '')}"
                f"（{top.get('source_id', '')}）——{top.get('text', '')}"
            )
        else:
            answer = "未能在规范条款库中检索到相关内容。"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer))])


def _build_agentic_retriever(args: argparse.Namespace) -> tuple[Retriever, str]:
    """构建 agentic 循环用的检索器：mock 时强制 fixture 回落（与 Step 6 一致）。"""
    offline_overrides = dict(vars(args))
    offline_overrides["mock_generator"] = True
    _, retriever_obj, db_path = build_app(args=argparse.Namespace(**offline_overrides))
    assert hasattr(retriever_obj, "retrieve"), "build_app 未返回检索器"
    return retriever_obj, db_path


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


def run_agentic_once(query: str, *, args: argparse.Namespace) -> dict:
    """走模型驱动的 Agentic RAG 外层循环（Step 6→7 双路径）。

    - ``--mock-generator``：全程离线——脚本化占位模型（第一轮 retrieve、第二轮按
      检索结果作答）+ mock 检索装配（fixture 回落），不要求任何 API Key；
    - 默认：真实 DeepSeek function calling（``build_chat_model`` + 真实检索装配）。
      无 Key 抛带指引的结构化错误（复用全局错误风格），绝不静默降级为假模型；
    - ``--stream``（仅真实链路）：messages+values 双模式流式，token 实时打印到
      stdout，payload 带 ``streamed=True`` 供 main 决定输出格式。
    """
    api_key_env = getattr(args, "api_key_env", DEFAULT_API_KEY_ENV)
    model: BaseChatModel
    if args.mock_generator:
        retriever_obj, db_path = _build_agentic_retriever(args)
        model = _ScriptedAgenticModel(query=query)
    else:
        try:
            model = build_chat_model(
                model=args.model,
                base_url=args.base_url,
                api_key_env=api_key_env,
            )
        except ValueError as exc:
            raise ValueError(
                f"{exc}。agentic 真实模型链路需要 API Key：export {api_key_env}=sk-...，"
                "或用 --mock-generator 走离线占位链路（不连真实模型，不要求 Key）"
            ) from exc
        try:
            _, retriever_obj, db_path = build_app(args=args)
        except ValueError as exc:
            raise ValueError(
                f"{exc}。真实检索装配失败：需配置 DASHSCOPE_API_KEY（embedding）且索引可用；"
                "或用 --mock-generator 走离线占位链路（fixture 回落，不要求 Key）"
            ) from exc

    registry = ToolRegistry()
    registry.register(build_retrieve_clauses_tool(retriever_obj))
    registry.register(build_cite_clauses_tool())
    registry.register(calc_velocity_spec)
    tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]

    # Step 8：动态系统提示装配（角色/版本快照/公式口径/工具指南）+ 运行时上下文投影
    assembler = PromptAssembler(tool_registry=registry)
    projection = RuntimeContextProjection()

    stream = bool(getattr(args, "stream", False)) and not args.mock_generator
    thread_id = uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}
    with ExitStack() as stack:
        saver = _build_chat_checkpointer(args.session_dir, stack)
        app = build_agentic_app(
            model=model,
            tools=tools,
            checkpointer=saver,
            prompt=assembler,  # callable：装配器按 state 动态生成 SystemMessage
        )
        initial_messages = _initial_messages_with_runtime_context(
            query,
            projection,
            session_refs=[thread_id],
        )
        if stream:
            state = asyncio.run(
                astream_agentic(
                    app,
                    query,
                    config,
                    on_token=lambda tok: print(tok, end="", flush=True),
                    messages=initial_messages,
                )
            )
            print(flush=True)
        else:
            state = app.invoke(
                {"messages": initial_messages, "query": query},
                config=config,
            )

    citations = [
        {"source_id": c.source_id, "spec_version": c.spec_version, "declared_by": c.declared_by}
        for c in state.get("citations", [])
    ]
    return {
        "ok": True,
        "query": query,
        "answer": state.get("answer", ""),
        "status": state.get("status", ""),
        "citations": citations,
        "errors": state.get("errors", []),
        "retry_count": state.get("retry_count", 0),
        "intent": "agentic",
        "streamed": stream,
        "retriever_fallback": bool(getattr(retriever_obj, "is_fallback", True)),
        "db_path": db_path,
    }


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
        result = app.invoke(state)
    except TriviumRetrievalError as exc:
        recorder.finish("failed", error=str(exc), query=query)
        raise RuntimeError(f"real db retrieval failed: {exc}") from exc
    citations = [c.model_dump(mode="json") | {"source_id": c.source_id} for c in result.get("citations", [])]
    context = [c.model_dump(mode="json") | {"source_id": c.source_id} for c in result.get("context", [])]
    final_trace_id = result.get("trace_id", trace_id)
    return {
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
    if payload["retriever_fallback"]:
        print("检索: fixture 回落（未使用真实 TriviumDB 索引）")
    if payload["errors"]:
        print("错误:", "; ".join(payload["errors"]))
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

        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        stack.callback(conn.close)
        return SqliteSaver(conn)
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
    """交互式多轮会话：checkpointer + thread_id 做会话隔离，--resume 恢复历史。"""
    session_id = args.resume or new_session_id()
    session_dir = Path(args.session_dir)
    if not args.resume:
        save_session_meta(session_id, session_dir=session_dir, query=(query or "interactive"))
    print(f"会话 ID: {session_id}")
    config = {"configurable": {"thread_id": session_id}}
    with ExitStack() as stack:
        saver = _build_chat_checkpointer(session_dir, stack)
        app = build_chat_app(checkpointer=saver)
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
                result = app.invoke(
                    {"messages": [HumanMessage(content=line)], "query": line},
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001 — 交互循环保持健壮，任一问题打印后继续
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
                continue
            if args.json:
                print(
                    json.dumps(
                        {"session_id": session_id, "answer": result.get("answer", ""), "status": result.get("status", "")},
                        ensure_ascii=False,
                    )
                )
                continue
            print(result.get("answer", ""))
            print(f"状态: {result.get('status', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    query = (args.query or "").strip()

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

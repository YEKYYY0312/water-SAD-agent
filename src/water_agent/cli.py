"""生产问答 CLI + 交互式问答循环。

生产链路：TriviumDB 混合检索召回 → LocalReranker 重排 → top_k=5 → LLM 生成。
默认真实 LLM（DeepSeek 官方 Claude 兼容）；--mock-generator 可用确定性生成器自测链路。
--mode agentic：Step 6 起模型驱动的 Agentic RAG 外层循环；Step 7 接真实 DeepSeek
function calling（无 Key 属结构化错误而非静默降级），--stream 可流式打印 token。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

from water_agent.domain.citation import CitationClaim
from water_agent.domain.state import default_state
from water_agent.generation.base import Generator
from water_agent.generation.chat_model import DEFAULT_API_KEY_ENV
from water_agent.generation.llm import DEFAULT_SYSTEM_PROMPT, build_llm_generator
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.calc import build_calc_graph, detect_intent, parse_calc_query
from water_agent.graphs.rag import build_rag_graph
from water_agent.observability.json_trace import (
    TRACE_DIR_ENV,
    JsonTraceRecorder,
    resolve_trace_dir,
)
from water_agent.prompting import PromptAssembler
from water_agent.retrieval.base import Retriever
from water_agent.retrieval.trivium import TriviumRetrievalError

# Kept importable from this module for callers that still import the scripted
# model from the CLI surface; the class now lives in water_agent.runtime.agentic.
from water_agent.runtime.agentic import (
    AgenticSession,
    _build_cache_service,
    _build_chat_checkpointer,
    _build_reranker,
    _build_retriever,
    _ScriptedAgenticModel,  # noqa: F401
)
from water_agent.runtime.governance.binding import (
    default_gateway_with_sink,
    local_request_context,
)
from water_agent.session import (
    DEFAULT_SESSION_DIR,
    new_session_id,
    save_session_meta,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRACE_DIR = REPO_ROOT / "data" / "traces"


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
        choices=["qa", "docgen", "multi-agent"],
        default="qa",
        help="qa 问答（默认）；docgen 计算书生成：gather→draft→人审(interrupt)→finalize，--resume 恢复挂起审批；multi-agent P4.5 协同流水线（显式启用）",
    )
    p.add_argument(
        "--doc-type",
        choices=["auto", "calculation_book", "design_note"],
        default="auto",
        help="docgen 文档类型：auto（默认，计算意图→计算书，其余→设计说明）/ calculation_book 计算书 / design_note 设计说明",
    )
    # 默认在解析时算，好让容器里的 WATER_AGENT_TRACE_DIR 生效：镜像内 /app/data 是只读的
    p.add_argument(
        "--trace-dir",
        default=str(resolve_trace_dir(default=DEFAULT_TRACE_DIR)),
        help=f"Trace 输出目录（默认读 ${TRACE_DIR_ENV}，未设置时 {DEFAULT_TRACE_DIR}）",
    )
    p.add_argument(
        "--drawing-root",
        default=None,
        help="图纸目录（P3，默认读环境变量 DRAWING_ROOT）。目录内 *.dxf 按文件名作登记号；"
        "未配置或目录内无 DXF 时不注册 read_drawing 工具（能力不可用就不暴露）",
    )
    p.add_argument("--json", action="store_true", help="以 JSON 输出（单次模式）")
    return p


def build_app(
    *,
    args: argparse.Namespace,
    trace: JsonTraceRecorder | None = None,
    request_context=None,
    gateway=None,
) -> tuple[object, Retriever, str]:
    """装配 检索(含 rerank) + 生成 + legacy RAG 图。返回 (app, retriever, db_path_or_empty).

    ``trace`` 传入图内 recorder，使检索/生成/引用校验等事件可被记录。
    ``request_context`` / ``gateway`` 由本次 run 的稳定会话标识构造（P2.5 主体隔离），
    为 ``None`` 时回落进程级默认主体（仍经 default_gateway，不绕过治理）。
    """
    retriever, db_path = _build_retriever(args, request_context=request_context, gateway=gateway)

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
            gateway=gateway,
            request_context=request_context,
        )

    app = build_rag_graph(
        retriever,
        generator,
        trace=trace,
        reranker=_build_reranker(args, request_context=request_context, gateway=gateway),
        top_k=args.top_k,
        recall_top_k=args.recall_top_k,
        request_context=request_context,
        cache_service=_build_cache_service(args) if not args.mock_generator else None,
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
    run_id = uuid4().hex
    trace_id = uuid4().hex
    # P2.5 主体隔离：用本次 run 的稳定标识构造 RequestContext，
    # 传给生成器/embedding/rerank/图，避免所有会话共享固定 thread_id（如 legacy-rag）。
    request_context = local_request_context(thread_id=run_id, request_id=f"req-{run_id[:12]}")
    # P2.5 trace 接线：usage/budget/rate-limit/breaker/failure 事件写入当前 run 的 trace。
    gateway = default_gateway_with_sink(recorder.record_provider_event)
    app, retriever, db_path = build_app(
        args=args, trace=recorder, request_context=request_context, gateway=gateway
    )
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

def _resolve_flow(args: argparse.Namespace) -> str:
    flow = getattr(args, "flow", "qa")
    env_flow = __import__("os").getenv("WATER_AGENT_FLOW", "")
    if env_flow:
        env_norm = env_flow.strip().lower().replace("_", "-")
        if env_norm in ("multi-agent", "multiagent") and flow == "qa":
            return "multi-agent"
    # argparse already normalizes; also accept underscore variant passed programmatically
    if isinstance(flow, str):
        flow = flow.replace("_", "-")
    return flow


def _multi_agent_systems_from_query(query: str) -> list[str]:
    """Extract only explicitly named systems; generic ``给排水`` stays ambiguous."""
    searchable = query.replace("给排水", "")
    systems: list[str] = []
    for system, pattern in (
        ("water_supply", r"给水|water[_ -]?supply"),
        ("drainage", r"排水|污水|drainage"),
        ("fire_protection", r"消防|fire[_ -]?protection"),
    ):
        if re.search(pattern, searchable, re.IGNORECASE):
            systems.append(system)
    return systems


def _multi_agent_named_value(query: str, names: str) -> str | None:
    match = re.search(
        rf"(?:{names})\s*(?:[:=：]|为)\s*([A-Za-z0-9_\-一-鿿]+)",
        query,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _multi_agent_required_outputs(query: str) -> list[str]:
    match = re.search(
        r"(?:输出|交付|required_outputs?)\s*(?:[:=：]|为)\s*([^。；;]+)",
        query,
        re.IGNORECASE,
    )
    if match is None:
        return []
    clause = match.group(1)
    outputs: list[str] = []
    for output, pattern in (
        ("calculation_book", r"计算书|calculation[_ -]?book"),
        ("design_note", r"设计说明|design[_ -]?note"),
        ("drawing", r"图纸|drawing|dxf"),
    ):
        if re.search(pattern, clause, re.IGNORECASE):
            outputs.append(output)
    return outputs


def _multi_agent_si_units_are_explicit(query: str, params: dict[str, Any]) -> bool:
    """Accept SI only when declared directly or encoded by every parsed value's unit."""
    if re.search(r"(?:单位\s*(?:[:=：]|为)\s*SI\b|SI\s*单位|国际单位制)", query, re.IGNORECASE):
        return True
    numeric_keys = {
        key
        for key, value in params.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if not numeric_keys:
        return False
    evidence_patterns = {
        "diameter_mm": r"\bDN\s*\d+(?:\.\d+)?\b",
        "flow_lps": r"(?:Q\s*=|流量\s*)\s*\d+(?:\.\d+)?\s*L\s*/\s*s",
        "length_m": r"(?:\bL\s*=|管长\s*)\s*\d+(?:\.\d+)?\s*m\b",
        "velocity_mps": r"(?:\bv\s*=|流速\s*)\s*\d+(?:\.\d+)?\s*m\s*/\s*s",
        "fill_ratio": r"h\s*/\s*d\s*=\s*\d+(?:\.\d+)?",
        "manning_n": r"\bn\s*=\s*\d+(?:\.\d+)?",
        "friction_factor": r"\bf\s*=\s*\d+(?:\.\d+)?",
        "hazen_williams_c": r"\bC\s*=\s*\d+(?:\.\d+)?",
        "static_head_m": r"几何(?:扬程)?\s*\d+(?:\.\d+)?\s*(?:m|米)\b",
        "friction_head_m": r"沿程\s*\d+(?:\.\d+)?\s*(?:m|米)\b",
        "local_head_m": r"局部\s*\d+(?:\.\d+)?\s*(?:m|米)\b",
        "residual_head_m": r"剩余\s*\d+(?:\.\d+)?\s*(?:m|米)\b",
    }
    return all(
        key in evidence_patterns and re.search(evidence_patterns[key], query, re.IGNORECASE)
        for key in numeric_keys
    )


def _parse_multi_agent_project_brief(
    query: str,
    *,
    project_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Build a ProjectBrief only from deterministic, explicitly evidenced input fields."""
    from water_agent.multi_agent.contracts import ProjectBrief

    outcome = parse_calc_query(query)
    systems = _multi_agent_systems_from_query(query)
    params = dict(outcome.params)
    if len(systems) == 1:
        params["scenario"] = systems[0]

    start_node = _multi_agent_named_value(query, r"起点|start(?:_node(?:_id)?)?")
    end_node = _multi_agent_named_value(query, r"终点|终止点|end(?:_node(?:_id)?)?")
    length_m = params.get("length_m")
    material = _multi_agent_named_value(query, r"材质|material")
    required_outputs = _multi_agent_required_outputs(query)

    missing: list[str] = []
    if not systems:
        missing.append("systems")
    if not _multi_agent_si_units_are_explicit(query, params):
        missing.append("unit_system")
    if outcome.formula is None:
        missing.extend(["hydraulic_cases[0].formula", "hydraulic_cases[0].inputs"])
    else:
        missing.extend(
            f"hydraulic_cases[0].inputs.{field}" for field in outcome.missing_inputs
        )
    if start_node is None:
        missing.append("topology[0].start_node_id")
    if end_node is None:
        missing.append("topology[0].end_node_id")
    if not isinstance(length_m, (int, float)) or isinstance(length_m, bool):
        missing.append("topology[0].length_m")
    if material is None:
        missing.append("spatial_constraints.material")
    if not required_outputs:
        missing.append("required_outputs")
    if len(systems) > 1:
        missing.append("hydraulic_cases[0].inputs.scenario")
    if missing:
        return None, list(dict.fromkeys(missing))

    assert outcome.formula is not None
    assert start_node is not None and end_node is not None
    assert isinstance(length_m, (int, float)) and not isinstance(length_m, bool)
    assert material is not None
    brief = ProjectBrief.model_validate(
        {
            "project_id": project_id,
            "request": query,
            "systems": systems,
            "unit_system": "SI",
            "hydraulic_cases": [
                {
                    "case_id": "case-cli-1",
                    "formula": outcome.formula,
                    "inputs": params,
                }
            ],
            "topology": [
                {
                    "start_node_id": start_node,
                    "end_node_id": end_node,
                    "length_m": float(length_m),
                }
            ],
            "spatial_constraints": [{"kind": "material", "value": material}],
            "required_outputs": required_outputs,
            "user_constraints": [],
        }
    )
    return brief.model_dump(mode="json"), []


def _summary_value(value: Any) -> str:
    """摘要里的值原样转成文本：数值不取整、不换单位（铁律 1），缺省显示为 —。"""
    if value is None:
        return "—"
    if isinstance(value, dict):
        return ", ".join(f"{key}={_summary_value(item)}" for key, item in value.items()) or "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(_summary_value(item) for item in value) or "—"
    return str(value)


def _print_multi_agent_approval_request(
    request: dict[str, Any], ui, *, fallback_summary: dict[str, Any] | None = None
) -> None:
    """批准前逐项摆出待批准的最终包（此前只打印一个哈希，批准人是盲批）。

    内容来自审批请求里的 ``summary``（``orchestrator.approval_summary``），这里只排版。升级前挂起的
    会话，持久化的请求里没有 summary，用调用方从会话状态重建的同源摘要（``fallback_summary``）。
    ``--json`` 时 ``ui`` 是 stderr，stdout 只留最终 JSON。
    """
    summary = request.get("summary") or fallback_summary or {}
    print("\n===== 待批准最终包（批准前请逐项核对）=====", file=ui)
    print(f"待批准最终包：{request['final_package_hash']}", file=ui)
    if not summary:
        print("（无法生成摘要：会话里没有完整的发布产物集，请勿批准）", file=ui)
    artifacts = summary.get("artifacts") or []
    print(f"产物清单（{len(artifacts)} 项）：", file=ui)
    for row in artifacts:
        print(
            f"  - {row['artifact_type']} rev{row['revision']} hash={row['payload_hash_prefix']}… "
            f"[{row['status']}] {row['artifact_id']}",
            file=ui,
        )
    segments = summary.get("segments") or []
    print(f"管段（{len(segments)} 段）：", file=ui)
    for seg in segments:
        dn = f"DN{seg['dn_mm']}" if seg.get("dn_mm") is not None else "DN —"
        print(
            f"  - {seg['segment_id']} | {seg['system']} | {seg['start_node_id']}→{seg['end_node_id']} | {dn} | "
            f"长度 {_summary_value(seg.get('length_m'))} m | 材质 {_summary_value(seg.get('material'))} | "
            f"坡度 {_summary_value(seg.get('slope'))} | hydraulic_status={seg['hydraulic_status']} | "
            f"计算工况 {_summary_value(seg.get('calc_case_ids'))}",
            file=ui,
        )
    cases = summary.get("calc_cases") or []
    print(f"水力工况（{len(cases)} 个）：", file=ui)
    for case in cases:
        print(f"  - {case['calc_case_id']} | {case['formula']} | {case['status']}", file=ui)
        print(f"      输入：{_summary_value(case.get('inputs'))}", file=ui)
        print(f"      结果：{_summary_value(case.get('outputs'))}", file=ui)
        for note in case.get("assumptions") or []:
            print(f"      assumption：{note}", file=ui)
    citations = summary.get("citations") or []
    print(f"设计依据（{len(citations)} 条）：", file=ui)
    for citation in citations:
        status = citation.get("version_status") or "未校验"
        print(f"  - {citation['source_id']}（规范版本状态：{status}）", file=ui)
    review = summary.get("review") or {}
    issues = review.get("issues") or []
    not_checked = review.get("not_checked") or []
    print(
        f"审查：decision={review.get('decision')}，问题 {len(issues)} 条，未检查 {len(not_checked)} 项",
        file=ui,
    )
    for issue in issues:
        print(
            f"  - 问题 [{issue['severity']}/{issue['owner']}] {issue['rule_id']}"
            f"{' @' + issue['object_ref'] if issue.get('object_ref') else ''}：{issue['message']}",
            file=ui,
        )
    for item in not_checked:
        print(f"  - 未检查：{item}", file=ui)
    print(f"staging 草图（DXF）：{_summary_value(summary.get('staging_dxf_path'))}", file=ui)
    disclosed = request.get("disclosed_limitations") or []
    if disclosed:
        print(f"批准前请确认以下 {len(disclosed)} 项披露（审查器未能判定合规的内容）：", file=ui)
        for item in disclosed:
            print(f"  - {item}", file=ui)
    print("==========================================", file=ui)


def _prompt_multi_agent_decision(ui) -> dict[str, Any] | None:
    """multi-agent 审批提示：批准？[y/n] →（拒绝时）拒绝原因。返回 resume 值；None = 挂起。

    两处提示的 Ctrl-C/EOF 语义一致：都挂起、不提交任何决定。此前只有第一个提示做了处理，
    "拒绝原因"处中断会让异常直接冲出 CLI（EOF 退出码 2，Ctrl-C 原样抛出）。
    """
    try:
        answer = _read_cli_input("批准发布最终草图？[y/n]: ", ui).strip().lower()
        if answer in {"y", "yes", "是"}:
            return {"approved": True}
        return {"approved": False, "reason": _read_cli_input("拒绝原因: ", ui).strip()}
    except (EOFError, KeyboardInterrupt):
        return None


def run_multi_agent_flow(args: argparse.Namespace, query: str) -> int:
    """``--flow multi-agent``：真实 P4.5 图，审批前只生成 staging 草图。"""
    import tempfile as _tf

    from langgraph.types import Command

    from water_agent.multi_agent.contracts import MULTI_MISSING_INPUTS
    from water_agent.multi_agent.orchestrator import (
        approval_summary,
        build_orchestrator_graph,
        default_multi_agent_state,
        review_disclosures,
    )

    session_id = args.resume or new_session_id()
    config = {"configurable": {"thread_id": session_id}}
    ui = sys.stderr if args.json else sys.stdout
    parsed_brief: dict[str, Any] | None = None
    if not args.resume:
        if not query:
            print("请提供项目描述，或使用 --resume 恢复待审批的 multi-agent 会话。", file=sys.stderr)
            return 2
        parsed_brief, missing_inputs = _parse_multi_agent_project_brief(
            query,
            project_id=f"proj-cli-{session_id}",
        )
        if missing_inputs:
            payload: dict[str, Any] = {
                "ok": False,
                "flow": "multi-agent",
                "session_id": session_id,
                "status": "missing_inputs",
                "waiting": True,
                "error_code": MULTI_MISSING_INPUTS,
                "missing_inputs": missing_inputs,
                "errors": [MULTI_MISSING_INPUTS],
                "release_checks": {},
                "exported_file_path": None,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json else None))
            return 1
    with ExitStack() as stack:
        saver = _build_chat_checkpointer(Path(args.session_dir), stack)
        from langgraph.checkpoint.sqlite import SqliteSaver

        if not isinstance(saver, SqliteSaver):
            print(json.dumps({"ok": False, "error": "multi-agent 审批恢复需要 SqliteSaver"}, ensure_ascii=False), file=sys.stderr)
            return 2
        app = build_orchestrator_graph(checkpointer=saver)
        snapshot = app.get_state(config) if args.resume else None
        # 判挂起以 interrupts 为准、不能只看 next：审批节点拒收非法 resume 值后会带 error 再
        # interrupt 一次，会话仍挂起，但 LangGraph 1.2.11 下此时 next 为 ()（见
        # orchestrator.approval_decision_error）。只看 next 会把挂起的会话当成已结束。
        pending = (
            [i for task in snapshot.tasks for i in (getattr(task, "interrupts", ()) or ())]
            if snapshot is not None
            else []
        )
        if snapshot is not None and pending:
            result: dict[str, Any] = {"__interrupt__": pending, **dict(snapshot.values)}
        elif args.resume:
            print(
                json.dumps(
                    {"ok": False, "error": f"multi-agent 会话 {session_id} 没有待批准的请求，无可恢复的审批"},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        else:
            assert parsed_brief is not None
            staging = _tf.mkdtemp(prefix="ma-cli-staging-")
            final = _tf.mkdtemp(prefix="ma-cli-final-")
            save_session_meta(session_id, session_dir=args.session_dir, query=query, flow="multi-agent")
            result = app.invoke(
                default_multi_agent_state(
                    project_brief=parsed_brief,
                    retrieved_source_ids=[], staging_dir=staging, final_dir=final,
                    output_file_id="cli-drawing",
                ),
                config=config,
            )

        while result.get("__interrupt__"):
            request = result["__interrupt__"][0].value
            if request.get("error"):
                # 图内拒收了上一次提交的 resume 值、会话重新挂起：说清楚，这次给出的决定才算数
                print(f"上一次提交的审批决定无效，未被采纳：{request['error']}", file=ui)
            _print_multi_agent_approval_request(request, ui, fallback_summary=approval_summary(result))
            decision = _prompt_multi_agent_decision(ui)
            if decision is None:
                print(f"\n审批已挂起，稍后使用 --resume {session_id} 恢复。", file=ui)
                return 0
            result = app.invoke(Command(resume=decision), config=config)

    payload = {
        "ok": result.get("status") == "succeeded", "flow": "multi-agent", "session_id": session_id,
        "status": result.get("status"), "errors": result.get("errors", []),
        "release_checks": result.get("release_checks", {}), "exported_file_path": result.get("exported_file_path"),
        "waiting": result.get("status") in {"waiting", "missing_inputs"},
        "missing_inputs": result.get("missing_inputs", []),
        "project_brief": result.get("project_brief"),
        "retrieved_source_ids": result.get("retrieved_source_ids", []),
        "disclosed_limitations": review_disclosures(result),
        # 结构化的最终包摘要（与审批请求同一来源）；发布集不完整时为 null
        "approval_summary": approval_summary(result),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json else None))
    return 0 if payload["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # D5 特性开关：WATER_AGENT_FLOW 环境变量等价于 --flow multi-agent
    args.flow = _resolve_flow(args)  # type: ignore[attr-defined]
    query = (args.query or "").strip()

    if getattr(args, "flow", "qa") == "multi-agent":
        try:
            return run_multi_agent_flow(args, query)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2

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

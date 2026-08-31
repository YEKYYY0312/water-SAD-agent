"""最小 RAG 状态机（P0）：router → retrieve → generate → citation_check → finalize。

引用校验失败且有重试预算时回流 retrieve，重试有明确上限（MAX_CITATION_RETRIES）。
"""
from __future__ import annotations

from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from water_agent.domain.state import (
    MAX_CITATION_RETRIES,
    MAX_TOTAL_STEPS,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    WaterAgentState,
)
from water_agent.generation.base import Generator
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.retrieval.base import Retriever


def build_rag_graph(
    retriever: Retriever,
    generator: Generator,
    trace: JsonTraceRecorder | None = None,
    *,
    reranker=None,
    top_k: int = 5,
    recall_top_k: int = 20,
):
    def _trace_start(state: WaterAgentState) -> None:
        if trace is not None:
            run_id = state.get("run_id") or uuid4().hex
            trace_id = state.get("trace_id") or uuid4().hex
            trace.start(run_id=run_id, trace_id=trace_id, query=state.get("query", ""))

    def _enter(node: str, state: WaterAgentState) -> None:
        if trace is not None:
            trace.node_enter(
                node,
                query=state.get("query", ""),
                retry_count=state.get("retry_count", 0),
                status=state.get("status", STATUS_RUNNING),
            )

    def _exit(node: str, state: WaterAgentState, **kwargs) -> None:
        if trace is not None:
            trace.node_exit(
                node,
                query=state.get("query", ""),
                retry_count=state.get("retry_count", 0),
                status=state.get("status", STATUS_RUNNING),
                **kwargs,
            )

    def router_node(state: WaterAgentState) -> dict:
        """P0 单意图路由：RAG。后续由 Supervisor 扩展 calc/drawing/docgen/material。"""
        run_id = state.get("run_id") or uuid4().hex
        trace_id = state.get("trace_id") or uuid4().hex
        if trace is not None:
            trace.start(run_id=run_id, trace_id=trace_id, query=state.get("query", ""))
        _enter("router", state)
        result = {
            "intent": "rag",
            "run_id": run_id,
            "trace_id": trace_id,
            "status": STATUS_RUNNING,
            "step_count": state.get("step_count", 0) + 1,
        }
        _exit("router", {**state, **result})
        return result

    def retrieve_node(state: WaterAgentState) -> dict:
        _enter("retrieve", state)
        query = state.get("query", "")
        # 混合检索先召回 recall_top_k 候选，再按 query rerank 截断到 top_k
        if reranker is not None:
            candidates = retriever.retrieve(query, top_k=recall_top_k)
            context = reranker.rerank(query, candidates, top_k=top_k)
        else:
            context = retriever.retrieve(query, top_k=top_k)
        result = {"context": context, "step_count": state.get("step_count", 0) + 1}
        if trace is not None:
            trace.record(
                node="retrieve",
                event="retrieve_complete",
                query=state.get("query", ""),
                context=context,
                retry_count=state.get("retry_count", 0),
                status=state.get("status", STATUS_RUNNING),
            )
        _exit("retrieve", {**state, **result}, context=context)
        return result

    def generate_node(state: WaterAgentState) -> dict:
        _enter("generate", state)
        generated = generator.generate(state.get("query", ""), state.get("context", []))
        result = {"answer": generated.answer, "citations": generated.citations, "step_count": state.get("step_count", 0) + 1}
        if trace is not None:
            trace.record(
                node="generate",
                event="generate_complete",
                query=state.get("query", ""),
                context=state.get("context", []),
                citations=generated.citations,
                retry_count=state.get("retry_count", 0),
                status=state.get("status", STATUS_RUNNING),
            )
        _exit("generate", {**state, **result}, context=state.get("context", []), citations=generated.citations)
        return result

    def citation_check_node(state: WaterAgentState) -> dict:
        """校验引用真实存在于本次检索结果，不得仅凭规范号判断有效。"""
        _enter("citation_check", state)
        valid = {c.source_id for c in state.get("context", [])}
        bad = [c.source_id for c in state.get("citations", []) if c.source_id not in valid]
        errors = list(state.get("errors", []))
        if bad:
            errors.append(f"引用校验失败：{bad} 不在本次检索结果中")
            result = {"citation_ok": False, "retry_count": state.get("retry_count", 0) + 1, "errors": errors, "query": state.get("query", "").strip()}
        else:
            result = {"citation_ok": True, "errors": errors}
        if trace is not None:
            trace.record(
                node="citation_check",
                event="citation_check_complete",
                query=state.get("query", ""),
                context=state.get("context", []),
                citations=state.get("citations", []),
                retry_count=result.get("retry_count", state.get("retry_count", 0)),
                error=errors[-1] if bad else None,
                status=state.get("status", STATUS_RUNNING),
            )
            if bad:
                trace.record(
                    node="citation_check",
                    event="retry",
                    query=state.get("query", ""),
                    context=state.get("context", []),
                    citations=state.get("citations", []),
                    retry_count=result["retry_count"],
                    error=errors[-1],
                    status=state.get("status", STATUS_RUNNING),
                )
        _exit("citation_check", {**state, **result}, context=state.get("context", []), citations=state.get("citations", []), error=errors[-1] if bad else None)
        return result

    def finalize_node(state: WaterAgentState) -> dict:
        _enter("finalize", state)
        errors = list(state.get("errors", []))
        if not state.get("context"):
            errors.append("检索无结果：无法在语料中定位相关条款")
            result = {"status": STATUS_FAILED, "errors": errors, "step_count": state.get("step_count", 0) + 1}
        elif not state.get("citation_ok", True):
            errors.append("达到引用校验重试上限，仍有无效引用")
            result = {"status": STATUS_FAILED, "errors": errors, "step_count": state.get("step_count", 0) + 1}
        else:
            result = {"status": STATUS_SUCCEEDED, "errors": errors, "step_count": state.get("step_count", 0) + 1}
        if trace is not None:
            if result["status"] == STATUS_FAILED:
                trace.record(node="finalize", event="error", query=state.get("query", ""), context=state.get("context", []), citations=state.get("citations", []), retry_count=state.get("retry_count", 0), error=errors[-1], status=result["status"])
            _exit("finalize", {**state, **result}, context=state.get("context", []), citations=state.get("citations", []), error=errors[-1] if result["status"] == STATUS_FAILED else None)
            trace.finish(status=result["status"], error=errors[-1] if result["status"] == STATUS_FAILED else None, context=state.get("context", []), citations=state.get("citations", []), retry_count=state.get("retry_count", 0))
        return result

    def route_after_citation(state: WaterAgentState) -> str:
        """引用通过 → finalize；未通过且有预算 → 回流 retrieve；否则 finalize。"""
        if state.get("citation_ok", True):
            return "finalize"
        if (
            state.get("retry_count", 0) < MAX_CITATION_RETRIES
            and state.get("step_count", 0) < MAX_TOTAL_STEPS
        ):
            return "retrieve"
        return "finalize"

    graph = StateGraph(WaterAgentState)
    graph.add_node("router", router_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("generate", generate_node)
    graph.add_node("citation_check", citation_check_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "router")
    graph.add_edge("router", "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "citation_check")
    graph.add_conditional_edges(
        "citation_check",
        route_after_citation,
        {"retrieve": "retrieve", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)

    return graph.compile()

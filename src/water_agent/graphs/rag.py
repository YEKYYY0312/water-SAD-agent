"""最小 RAG 状态机（P0）：router → retrieve → generate → citation_check → finalize。

引用校验失败且有重试预算时回流 retrieve，重试有明确上限（MAX_CITATION_RETRIES）。
"""
from __future__ import annotations

import time
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from water_agent.domain.citation import CitationClaim, enrich_claims, validate_citations
from water_agent.domain.state import (
    MAX_CITATION_RETRIES,
    MAX_TOTAL_STEPS,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    WaterAgentState,
)
from water_agent.generation.base import Generator
from water_agent.generation.cost import estimate_deepseek_cost
from water_agent.ingestion.version_registry import get_version_registry
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.retrieval.base import Retriever
from water_agent.runtime.governance.binding import local_request_context
from water_agent.runtime.governance.cache_service import AnswerCacheService
from water_agent.runtime.governance.contracts import (
    CallCategory,
    RequestContext,
)
from water_agent.runtime.governance.pipeline import (
    GuardrailPipeline,
    default_guardrail_pipeline,
)


def _generator_model_hint(generator: Generator) -> tuple[str, float]:
    """从生成器提取模型名与温度（仅真实 LLM 生成器有；mock 返回空串）。"""
    config = getattr(generator, "config", None)
    model = str(getattr(config, "model", "") or "")
    temperature = float(getattr(config, "temperature", 0.0) or 0.0)
    return model, temperature


def build_rag_graph(
    retriever: Retriever,
    generator: Generator,
    trace: JsonTraceRecorder | None = None,
    *,
    reranker=None,
    top_k: int = 5,
    recall_top_k: int = 20,
    version_registry=None,
    request_context: RequestContext | None = None,
    pipeline: GuardrailPipeline | None = None,
    cache_service: AnswerCacheService | None = None,
):
    """legacy 单次 RAG 图。``version_registry`` 缺省用 data/versions.json 单例（Step 8）。

    P2.5 生产治理接线（review 修复）：
    - ``request_context``：本次 run 的稳定会话主体（限流/预算/缓存隔离）。
    - ``pipeline``：输入/输出护栏（确定性正则，fail closed）。
    - ``cache_service``：精确回答缓存（lookup 在 provider 调用前，store 在引用/输出校验后）。
    三者缺省回落进程级默认实例，仍经治理层，不存在绕过。
    """
    if version_registry is None:
        version_registry = get_version_registry()
    pipeline = pipeline if pipeline is not None else default_guardrail_pipeline()
    if request_context is None:
        request_context = local_request_context(thread_id="legacy-rag")
    model_name, temperature = _generator_model_hint(generator)

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

    def _record_guardrail(stage: str, outcome) -> None:
        """把护栏裁定写入 trace（脱敏：只带规则 ID/动作/计数，不带原文）。"""
        if trace is None:
            return
        payload = outcome.trace_payload()
        try:
            trace.record_guardrail_event(
                stage=stage,
                action=payload.get("action", ""),
                rule_id=payload.get("rule_id", ""),
                ruleset_version=payload.get("ruleset_version", ""),
                code=payload.get("code"),
                hits=payload.get("hits", []),
                input_hash=trace.hash_input(outcome.content) if outcome.content else "",
            )
        except Exception:  # noqa: BLE001, S110 — 观测失败绝不影响执行
            pass

    def router_node(state: WaterAgentState) -> dict:
        """P0 单意图路由：RAG。后续由 Supervisor 扩展 calc/drawing/docgen/material。

        P2.5 输入护栏（review 修复）：在检索、模型调用、缓存 lookup 之前对用户输入
        做超长/注入/PII/secret 检查。命中注入或超长 → ``blocked``（直接收口，
        不再检索/生成）；PII/secret 按 ``RequestContext.pii_mode`` 脱敏后，
        后续检索与模型使用**处理后的文本**。
        """
        run_id = state.get("run_id") or uuid4().hex
        trace_id = state.get("trace_id") or uuid4().hex
        raw_query = state.get("query", "")
        if trace is not None:
            trace.start(run_id=run_id, trace_id=trace_id, query=raw_query)
        _enter("router", state)

        outcome = pipeline.check_user_input(raw_query, request_context)
        _record_guardrail("input", outcome)
        if not outcome.allowed:
            block_msg = f"输入未通过护栏校验（{outcome.decision.rule_id}）：{outcome.decision.detail}"
            result = {
                "intent": "rag",
                "run_id": run_id,
                "trace_id": trace_id,
                "status": STATUS_BLOCKED,
                "errors": list(state.get("errors", [])) + [block_msg],
                "answer": "",
                "step_count": state.get("step_count", 0) + 1,
            }
            _exit("router", {**state, **result}, error=block_msg)  # type: ignore[typeddict-item]
            return result

        # 放行：后续用护栏处理后的文本（PII 脱敏后不改写结构，只替换敏感片段）
        result = {
            "intent": "rag",
            "run_id": run_id,
            "trace_id": trace_id,
            "status": STATUS_RUNNING,
            "query": outcome.content,
            "step_count": state.get("step_count", 0) + 1,
        }
        _exit("router", {**state, **result})  # type: ignore[typeddict-item]
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
                retry_count=state.get("retry_count", 0),  # type: ignore[arg-type]
                status=state.get("status", STATUS_RUNNING),  # type: ignore[arg-type]
            )
        _exit("retrieve", {**state, **result}, context=context)  # type: ignore[typeddict-item]
        return result

    def _record_cache(disposition, saved: dict | None = None) -> None:
        """把缓存裁定写入 trace（只写 hash/状态/年龄，不写 key 原文或 query 原文）。"""
        if trace is None:
            return
        try:
            trace.record_cache_event(
                node="cache",
                cache_status=disposition.status.value,
                cache_key_hash=disposition.cache_key_hash,
                reason=disposition.reason,
                entry_age_seconds=disposition.entry_age_seconds,
                eligible_for_store=disposition.eligible_for_store,
                saved_input_tokens=(saved or {}).get("input_tokens"),
                saved_output_tokens=(saved or {}).get("output_tokens"),
                saved_cost_usd=(saved or {}).get("cost_usd"),
            )
        except Exception:  # noqa: BLE001, S110 — 观测失败绝不影响执行
            pass

    def generate_node(state: WaterAgentState) -> dict:
        _enter("generate", state)
        query = state.get("query", "")
        context = state.get("context", [])

        # P2.5 精确缓存（review 修复）：lookup 发生在真正 provider 调用之前。
        # 命中后**仍**要过 citation_check 与输出护栏（见 finalize_node），不能直接返回。
        cached_answer = None
        cache_disposition = None
        if cache_service is not None and model_name and context:
            cache_disposition, cached_answer = cache_service.lookup(
                query=query,
                ctx=request_context,
                model=model_name,
                temperature=temperature,
                category=CallCategory.RAG_GENERATION,
            )
            _record_cache(cache_disposition)

        if cached_answer is not None:
            # cache hit：跳过 provider 调用，重建引用声明（后续 citation_check 再验）
            generated_answer = cached_answer.answer
            claims = [
                CitationClaim(source_id=sid, declared_by="system")
                for sid in cached_answer.citation_source_ids
            ]
            gen_latency_ms = 0.0
            cache_hit = True
            generated_citations = []
        else:
            started = time.perf_counter()
            generated = generator.generate(query, context)
            gen_latency_ms = (time.perf_counter() - started) * 1000
            generated_answer = generated.answer
            claims = [
                CitationClaim(source_id=cit.source_id, declared_by="system")
                for cit in generated.citations
            ]
            generated_citations = generated.citations
            cache_hit = False

        result = {
            "answer": generated_answer,
            "citations": claims,
            "cache_hit": cache_hit,
            "step_count": state.get("step_count", 0) + 1,
        }
        if trace is not None:
            if not cache_hit:
                # Step 10 观测：每次生成一条 llm_call；mock 生成器记 0 token
                usage = dict(getattr(generator, "last_usage", None) or {})
                gen_config = getattr(generator, "config", None)
                in_tok = int(usage.get("prompt_tokens", 0) or 0)
                out_tok = int(usage.get("completion_tokens", 0) or 0)
                trace.record_llm_call(
                    node="generate",
                    model=str(getattr(gen_config, "model", "") or type(generator).__name__),
                    provider="deepseek" if usage else "mock",
                    prompt_preview=query,
                    response_preview=generated_answer,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=int(usage.get("total_tokens", in_tok + out_tok) or 0),
                    cost_usd=estimate_deepseek_cost(in_tok, out_tok) if usage else 0.0,
                    latency_ms=gen_latency_ms,
                    query=query,
                )
            trace.record(
                node="generate",
                event="generate_complete",
                query=query,
                context=context,
                citations=generated_citations,
                retry_count=state.get("retry_count", 0),  # type: ignore[arg-type]
                status=state.get("status", STATUS_RUNNING),  # type: ignore[arg-type]
                data={"cache_hit": cache_hit},
            )
        _exit("generate", {**state, **result}, context=context, citations=generated_citations)  # type: ignore[typeddict-item]
        return result

    def citation_check_node(state: WaterAgentState) -> dict:
        """校验引用真实存在于本次检索结果。

        转用 ``validate_citations`` 做集合级确定性校验（行为保持：source_id 集合比较，
        不改评测契约）；invalid 时把可行动错误文本作为结构化 observation 写入
        ``errors``，供 Step 6 回流给模型自纠。
        """
        _enter("citation_check", state)
        claims = list(state.get("citations", []))
        cited_ids = [claim.source_id for claim in claims]
        context_ids = [clause.source_id for clause in state.get("context", [])]
        check = validate_citations(cited_ids, context_ids, version_registry=version_registry)
        bad = check.invalid
        errors = list(state.get("errors", []))
        if check.errors:
            errors.extend(check.errors)
        # Step 8：版本状态写回 claims（未知只标记，不影响 citation_ok）
        enriched = enrich_claims(claims, check)
        if bad:
            result = {"citation_ok": False, "retry_count": state.get("retry_count", 0) + 1, "errors": errors, "query": state.get("query", "").strip(), "citations": enriched}
        else:
            result = {"citation_ok": True, "errors": errors, "citations": enriched}
        if trace is not None:
            trace.record(
                node="citation_check",
                event="citation_check_complete",
                query=state.get("query", ""),
                context=state.get("context", []),
                citations=state.get("citations", []),
                retry_count=result.get("retry_count", state.get("retry_count", 0)),  # type: ignore[arg-type]
                error=errors[-1] if check.errors else None,
                status=state.get("status", STATUS_RUNNING),  # type: ignore[arg-type]
            )
            if bad:
                trace.record(
                    node="citation_check",
                    event="retry",
                    query=state.get("query", ""),
                    context=state.get("context", []),
                    citations=state.get("citations", []),
                    retry_count=result["retry_count"],  # type: ignore[arg-type]
                    error=errors[-1],
                    status=state.get("status", STATUS_RUNNING),  # type: ignore[arg-type]
                )
        _exit("citation_check", {**state, **result}, context=state.get("context", []), citations=state.get("citations", []), error=errors[-1] if check.errors else None)  # type: ignore[typeddict-item]
        return result

    def finalize_node(state: WaterAgentState) -> dict:
        _enter("finalize", state)
        errors = list(state.get("errors", []))

        # 输入护栏已阻断：直接收口 blocked，不再走输出护栏（没有 answer）。
        if state.get("status") == STATUS_BLOCKED:
            result = {"status": STATUS_BLOCKED, "errors": errors, "step_count": state.get("step_count", 0) + 1}
            if trace is not None:
                _exit("finalize", {**state, **result}, error=errors[-1] if errors else None)  # type: ignore[typeddict-item]
                trace.finish(status=STATUS_BLOCKED, error=errors[-1] if errors else None, citations=list(state.get("citations", [])), retry_count=state.get("retry_count", 0))
            return result

        citation_ok = bool(state.get("citation_ok", True))
        has_context = bool(state.get("context"))
        answer = state.get("answer", "")

        # 输出护栏（review 修复）：引用未验证/结构不合规/危险执行指令 → blocked；
        # PII/secret 按 pii_mode 处理（redact 时用处理后文本）。与既有引用/结构校验组合。
        output_outcome = None
        final_answer = answer
        if has_context and citation_ok and answer:
            output_outcome = pipeline.check_output(
                answer,
                request_context,
                citations_verified=citation_ok,
                structured_output_ok=True,
            )
            _record_guardrail("output", output_outcome)
            final_answer = output_outcome.content

        if not has_context:
            errors.append("检索无结果：无法在语料中定位相关条款")
            result = {"status": STATUS_FAILED, "errors": errors, "step_count": state.get("step_count", 0) + 1}
        elif not citation_ok:
            errors.append("达到引用校验重试上限，仍有无效引用")
            result = {"status": STATUS_FAILED, "errors": errors, "step_count": state.get("step_count", 0) + 1}
        elif output_outcome is not None and not output_outcome.allowed:
            errors.append(f"输出未通过护栏校验（{output_outcome.decision.rule_id}）：{output_outcome.decision.detail}")
            result = {"status": STATUS_BLOCKED, "errors": errors, "answer": "", "step_count": state.get("step_count", 0) + 1}
        else:
            result = {"status": STATUS_SUCCEEDED, "errors": errors, "answer": final_answer, "step_count": state.get("step_count", 0) + 1}

        # 精确缓存写入（review 修复）：只在 provider 真实生成（非命中）、且引用与输出护栏
        # 均通过时，按既有资格条件（temperature=0/只读 RAG/版本已知/无阻断无脱敏等）写入。
        # 命中时不重复写；版本 unknown 时 eligibility 为 INELIGIBLE（fail closed）。
        if (
            cache_service is not None
            and model_name
            and not state.get("cache_hit", False)
            and result["status"] == STATUS_SUCCEEDED
            and output_outcome is not None
            and output_outcome.allowed
        ):
            try:
                source_ids = [c.source_id for c in state.get("citations", [])]
                eligibility = cache_service.eligibility_for(
                    category=CallCategory.RAG_GENERATION,
                    temperature=temperature,
                    citation_verified=citation_ok,
                    output_guardrails_passed=output_outcome.allowed,
                    answer=final_answer,
                    source_ids=source_ids,
                    ctx=request_context,
                    calc_result_present=False,
                    contains_redacted_or_blocked=output_outcome.redacted,
                )
                disposition = cache_service.store(
                    query=state.get("query", ""),
                    ctx=request_context,
                    model=model_name,
                    temperature=temperature,
                    answer=final_answer,
                    source_ids=source_ids,
                    eligibility=eligibility,
                    category=CallCategory.RAG_GENERATION,
                )
                _record_cache(disposition)
            except Exception:  # noqa: BLE001, S110 — 缓存失败不影响主流程
                pass

        if trace is not None:
            if result["status"] in (STATUS_FAILED, STATUS_BLOCKED):
                trace.record(node="finalize", event="error", query=state.get("query", ""), context=state.get("context", []), citations=state.get("citations", []), retry_count=state.get("retry_count", 0), error=errors[-1] if errors else None, status=result["status"])
            _exit("finalize", {**state, **result}, context=state.get("context", []), citations=state.get("citations", []), error=errors[-1] if result["status"] in (STATUS_FAILED, STATUS_BLOCKED) else None)  # type: ignore[typeddict-item]
            trace.finish(status=result["status"], error=errors[-1] if result["status"] in (STATUS_FAILED, STATUS_BLOCKED) else None, context=state.get("context", []), citations=state.get("citations", []), retry_count=state.get("retry_count", 0),  # type: ignore[arg-type]
            )
        return result

    def route_after_router(state: WaterAgentState) -> str:
        """输入护栏阻断 → 直接 finalize；否则进入检索。"""
        if state.get("status") == STATUS_BLOCKED:
            return "finalize"
        return "retrieve"

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
    graph.add_conditional_edges(
        "router",
        route_after_router,
        {"retrieve": "retrieve", "finalize": "finalize"},
    )
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "citation_check")
    graph.add_conditional_edges(
        "citation_check",
        route_after_citation,
        {"retrieve": "retrieve", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)

    return graph.compile()

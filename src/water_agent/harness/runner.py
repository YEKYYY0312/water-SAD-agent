"""JSONL Harness 评测跑分器（Step 10 双轨评测）。

机制层（``variant=mock``，默认）：InMemoryRetriever + MockGenerator —— 离线、确定性、PR 门禁。
质量层（``variant=real``，需 Key）：真实 TriviumDB 索引 + 真实 Embedding + 真实 LLM + 可选 rerank + 可选 LLM-as-judge —— 无 Key 时结构化 SKIP。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from water_agent.domain.models import (
    CalcCase,
    EvalReport,
    EvalResult,
    TokenUsage,
)
from water_agent.domain.state import default_state
from water_agent.generation.cost import estimate_deepseek_cost
from water_agent.generation.llm import DEFAULT_SYSTEM_PROMPT, build_llm_generator
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.calc import build_calc_graph
from water_agent.graphs.rag import build_rag_graph
from water_agent.harness.doubles import BadCitationGenerator
from water_agent.harness.llm_judge import LLMJudge
from water_agent.harness.loader import EvalLoadBundle, EvalLoadError, load_eval_bundle
from water_agent.harness.retrieval_metrics import aggregate_retrieval_metrics
from water_agent.harness.scorer import score_calc_case, score_eval_case
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.prompting import PromptAssembler
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.tools.hydraulics import run_calc

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVAL_REPORT_DIR = REPO_ROOT / "data" / "eval" / "reports"
DEFAULT_TRACE_DIR = REPO_ROOT / "data" / "eval" / "traces"
DEFAULT_FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
DEFAULT_INDEX_DIR = REPO_ROOT / "data" / "indexes"
DEFAULT_DB_DIMENSIONS = 512
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_JUDGE_MODEL = "deepseek-chat"
DEFAULT_JUDGE_BASE_URL = "https://api.deepseek.com"
DEFAULT_JUDGE_API_KEY_ENV = "DEEPSEEK_API_KEY"

# 退出码语义（与 SKIP 明确区分）：
#   0 全部通过或全部 SKIP；1 有用例失败；2 评测集加载问题；3 有 Key 但真实组件构建失败
EXIT_REAL_COMPONENTS_FAILED = 3


class RealComponentsError(RuntimeError):
    """有 Key 但真实组件（索引/embedding/模型/judge）构建失败——是失败，不是 SKIP。"""


def _pct(values: list[float], percent: float) -> float:
    """计算百分位数（无 numpy 依赖）。"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * percent / 100)))
    return sorted_vals[k]


def _pick_index(db_path: str | Path | None = None) -> Path | None:
    """选择真实 TriviumDB 索引路径。优先显式指定，否则取 data/indexes 下最新 db。"""
    if db_path and Path(db_path).exists():
        return Path(db_path)
    candidates = sorted(Path(DEFAULT_INDEX_DIR).glob("*.db")) if DEFAULT_INDEX_DIR.exists() else []
    return candidates[-1] if candidates else None


class HarnessRunner:
    """执行 JSONL 评测集并生成报告。"""

    def __init__(
        self,
        *,
        eval_path: str | Path,
        report_dir: str | Path = DEFAULT_EVAL_REPORT_DIR,
        trace_dir: str | Path = DEFAULT_TRACE_DIR,
        fixture_path: str | Path = DEFAULT_FIXTURE_PATH,
        # Step 10：双轨评测扩展
        variant: str | None = None,  # None = 按用例级 variant 路由；"mock"|"trivium"|"real" 可覆盖
        top_k: int = 5,
        recall_top_k: int = 20,
        hybrid_alpha: float = 0.7,
        db_path: str | Path | None = None,
        dimensions: int | None = None,
        rerank: str = "local",
        rerank_model: str | None = None,
        rerank_base_url: str | None = None,
        rerank_api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        judge: bool = False,
        judge_model: str = DEFAULT_JUDGE_MODEL,
        judge_base_url: str = DEFAULT_JUDGE_BASE_URL,
        judge_api_key_env: str = DEFAULT_JUDGE_API_KEY_ENV,
    ) -> None:
        self.eval_path = Path(eval_path)
        self.report_dir = Path(report_dir)
        self.trace_dir = Path(trace_dir)
        self.fixture_path = Path(fixture_path)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._clauses = load_clauses_jsonl(self.fixture_path)
        self._retriever = InMemoryRetriever(self._clauses)
        self._trivium_retriever: TriviumRetriever | None = None
        # Step 10 扩展
        self.variant = variant  # 运行级变体覆盖
        self.top_k = top_k
        self.recall_top_k = recall_top_k
        self.hybrid_alpha = hybrid_alpha
        self.db_path = Path(db_path) if db_path else None
        self.dimensions = dimensions
        self.rerank = rerank
        self.rerank_model = rerank_model
        self.rerank_base_url = rerank_base_url
        self.rerank_api_key = rerank_api_key
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.judge = judge
        self.judge_model = judge_model
        self.judge_base_url = judge_base_url
        self.judge_api_key_env = judge_api_key_env
        # 真实组件缓存（惰性构建）
        self._real_retriever: TriviumRetriever | None = None
        self._real_generator = None
        self._real_skip_reason: str | None = None
        self._real_judge: LLMJudge | None = None

    # ── 真实组件装配 ────────────────────────────────────────────────

    def _probe_real_skip(self) -> str | None:
        """检测真实质量层运行前置条件，返回跳过原因（None=可运行）。"""
        deepseek = os.environ.get(self.api_key_env, "").strip()
        dashscope = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        missing: list[str] = []
        if not deepseek:
            missing.append(self.api_key_env)
        if not dashscope:
            missing.append("DASHSCOPE_API_KEY")
        if missing:
            return f"SKIP: 缺少 API Key: {', '.join(missing)}。真实质量层需配置 DASHSCOPE_API_KEY（embedding）与 {self.api_key_env}（LLM）。"
        index = _pick_index(self.db_path)
        if index is None:
            return "SKIP: 无 TriviumDB 索引（data/indexes/ 下无可用的 .db 文件）。"
        return None

    def _build_real_retriever(self) -> TriviumRetriever:
        """构建真实 TriviumDB 检索器（需 DASHSCOPE_API_KEY 与索引）。"""
        import triviumdb

        from water_agent.embedding import build_embedding_provider

        index = _pick_index(self.db_path)
        assert index is not None, "real retriever requires an index"
        dim = self.dimensions
        if dim is None:
            probe = triviumdb.TriviumDB(str(index))
            try:
                dim = int(probe.dim())
            finally:
                probe.close()
        dim = dim or DEFAULT_DB_DIMENSIONS
        provider = build_embedding_provider(dimensions=dim)
        return TriviumRetriever(
            db_path=index,
            embedding_fn=provider.embed,
            hybrid_alpha=self.hybrid_alpha,
            dim=dim,
            fallback_clauses=self._clauses,
        )

    def _build_real_generator(self):
        """构建真实 LLM 生成器（需 DEEPSEEK_API_KEY）。"""
        assembler = PromptAssembler()
        sys_content = assembler.assemble_prompt().content
        system_prompt = sys_content if isinstance(sys_content, str) else DEFAULT_SYSTEM_PROMPT
        return build_llm_generator(
            model=self.model,
            base_url=self.base_url,
            system_prompt=system_prompt,
        )

    def _ensure_real_components(self):
        """惰性初始化真实组件。如不可用，设置 ``_real_skip_reason``。"""
        if self._real_skip_reason is not None:
            return
        if self._real_retriever is not None and self._real_generator is not None:
            return
        skip = self._probe_real_skip()
        if skip:
            self._real_skip_reason = skip
            return
        try:
            self._real_retriever = self._build_real_retriever()
            self._real_generator = self._build_real_generator()
            if self.judge:
                self._real_judge = LLMJudge(
                    judge_model=self.judge_model,
                    judge_base_url=self.judge_base_url,
                    judge_api_key_env=self.judge_api_key_env,
                )
        except Exception as exc:
            raise RealComponentsError(f"真实组件构建失败（有 Key，非 SKIP）：{exc}") from exc

    # ── 检索器与生成器工厂 ──────────────────────────────────────────

    def _retriever_for(self, variant: str):
        if variant == "real":
            self._ensure_real_components()
            if self._real_skip_reason:
                return None
            return self._real_retriever
        if variant == "trivium":
            if self._trivium_retriever is None:
                self._trivium_retriever = TriviumRetriever(
                    db_path=None,
                    fallback_clauses=self._clauses,
                )
            return self._trivium_retriever
        return self._retriever

    def _generator_for(self, variant: str):
        if variant == "real":
            self._ensure_real_components()
            if self._real_skip_reason:
                return None
            return self._real_generator
        if variant == "bad_citation":
            return BadCitationGenerator()
        return MockGenerator()

    def _reranker_for(self, variant: str):
        """根据 variant 和配置返回 reranker 实例。"""
        if variant != "real":
            return None
        if self.rerank == "local":
            from water_agent.retrieval.rerank import LocalReranker
            return LocalReranker()
        if self.rerank == "bailian":
            from water_agent.retrieval.rerank import BailianReranker, build_bailian_reranker
            return build_bailian_reranker(
                api_key=self.rerank_api_key,
                model=self.rerank_model or BailianReranker.DEFAULT_MODEL,
                base_url=self.rerank_base_url or BailianReranker.DEFAULT_BASE_URL,
            )
        return None

    # ── 评测执行 ────────────────────────────────────────────────────

    def _effective_variant(self, case) -> str:
        """返回真实生效的变体：运行级覆盖优先，否则用例级。"""
        if self.variant and self.variant != "auto":
            return self.variant
        return getattr(case, "variant", "mock")

    def _skip_result(self, case, *, reason: str, line_no: int | None = None) -> EvalResult:
        """生成结构化 SKIP 结果（不参与 pass/fail 统计）。"""
        return EvalResult(
            case_id=case.id,
            passed=False,
            expected_status=getattr(case, "expected_status", ""),
            actual_status="skipped",
            failure_reason=reason,
            line_no=line_no,
            variant=self._effective_variant(case),
            skipped=True,
        )

    def _run_calc_case(self, case: CalcCase, *, line_no: int | None = None) -> EvalResult:
        """calc 端到端：走计算子图（真工具 + 确定性解释器），判分仍用 Step 1 run_calc。

        子图 compute 内部调 run_calc 使用与评测一致的输入，故数值与直接判分同源；
        缺参/非法路径可能短路（无 compute 产物），回退 run_calc 取结构化 CalcResult，
        保证判分确定性与 Step 1 一致。
        """
        app = build_calc_graph()
        state: dict = {
            "query": case.query or "",
            "intent": "calc",
            "formula": case.formula,
            "params": dict(case.input),
            "calc_result": None,
            "missing_inputs": [],
            "assumptions": [],
            "errors": [],
            "candidates": [],
            "answer": "",
            "status": "running",
            "step_count": 0,
        }
        started = time.perf_counter()
        graph_result = app.invoke(state)
        latency_ms = (time.perf_counter() - started) * 1000
        calc_result = graph_result.get("calc_result")
        if calc_result is None:
            calc_result = run_calc(case.formula, case.input)
        result = score_calc_case(case, calc_result=calc_result, latency_ms=latency_ms, line_no=line_no)
        result.variant = self._effective_variant(case)
        return result

    def _run_case(self, case, *, line_no: int | None = None):
        if isinstance(case, CalcCase):
            return self._run_calc_case(case, line_no=line_no)

        _variant = self._effective_variant(case)

        # SKIP 预检
        if _variant == "real":
            self._ensure_real_components()
            if self._real_skip_reason:
                return self._skip_result(case, reason=self._real_skip_reason, line_no=line_no)

        # 装配图组件
        retriever = self._retriever_for(_variant)
        generator = self._generator_for(_variant)
        if retriever is None or generator is None:
            return self._skip_result(case, reason=f"SKIP: {_variant} 组件不可用", line_no=line_no)

        reranker = self._reranker_for(_variant)

        trace_id = uuid4().hex
        run_id = uuid4().hex
        recorder = JsonTraceRecorder(self.trace_dir)
        app = build_rag_graph(
            retriever, generator,
            trace=recorder,
            reranker=reranker,
            top_k=self.top_k if _variant == "real" else getattr(case, "top_k", 5),
            recall_top_k=self.recall_top_k if _variant == "real" else 20,
        )
        state = default_state(query=case.query, run_id=run_id, trace_id=trace_id)
        started = time.perf_counter()
        result: dict = app.invoke(state)
        latency_ms = (time.perf_counter() - started) * 1000

        actual_citations = [citation.source_id for citation in result.get("citations", [])]
        context_articles = [clause.source_id for clause in result.get("context", [])]
        retry_count = int(result.get("retry_count", 0))

        # 评分
        scored = score_eval_case(
            case,
            actual_status=result.get("status", ""),
            actual_citations=actual_citations,
            context_articles=context_articles,
            latency_ms=latency_ms,
            trace_id=result.get("trace_id", trace_id),
            retry_count=retry_count,
            line_no=line_no,
        )
        scored.variant = _variant

        # 真实生成器 token 用量
        gen_usage = getattr(generator, "last_usage", None) or {}
        if gen_usage.get("prompt_tokens") or gen_usage.get("completion_tokens"):
            inp = int(gen_usage.get("prompt_tokens", 0) or 0)
            out = int(gen_usage.get("completion_tokens", 0) or 0)
            scored.token_usage = TokenUsage(
                input_tokens=inp,
                output_tokens=out,
                total_tokens=inp + out,
                cost_usd=estimate_deepseek_cost(inp, out),
            )

        # 回答质量 Judge（仅 variant=real 且 --judge 启用时）
        if self.judge and _variant == "real" and self._real_judge is not None:
            judge_result = self._real_judge.evaluate(
                case.query,
                result.get("answer", ""),
                context=[c.text for c in result.get("context", [])] if result.get("context") else None,
            )
            if judge_result.scores:
                scored.judge_scores = {k: v.score for k, v in judge_result.scores.items()}
                scored.judge_reasoning = json.dumps(
                    {k: v.reasoning for k, v in judge_result.scores.items()},
                    ensure_ascii=False,
                )
            # judge token 用量合并到 case token_usage
            if judge_result.token_usage:
                jt = judge_result.token_usage
                if scored.token_usage is None:
                    scored.token_usage = jt
                else:
                    scored.token_usage.input_tokens += jt.input_tokens
                    scored.token_usage.output_tokens += jt.output_tokens
                    scored.token_usage.total_tokens += jt.total_tokens
                    scored.token_usage.cost_usd = round(
                        scored.token_usage.cost_usd + jt.cost_usd, 8
                    )

        return scored

    def _error_result(self, case, *, line_no: int | None, exc: BaseException) -> EvalResult:
        """单用例崩溃兜底：标记 failed + actual_status=error，堆栈进 report 与 trace。"""
        traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            (self.trace_dir / f"{case.id}_error.trace").write_text(traceback_text, encoding="utf-8")
        except OSError:
            pass
        return EvalResult(
            case_id=case.id,
            passed=False,
            expected_status=getattr(case, "expected_status", ""),
            actual_status="error",
            failure_reason=f"{type(exc).__name__}: {exc}",
            exception_trace=traceback_text,
            line_no=line_no,
            variant=self._effective_variant(case),
        )

    def run_bundle(self, bundle: EvalLoadBundle):
        results: list[EvalResult] = []
        for index, case in enumerate(bundle.cases):
            line_no = bundle.line_nos[index] if index < len(bundle.line_nos) else None
            try:
                results.append(self._run_case(case, line_no=line_no))
            except RealComponentsError:
                raise  # 整批都跑不了，直接上抛让 main 用独立退出码表达
            except Exception as exc:  # noqa: BLE001 — 单用例隔离
                results.append(self._error_result(case, line_no=line_no, exc=exc))

        load_issues = [
            {"line_no": issue.line_no, "error": issue.error, "raw": issue.raw}
            for issue in bundle.issues
        ]

        # 聚合统计（排除 skipped 用例）
        non_skipped = [r for r in results if not r.skipped]
        skipped = len(results) - len(non_skipped)
        passed = sum(1 for r in non_skipped if r.passed)
        total = len(results)
        failed = total - passed - skipped
        pass_rate = (passed / len(non_skipped)) if non_skipped else 0.0

        # 引用正确率（非 skipped 用例中，引用全部在上下文中）
        def _citation_ok(r: EvalResult) -> bool:
            ctx = set(r.context_articles)
            return all(c in ctx for c in r.actual_citations) if r.actual_citations else True
        citation_ok_count = sum(1 for r in non_skipped if _citation_ok(r))
        citation_ok_rate = (citation_ok_count / len(non_skipped)) if non_skipped else 0.0

        # 延迟百分位
        latencies = [r.latency_ms for r in non_skipped if r.latency_ms > 0]
        avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
        p50 = _pct(latencies, 50) if latencies else 0.0
        p95 = _pct(latencies, 95) if latencies else 0.0

        # 检索指标聚合
        retrieval_metrics = aggregate_retrieval_metrics(
            [r.retrieval_metrics for r in non_skipped if r.retrieval_metrics]
        )

        # 成本 & token 汇总
        total_cost = sum(
            (r.token_usage.cost_usd if r.token_usage else 0.0) for r in non_skipped
        )
        total_tokens = sum(
            (r.token_usage.total_tokens if r.token_usage else 0) for r in non_skipped
        )

        _variant = self.variant or "per-case"
        report = EvalReport(
            total=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            pass_rate=pass_rate,
            cases=results,
            input_path=str(self.eval_path),
            output_path="",
            variant=_variant,
            citation_ok_rate=citation_ok_rate,
            total_cost_usd=round(total_cost, 6),
            total_tokens=total_tokens,
            avg_latency_ms=avg_latency,
            p50_latency_ms=p50,
            p95_latency_ms=p95,
            retrieval_metrics=retrieval_metrics,
        )
        report_path = self._write_report(report, load_issues=load_issues)
        report.output_path = str(report_path)
        return report, report_path, load_issues

    def run(self):
        bundle = load_eval_bundle(self.eval_path, strict=False)
        return self.run_bundle(bundle)

    def _write_report(self, report: EvalReport, *, load_issues: list[dict]) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.report_dir / f"{self.eval_path.stem}_{stamp}.json"
        payload = report.model_dump(mode="json")
        payload["load_issues"] = load_issues
        payload["created_at"] = report.created_at.isoformat()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


# ── CLI ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the water_agent JSONL harness")
    p.add_argument("eval_path", help="Path to a JSONL evaluation file")
    p.add_argument("--report-dir", default=str(DEFAULT_EVAL_REPORT_DIR), help="Directory for report files")
    p.add_argument("--trace-dir", default=str(DEFAULT_TRACE_DIR), help="Directory for trace files")
    p.add_argument("--fixture-path", default=str(DEFAULT_FIXTURE_PATH), help="Retriever fixture JSONL path")
    p.add_argument("--variant", choices=["mock", "trivium", "real"], default=None,
                   help="运行级变体：mock（机制层，默认按用例级路由），real（质量层，需 Key）")
    p.add_argument("--top-k", type=int, default=5, help="rerank 后返回条数")
    p.add_argument("--recall-top-k", type=int, default=20, help="混合检索召回的候选条数")
    p.add_argument("--hybrid-alpha", type=float, default=0.7)
    p.add_argument("--db-path", default=None, help="TriviumDB 索引路径（默认用 data/indexes 下最新库）")
    p.add_argument("--dimensions", type=int, default=None, choices=[256, 512, 768, 1024], help="embedding 维度")
    p.add_argument("--rerank", choices=["local", "bailian", "none"], default="local", help="rerank 策略")
    p.add_argument("--model", default=DEFAULT_MODEL, help="LLM 模型名")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="LLM base_url")
    p.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV, help="真实模型 API Key 环境变量名")
    p.add_argument("--judge", action="store_true", help="启用 LLM-as-judge 回答质量评分（仅 --variant real 有效）")
    p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help="Judge 模型名")
    p.add_argument("--judge-base-url", default=DEFAULT_JUDGE_BASE_URL, help="Judge base_url")
    p.add_argument("--judge-api-key-env", default=DEFAULT_JUDGE_API_KEY_ENV, help="Judge API Key 环境变量名")
    return p



def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        runner = HarnessRunner(
            eval_path=args.eval_path,
            report_dir=args.report_dir,
            trace_dir=args.trace_dir,
            fixture_path=args.fixture_path,
            variant=args.variant,
            top_k=args.top_k,
            recall_top_k=args.recall_top_k,
            hybrid_alpha=args.hybrid_alpha,
            db_path=args.db_path,
            dimensions=args.dimensions,
            rerank=args.rerank,
            model=args.model,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
            judge=args.judge,
            judge_model=args.judge_model,
            judge_base_url=args.judge_base_url,
            judge_api_key_env=args.judge_api_key_env,
        )
        report, report_path, load_issues = runner.run()
    except RealComponentsError as exc:
        print(
            json.dumps({"ok": False, "kind": "real_components_failed", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return EXIT_REAL_COMPONENTS_FAILED
    except (FileNotFoundError, EvalLoadError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    output = {
        "ok": True,
        "variant": runner.variant or "per-case",
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "skipped": report.skipped,
        "pass_rate": report.pass_rate,
        "citation_ok_rate": report.citation_ok_rate,
        "total_cost_usd": report.total_cost_usd,
        "total_tokens": report.total_tokens,
        "avg_latency_ms": report.avg_latency_ms,
        "report_path": str(report_path),
        "load_issues": load_issues,
    }
    # SKIP 全量时添加标记
    if report.skipped == report.total and report.skipped > 0:
        output["skipped_all"] = True
        if report.cases and report.cases[0].failure_reason:
            output["skip_reason"] = report.cases[0].failure_reason

    print(json.dumps(output, ensure_ascii=False, indent=2))
    if load_issues:
        return 2
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
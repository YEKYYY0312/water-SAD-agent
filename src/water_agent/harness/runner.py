"""JSONL Harness 评测跑分器（Step 10 双轨评测）。

机制层（``variant=mock``，默认）：InMemoryRetriever + MockGenerator —— 离线、确定性、PR 门禁。
质量层（``variant=real``，需 Key）：真实 TriviumDB 索引 + 真实 Embedding + 真实 LLM + 可选 rerank + 可选 LLM-as-judge —— 无 Key 时结构化 SKIP。

真实变体默认走 **agentic 图**（``--real-graph agentic``）：模型经 ``cite_clauses`` 自己声明引用，
校验器真判。legacy 图（``--real-graph legacy``）的引用是系统从 context 前 N 条代填的，
citation_ok 恒真、pass_rate 实际等于检索 recall@k——保留仅供改造前后对比（workbench benchmark）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from water_agent.docgen import (
    RealPolishTransport,
    render_docgen_doc,
    run_quality_polish,
    strip_polish_meta_lines,
)
from water_agent.docgen.quality import PolishTransport
from water_agent.domain.models import (
    CalcCase,
    DocgenCase,
    DocgenQualityCase,
    EvalReport,
    EvalResult,
    GovernanceCase,
    MaterialCase,
    TokenUsage,
)
from water_agent.domain.state import default_state
from water_agent.generation.chat_model import build_chat_model
from water_agent.generation.cost import estimate_deepseek_cost
from water_agent.generation.llm import DEFAULT_SYSTEM_PROMPT, build_llm_generator
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.agent_rag import _extract_validation_signal, build_agentic_app
from water_agent.graphs.calc import build_calc_graph
from water_agent.graphs.rag import build_rag_graph
from water_agent.harness.doubles import BadCitationGenerator
from water_agent.harness.llm_judge import LLMJudge
from water_agent.harness.loader import EvalLoadBundle, EvalLoadError, load_eval_bundle
from water_agent.harness.retrieval_metrics import aggregate_retrieval_metrics
from water_agent.harness.scorer import (
    score_calc_case,
    score_docgen_case,
    score_docgen_quality_case,
    score_eval_case,
    score_governance_case,
    score_material_case,
)
from water_agent.materials import aggregate_materials
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.prompting import PromptAssembler
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.rerank import RerankingRetriever
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.runtime.governance.binding import (
    default_gateway_with_sink,
    local_request_context,
)
from water_agent.tools.calc_velocity import calc_velocity_spec
from water_agent.tools.cite import build_cite_clauses_tool
from water_agent.tools.ds_adapters import tool_spec_to_base_tool
from water_agent.tools.hydraulics import run_calc
from water_agent.tools.material_stats import material_stats_spec
from water_agent.tools.read_drawing import maybe_build_read_drawing_tool
from water_agent.tools.registry import ToolRegistry
from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

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

# P4 Step 25 性能优化：模块级 MCP 装配缓存，避免评测时每条用例重复 I/O
_MCP_ASSEMBLY_CACHE: tuple[object, float] | None = None  # (assembly, config_mtime)
_MCP_CONFIG_PATH = REPO_ROOT / "data" / "mcp_servers.json"


def _get_or_create_mcp_assembly(trace: object | None = None) -> object | None:
    """获取或创建 MCP 装配，配置文件 mtime 变化时重建（模块级缓存）。"""
    global _MCP_ASSEMBLY_CACHE

    if not _MCP_CONFIG_PATH.exists():
        return None

    try:
        current_mtime = _MCP_CONFIG_PATH.stat().st_mtime
    except OSError:
        return None

    # 缓存命中且配置未变化
    if _MCP_ASSEMBLY_CACHE is not None:
        cached_assembly, cached_mtime = _MCP_ASSEMBLY_CACHE
        if cached_mtime == current_mtime:
            return cached_assembly

    # 缓存未命中或配置已变化，重新装配
    try:
        from water_agent.mcp.wiring import load_mcp_gateway, open_gateway_sessions
        mcp_cfg = load_mcp_gateway()
        mcp_assembly = open_gateway_sessions(mcp_cfg)
        _MCP_ASSEMBLY_CACHE = (mcp_assembly, current_mtime)
        return mcp_assembly
    except Exception:  # noqa: BLE001
        return None


class RealComponentsError(RuntimeError):
    """有 Key 但真实组件（索引/embedding/模型/judge）构建失败——是失败，不是 SKIP。"""


def _pct(values: list[float], percent: float) -> float:
    """计算百分位数（无 numpy 依赖）。"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * percent / 100)))
    return sorted_vals[k]


def citation_ok_for_result(r: EvalResult) -> bool:
    """单条结果的引用正确语义（P1/Step 10 评审收口）。

    - 引用全部 ∈ 本次检索上下文 → ok；
    - 零引用：对存在 ``expected_articles``（gold 要求引用）的 RAG 用例必须计为
      citation failure（与 agentic 零引用强制收口同语义）；不需要引用的用例
      （无 gold 引用要求）零引用不误伤。
    """
    if not r.actual_citations:
        return not bool(r.expected_articles)
    ctx = set(r.context_articles)
    return all(c in ctx for c in r.actual_citations)


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
        real_graph: str = "agentic",
        max_tool_turns: int = 10,
    ) -> None:
        if real_graph not in {"agentic", "legacy"}:
            raise ValueError(f"real_graph 必须是 agentic|legacy，得到 {real_graph!r}")
        self.real_graph = real_graph
        self.max_tool_turns = max_tool_turns
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
        self._real_chat_model = None
        self._real_skip_reason: str | None = None
        self._real_judge: LLMJudge | None = None
        self._real_polisher: PolishTransport | None = None

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

    def _build_real_chat_model(self):
        """构建真实 chat model（ChatOpenAI/DeepSeek function calling，agentic 图用）。"""
        return build_chat_model(model=self.model, base_url=self.base_url, api_key_env=self.api_key_env)

    def _ensure_real_components(self):
        """惰性初始化真实组件。缺 Key/索引 → ``_real_skip_reason``；建不起来 → RealComponentsError。"""
        if self._real_skip_reason is not None:
            return
        if self._real_retriever is not None and (self._real_generator is not None or self._real_chat_model is not None):
            return
        skip = self._probe_real_skip()
        if skip:
            self._real_skip_reason = skip
            return
        try:
            self._real_retriever = self._build_real_retriever()
            # 关键真实链路防线：真实检索器若已回落 fixture（is_fallback）或带 db_error，
            # 说明真实索引不可用，拒绝在 fixture 语料上跑"真实"评测——否则结果会被污染成
            # 机制层分数而误报为真实质量（code review 问题 1b）。
            # 注：测试注入的检索器替身（如 _FixedOrderRetriever）无这些属性，按无缺陷处理。
            fallback = bool(getattr(self._real_retriever, "is_fallback", False))
            db_error = getattr(self._real_retriever, "db_error", None)
            if fallback:
                raise RealComponentsError(
                    "真实检索器回落 fixture（is_fallback=True）；"
                    f"db_error={db_error!r}，"
                    "真实评测必须在真实索引上运行，拒绝用 fixture 语料跑 --variant real"
                )
            if db_error:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "warning": "真实检索器带缺陷运行（db_error 非空，检索结果可能不完整）",
                            "db_error": db_error,
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
            if self.real_graph == "agentic":
                self._real_chat_model = self._build_real_chat_model()
            else:
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

    def _run_docgen_case(self, case: DocgenCase, *, line_no: int | None = None) -> EvalResult:
        """docgen 机制层：模板引擎确定性渲染（与 docgen 图 draft/finalize 同一数据源）。

        未知文档类型/渲染异常 → 传 ``render_error`` 给 scorer，由 scorer 按
        ``expected_status`` 判 invalid_input（成功/失败语义单点化，不在 runner 旁路）。
        """
        started = time.perf_counter()
        try:
            rendered = render_docgen_doc(
                query=case.query,
                doc_type=case.doc_type,
                calc_result=case.calc_result,
                citations=case.citations,
            )
        except ValueError as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            result = score_docgen_case(
                case,
                rendered=None,
                render_error=str(exc),
                latency_ms=latency_ms,
                line_no=line_no,
            )
            result.variant = "programmatic"
            return result
        latency_ms = (time.perf_counter() - started) * 1000
        result = score_docgen_case(case, rendered=rendered, latency_ms=latency_ms, line_no=line_no)
        result.variant = "programmatic"
        return result

    def _run_material_case(self, case: MaterialCase, *, line_no: int | None = None) -> EvalResult:
        """material 机制层：管段表程序化聚合（与 material_stats 工具同源），判分全确定。"""
        started = time.perf_counter()
        table = aggregate_materials(case.segments)
        latency_ms = (time.perf_counter() - started) * 1000
        result = score_material_case(case, table=table, latency_ms=latency_ms, line_no=line_no)
        result.variant = "programmatic"
        return result

    @staticmethod
    def _offline_polisher(mode: str) -> PolishTransport:
        """质量套件脚本化传输：只用于离线验证管线与自检，不冒充真实 LLM。"""

        def polish(_system: str, user: str) -> str:
            lines = strip_polish_meta_lines(user)
            text = "\n".join(lines)
            if mode == "identity":
                return text
            if mode == "paraphrase":
                return text.replace("项目 / 查询：", "项目说明：").replace(
                    "供人工复核后确认", "应由专业人员复核确认"
                )
            if mode == "mutate_number":
                return re.sub(r"(=\s*)-?\d+(?:\.\d+)?", r"\g<1>999", text, count=1)
            if mode == "inject_citation":
                if lines:
                    lines[0] += "；另见 FIX 9.9.9"
                return "\n".join(lines)
            if mode == "drop_line":
                return "\n".join(lines[:-1])
            raise ValueError(f"未知 polish_mode: {mode}")

        return polish

    def _build_real_polisher(self) -> PolishTransport:
        return RealPolishTransport(
            model=self.model,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
        )

    def _run_docgen_quality_case(
        self,
        case: DocgenQualityCase,
        *,
        line_no: int | None = None,
    ) -> EvalResult:
        """docgen 质量层：结构化底座 → 受限润色 → 确定性自检 → 质量结论。"""
        started = time.perf_counter()
        try:
            rendered = render_docgen_doc(
                query=case.query,
                doc_type=case.doc_type,
                calc_result=case.calc_result,
                citations=case.citations,
            )
        except ValueError as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            return EvalResult(
                case_id=case.id,
                passed=False,
                expected_status=case.expected_verdict,
                actual_status="invalid_input",
                failure_reason=f"docgen quality base render failed: {exc}",
                latency_ms=round(latency_ms, 3),
                line_no=line_no,
                variant=self._effective_variant(case),
            )

        variant = self._effective_variant(case)
        if variant == "real":
            if not os.environ.get(self.api_key_env, "").strip():
                verdict = run_quality_polish(rendered, api_key_env=self.api_key_env)
            else:
                if self._real_polisher is None:
                    self._real_polisher = self._build_real_polisher()
                verdict = run_quality_polish(
                    rendered,
                    polish=self._real_polisher,
                    api_key_env=self.api_key_env,
                    used_real_model=True,
                )
        else:
            verdict = run_quality_polish(
                rendered,
                polish=self._offline_polisher(case.polish_mode),
            )

        latency_ms = (time.perf_counter() - started) * 1000
        result = score_docgen_quality_case(
            case,
            verdict=verdict,
            latency_ms=latency_ms,
            line_no=line_no,
        )
        result.variant = variant
        if variant == "real" and not verdict.skipped and verdict.verdict != "blocked":
            result.quality_pass = verdict.ok
        if variant == "real":
            usage = getattr(self._real_polisher, "last_usage", None)
            if isinstance(usage, TokenUsage):
                result.token_usage = usage.model_copy(deep=True)
        return result

    def _run_governance_case(
        self, case: GovernanceCase, *, line_no: int | None = None
    ) -> EvalResult:
        """驱动治理场景并判分（P2.5 Step 15）。

        全离线、无 Key 依赖，因此**永不 SKIP** —— 规格 §7 要求 guardrail/cost/cache
        核心用例不得 SKIP。场景执行异常由 scorer 计为失败（不是 SKIP）。
        """
        from water_agent.runtime.governance.scenarios import run_scenario

        started = time.perf_counter()
        outcome = run_scenario(case.scenario)
        latency_ms = (time.perf_counter() - started) * 1000
        scored = score_governance_case(
            case, outcome=outcome, latency_ms=latency_ms, line_no=line_no
        )
        scored.variant = "programmatic"
        return scored

    def _run_case(self, case, *, line_no: int | None = None):
        if isinstance(case, CalcCase):
            return self._run_calc_case(case, line_no=line_no)
        if isinstance(case, DocgenCase):
            return self._run_docgen_case(case, line_no=line_no)
        if isinstance(case, DocgenQualityCase):
            return self._run_docgen_quality_case(case, line_no=line_no)
        if isinstance(case, MaterialCase):
            return self._run_material_case(case, line_no=line_no)
        if isinstance(case, GovernanceCase):
            return self._run_governance_case(case, line_no=line_no)

        _variant = self._effective_variant(case)

        # SKIP 预检
        if _variant == "real":
            self._ensure_real_components()
            if self._real_skip_reason:
                return self._skip_result(case, reason=self._real_skip_reason, line_no=line_no)
            if self.real_graph == "agentic":
                return self._run_real_agentic_case(case, line_no=line_no)

        # 装配图组件
        retriever = self._retriever_for(_variant)
        generator = self._generator_for(_variant)
        if retriever is None or generator is None:
            return self._skip_result(case, reason=f"SKIP: {_variant} 组件不可用", line_no=line_no)

        reranker = self._reranker_for(_variant)

        trace_id = uuid4().hex
        run_id = uuid4().hex
        recorder = JsonTraceRecorder(self.trace_dir)
        # P2.5 主体隔离：本次 run 的稳定标识构造 RequestContext，传给生成器与图。
        request_context = local_request_context(thread_id=run_id, request_id=f"req-{run_id[:12]}")
        gateway = default_gateway_with_sink(recorder.record_provider_event)
        if hasattr(generator, "_request_context"):
            generator._request_context = request_context
        if hasattr(generator, "_gateway"):
            generator._gateway = gateway
        app = build_rag_graph(
            retriever, generator,
            trace=recorder,
            reranker=reranker,
            top_k=self.top_k if _variant == "real" else getattr(case, "top_k", 5),
            recall_top_k=self.recall_top_k if _variant == "real" else 20,
            request_context=request_context,
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

        # 真实生成器 token 用量（P2.5 review 修复：缺失时保持 unknown，不填 0）
        gen_usage = getattr(generator, "last_usage", None) or {}
        if isinstance(gen_usage.get("prompt_tokens"), int) and isinstance(
            gen_usage.get("completion_tokens"), int
        ):
            inp = int(gen_usage["prompt_tokens"])
            out = int(gen_usage["completion_tokens"])
            scored.token_usage = TokenUsage.known(
                input_tokens=inp, output_tokens=out,
                cost_usd=estimate_deepseek_cost(inp, out),
            )

        if _variant == "real":
            self._apply_judge(scored, case.query, result.get("answer", ""), context_texts=[c.text for c in result.get("context", [])])

        return scored

    def _run_real_agentic_case(self, case, *, line_no: int | None = None) -> EvalResult:
        """真实质量层（agentic 图）：模型自己检索、自己用 cite_clauses 声明引用、校验器真判。

        context_articles 取自消息历史里 retrieve_clauses 的命中（按召回顺序，供 recall@k）；
        token 用量汇总自各 AIMessage.usage_metadata；trace 由 build_agentic_app(trace=) 落盘。

        评测标签纪律（P1/Step 10 评审）：runner 执行期间**不读取** gold/forbidden 字段来
        调整检索或上下文——forbidden 条款若被召回就原样进入模型上下文，判分完全交给
        scorer（模型完成回答之后）。Recall@k/MRR 与 context_articles 因此都是真实
        Trivium/Rerank 返回结果的原始口径，legacy 与 agentic 对比使用相同检索输入。
        """
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.checkpoint.memory import MemorySaver

        assert self._real_retriever is not None and self._real_chat_model is not None
        reranker = self._reranker_for("real")
        retriever = (
            RerankingRetriever(self._real_retriever, reranker, recall_top_k=self.recall_top_k)
            if reranker is not None
            else self._real_retriever
        )
        recorder = JsonTraceRecorder(self.trace_dir)
        # P2.5 主体隔离 + trace 接线：每条用例一个真实 thread_id，
        # ToolRegistry 传入当前 trace 与真实主体（工具/护栏事件成对落盘）。
        case_thread_id = uuid4().hex
        request_context = local_request_context(
            thread_id=case_thread_id, request_id=f"req-{case_thread_id[:12]}"
        )
        # 让缓存的真实 chat model 用本条用例的主体（GovernedChatModel 经 _BudgetChatModel
        # 持有内层引用，这里直接置位其 request_context 即可逐用例生效）。
        gateway = default_gateway_with_sink(recorder.record_provider_event)
        if hasattr(self._real_chat_model, "request_context"):
            self._real_chat_model.request_context = request_context
        if hasattr(self._real_chat_model, "gateway"):
            self._real_chat_model.gateway = gateway
        # 先创建带本地工具的基础 registry（异常恢复路径保留本地工具）
        base_registry = ToolRegistry(principal=request_context.principal, trace=recorder)
        base_registry.register(build_retrieve_clauses_tool(retriever))
        base_registry.register(build_cite_clauses_tool())
        base_registry.register(calc_velocity_spec)
        base_registry.register(material_stats_spec)
        drawing_tool, _ = maybe_build_read_drawing_tool()
        if drawing_tool is not None:
            base_registry.register(drawing_tool)

        registry = base_registry  # 默认使用基础 registry
        # P4 Step 25：MCP 网关装配（装配期单入口，manifest hash 进 trace；单台故障不拖垮其余）。
        # 性能优化：使用模块级缓存避免每条用例重复 I/O
        mcp_assembly = _get_or_create_mcp_assembly(trace=recorder)
        try:
            from water_agent.mcp.wiring import register_mcp_tools
            from water_agent.runtime.governance.contracts import Capability as _Cap
            if mcp_assembly is not None:
                try:
                    recorder.record(node="mcp", event="mcp_assembly", data=mcp_assembly.summary())  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001, S110
                    pass
                if mcp_assembly.sessions:  # type: ignore[attr-defined]
                    # 先尝试注册 MCP 工具，只有成功注册后才发放能力（时序漏洞修复）
                    temp_registry = ToolRegistry(principal=request_context.principal, trace=recorder)
                    tool_count = register_mcp_tools(mcp_assembly, temp_registry)
                    if tool_count:
                        # 只有成功注册工具后才更新主体能力
                        caps2 = frozenset(request_context.principal.capabilities) | {_Cap.MCP_INVOKE}
                        request_context = request_context.model_copy(update={"principal": request_context.principal.model_copy(update={"capabilities": caps2})})
                        # 主体能力更新后重建 registry（护栏按新主体校验），再把所有工具注进去
                        new_registry = ToolRegistry(principal=request_context.principal, trace=recorder)
                        new_registry.register(build_retrieve_clauses_tool(retriever))
                        new_registry.register(build_cite_clauses_tool())
                        new_registry.register(calc_velocity_spec)
                        new_registry.register(material_stats_spec)
                        drawing_tool2, _ = maybe_build_read_drawing_tool()
                        if drawing_tool2 is not None:
                            new_registry.register(drawing_tool2)
                        register_mcp_tools(mcp_assembly, new_registry)
                        # 只有全部成功后才切换到新 registry（异常恢复时保留 base_registry）
                        registry = new_registry
        except Exception as exc:  # noqa: BLE001
            # 异常时 registry 仍是 base_registry（带本地工具）
            try:
                recorder.record(node="mcp", event="mcp_assembly_error", data={"error": str(exc)[:400]})
            except Exception:  # noqa: BLE001, S110
                pass
        tools = [tool_spec_to_base_tool(registry, name) for name in registry.list_all_names()]

        started = time.perf_counter()
        with MemorySaver() as saver:
            app = build_agentic_app(
                model=self._real_chat_model,
                tools=tools,
                checkpointer=saver,
                prompt=PromptAssembler(tool_registry=registry),
                max_tool_turns=self.max_tool_turns,
                trace=recorder,
                request_context=request_context,
            )
            result = app.invoke(
                {"messages": [HumanMessage(content=case.query)], "query": case.query},
                config={"configurable": {"thread_id": case_thread_id}},
            )
        latency_ms = (time.perf_counter() - started) * 1000

        context_articles, _ = _extract_validation_signal(result)
        actual_citations = [c.source_id for c in result.get("citations", [])]
        scored = score_eval_case(
            case,
            actual_status=result.get("status", ""),
            actual_citations=actual_citations,
            context_articles=context_articles,
            latency_ms=latency_ms,
            trace_id=recorder.trace_id or "",
            retry_count=int(result.get("retry_count", 0)),
            line_no=line_no,
        )
        scored.variant = "real"
        if not scored.passed and result.get("errors"):
            scored.failure_reason = "; ".join([scored.failure_reason, *result["errors"]]).strip("; ")

        # 逐消息聚合 usage_metadata；任一消息缺失 usage 则该次真实用量不可完整对账。
        in_tok = out_tok = 0
        saw_usage = False
        for m in result.get("messages", []):
            meta = getattr(m, "usage_metadata", None) if isinstance(m, AIMessage) else None
            if not meta:
                continue
            i = meta.get("input_tokens") if isinstance(meta, dict) else getattr(meta, "input_tokens", None)
            o = meta.get("output_tokens") if isinstance(meta, dict) else getattr(meta, "output_tokens", None)
            if isinstance(i, int) and isinstance(o, int):
                in_tok += i
                out_tok += o
                saw_usage = True
        if saw_usage:
            scored.token_usage = TokenUsage.known(
                input_tokens=in_tok, output_tokens=out_tok,
                cost_usd=estimate_deepseek_cost(in_tok, out_tok),
            )
        self._apply_judge(scored, case.query, result.get("answer", ""), context_texts=self._context_texts_from_messages(result))
        return scored

    @staticmethod
    def _context_texts_from_messages(result: dict) -> list[str]:
        """从 retrieve_clauses 的 ToolMessage 里取条文正文，供 judge 做忠实度判据。"""
        from langchain_core.messages import ToolMessage

        from water_agent.graphs.agent_rag import _payload_of

        texts: list[str] = []
        for m in result.get("messages", []):
            if isinstance(m, ToolMessage) and m.name == "retrieve_clauses":
                for hit in _payload_of(m).get("hits", []) or []:
                    text = hit.get("text")
                    if text:
                        texts.append(str(text))
        return texts

    def _apply_judge(self, scored: EvalResult, query: str, answer: str, *, context_texts: list[str] | None) -> None:
        """--judge 时给用例打 rubric 分并合并 judge token 用量（real 变体专用）。

        质量判定（quality_pass）独立于机制层 passed：judge 低分只影响质量结论与报告，
        绝不回写/篡改确定性 citation 判分（scorer 保持程序化）。
        """
        from water_agent.harness.quality import judge_quality_verdict

        if not self.judge or self._real_judge is None:
            return
        judge_result = self._real_judge.evaluate(query, answer, context=context_texts or None)
        if judge_result.scores:
            scored.judge_scores = {k: v.score for k, v in judge_result.scores.items()}
            scored.judge_reasoning = json.dumps({k: v.reasoning for k, v in judge_result.scores.items()}, ensure_ascii=False)
        scored.quality_pass, scored.quality_fail_reasons = judge_quality_verdict(scored.judge_scores)
        if judge_result.error and not judge_result.scores:
            # judge 有 Key 但调用失败/无评分：无法下质量结论（保持 None），原因进报告
            scored.quality_fail_reasons = [judge_result.error]
        if judge_result.token_usage:
            jt = judge_result.token_usage
            if scored.token_usage is None:
                scored.token_usage = jt
            else:
                # P2.5 review 修复：usage_unknown 的字段为 None，不得把 None 当 0 相加；
                # 双方都 known 才可合并，否则合并结果保持 unknown。
                st = scored.token_usage
                if (
                    st.usage_known
                    and jt.usage_known
                    and st.input_tokens is not None
                    and jt.input_tokens is not None
                    and st.output_tokens is not None
                    and jt.output_tokens is not None
                ):
                    st.input_tokens += jt.input_tokens
                    st.output_tokens += jt.output_tokens
                    st.total_tokens = (st.input_tokens or 0) + (st.output_tokens or 0)
                    st.cost_usd = round((st.cost_usd or 0.0) + (jt.cost_usd or 0.0), 8)

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

        # 引用正确率（非 skipped 用例；零引用对存在 expected_articles 的用例计失败）
        citation_ok_count = sum(1 for r in non_skipped if citation_ok_for_result(r))
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

        # 成本 & token 汇总（P2.5 review 修复：unknown 的 None 不参与求和，不写成 0）
        total_cost = sum(
            (r.token_usage.cost_usd or 0.0)
            for r in non_skipped
            if r.token_usage and r.token_usage.cost_usd is not None
        )
        total_tokens = sum(
            (r.token_usage.total_tokens or 0)
            for r in non_skipped
            if r.token_usage and r.token_usage.total_tokens is not None
        )

        # 质量层汇总（judge 判定过才参与；quality_pass=None 的用例不计入分母）
        judged = [r for r in non_skipped if r.quality_pass is not None]
        quality_judged_count = len(judged)
        quality_pass_count = sum(1 for r in judged if r.quality_pass)
        quality_low_score_count = quality_judged_count - quality_pass_count
        quality_pass_rate = (quality_pass_count / quality_judged_count) if judged else 0.0
        dims: list[str] = []
        for r in judged:
            for dim in r.judge_scores:
                if dim not in dims:
                    dims.append(dim)
        judge_dimension_averages: dict[str, float] = {}
        for dim in dims:
            with_score = [r for r in judged if dim in r.judge_scores]
            if with_score:
                judge_dimension_averages[dim] = round(
                    sum(r.judge_scores[dim] for r in with_score) / len(with_score), 4
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
            judge_dimension_averages=judge_dimension_averages,
            quality_low_score_count=quality_low_score_count,
            quality_judged_count=quality_judged_count,
            quality_pass_rate=round(quality_pass_rate, 4),
        )
        report_path = self._write_report(report, load_issues=load_issues)
        report.output_path = str(report_path)
        return report, report_path, load_issues

    def run(self):
        bundle = load_eval_bundle(self.eval_path, strict=False)
        return self.run_bundle(bundle)

    def _write_report(self, report: EvalReport, *, load_issues: list[dict]) -> Path:
        # 微秒级时间戳避免同一评测集并行跑 offline/real/SKIP 时互相覆盖证据。
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.report_dir / f"{self.eval_path.stem}_{stamp}.json"
        suffix = 1
        while path.exists():
            path = self.report_dir / f"{self.eval_path.stem}_{stamp}_{suffix}.json"
            suffix += 1
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
    p.add_argument("--real-graph", choices=["agentic", "legacy"], default="agentic",
                   help="--variant real 走哪条图：agentic（默认，模型自己声明引用、校验器真判）/ legacy（引用系统代填，仅供对比）")
    p.add_argument("--max-tool-turns", type=int, default=10, help="agentic 图每轮工具调用预算")
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
            real_graph=args.real_graph,
            max_tool_turns=args.max_tool_turns,
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
        "real_graph": runner.real_graph,
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "skipped": report.skipped,
        "pass_rate": report.pass_rate,
        "citation_ok_rate": report.citation_ok_rate,
        "total_cost_usd": report.total_cost_usd,
        "total_tokens": report.total_tokens,
        "avg_latency_ms": report.avg_latency_ms,
        "quality_pass_rate": report.quality_pass_rate,
        "quality_low_score_count": report.quality_low_score_count,
        "quality_judged_count": report.quality_judged_count,
        "judge_dimension_averages": report.judge_dimension_averages,
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

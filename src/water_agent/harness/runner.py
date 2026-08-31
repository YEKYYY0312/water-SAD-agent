"""JSONL Harness 评测跑分器。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from water_agent.domain.models import CalcCase, EvalReport, EvalResult
from water_agent.domain.state import default_state
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.calc import build_calc_graph
from water_agent.graphs.rag import build_rag_graph
from water_agent.harness.doubles import BadCitationGenerator
from water_agent.harness.loader import EvalLoadBundle, EvalLoadError, load_eval_bundle
from water_agent.harness.scorer import score_calc_case, score_eval_case
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.trivium import TriviumRetriever
from water_agent.tools.hydraulics import run_calc

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EVAL_REPORT_DIR = REPO_ROOT / "data" / "eval" / "reports"
DEFAULT_TRACE_DIR = REPO_ROOT / "data" / "eval" / "traces"
DEFAULT_FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


class HarnessRunner:
    """执行 JSONL 评测集并生成报告。"""

    def __init__(
        self,
        *,
        eval_path: str | Path,
        report_dir: str | Path = DEFAULT_EVAL_REPORT_DIR,
        trace_dir: str | Path = DEFAULT_TRACE_DIR,
        fixture_path: str | Path = DEFAULT_FIXTURE_PATH,
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

    def _retriever_for(self, variant: str):
        if variant == "trivium":
            if self._trivium_retriever is None:
                # P1 子块库：优先 fixtures 切块后的临时库；无则回落内存
                self._trivium_retriever = TriviumRetriever(
                    db_path=None,
                    fallback_clauses=self._clauses,
                )
            return self._trivium_retriever
        return self._retriever

    def _generator_for(self, variant: str):
        if variant == "bad_citation":
            return BadCitationGenerator()
        return MockGenerator()

    def _run_calc_case(self, case: CalcCase, *, line_no: int | None = None) -> EvalResult:
        """calc 端到端：走计算子图（真工具 + 确定性解释器），判分仍用 Step 1 run_calc。

        子图 compute 内部调 run_calc 使用与评测一致的输入，故数值与直接判分同源；
        缺参/非法路径可能短路（无 compute 产物），回退 run_calc 取结构化 CalcResult，
        保证判分确定性与 Step 1 一致。
        """
        app = build_calc_graph()
        state = {
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
        return score_calc_case(case, calc_result=calc_result, latency_ms=latency_ms, line_no=line_no)

    def _run_case(self, case, *, line_no: int | None = None):
        if isinstance(case, CalcCase):
            return self._run_calc_case(case, line_no=line_no)
        trace_id = uuid4().hex
        run_id = uuid4().hex
        recorder = JsonTraceRecorder(self.trace_dir)
        retriever = self._retriever_for(case.variant)
        app = build_rag_graph(retriever, self._generator_for(case.variant), trace=recorder)
        state = default_state(query=case.query, run_id=run_id, trace_id=trace_id)
        started = time.perf_counter()
        result = app.invoke(state)
        latency_ms = (time.perf_counter() - started) * 1000
        actual_citations = [citation.source_id for citation in result.get("citations", [])]
        context_articles = [clause.source_id for clause in result.get("context", [])]
        retry_count = int(result.get("retry_count", 0))
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
        return scored

    def run_bundle(self, bundle: EvalLoadBundle):
        results = [self._run_case(case) for case in bundle.cases]
        load_issues = [
            {
                "line_no": issue.line_no,
                "error": issue.error,
                "raw": issue.raw,
            }
            for issue in bundle.issues
        ]
        passed = sum(1 for result in results if result.passed)
        total = len(results)
        failed = total - passed
        pass_rate = (passed / total) if total else 0.0
        report = EvalReport(
            total=total,
            passed=passed,
            failed=failed,
            pass_rate=pass_rate,
            cases=results,
            input_path=str(self.eval_path),
            output_path="",
        )
        report_path = self._write_report(report, load_issues=load_issues)
        report.output_path = str(report_path)
        return report, report_path, load_issues

    def run(self):
        bundle = load_eval_bundle(self.eval_path, strict=False)
        return self.run_bundle(bundle)

    def _write_report(self, report: EvalReport, *, load_issues: list[dict[str, str | int | None]]) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.report_dir / f"{self.eval_path.stem}_{stamp}.json"
        payload = report.model_dump(mode="json")
        payload["load_issues"] = load_issues
        payload["created_at"] = report.created_at.isoformat()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the water_agent JSONL harness")
    parser.add_argument("eval_path", help="Path to a JSONL evaluation file")
    parser.add_argument("--report-dir", default=str(DEFAULT_EVAL_REPORT_DIR), help="Directory for report files")
    parser.add_argument("--trace-dir", default=str(DEFAULT_TRACE_DIR), help="Directory for trace files")
    parser.add_argument("--fixture-path", default=str(DEFAULT_FIXTURE_PATH), help="Retriever fixture JSONL path")
    return parser



def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        runner = HarnessRunner(
            eval_path=args.eval_path,
            report_dir=args.report_dir,
            trace_dir=args.trace_dir,
            fixture_path=args.fixture_path,
        )
        report, report_path, load_issues = runner.run()
    except (FileNotFoundError, EvalLoadError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    output = {
        "ok": True,
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "pass_rate": report.pass_rate,
        "report_path": str(report_path),
        "load_issues": load_issues,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if load_issues:
        return 2
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""生产问答 CLI + 交互式问答循环。

生产链路：TriviumDB 混合检索召回 → LocalReranker 重排 → top_k=5 → LLM 生成。
默认真实 LLM（DeepSeek 官方 Claude 兼容）；--mock-generator 可用确定性生成器自测链路。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from water_agent.domain.state import default_state
from water_agent.embedding import build_embedding_provider
from water_agent.generation.base import Generator
from water_agent.generation.llm import build_llm_generator
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.rag import build_rag_graph
from water_agent.observability.json_trace import JsonTraceRecorder
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.retrieval.rerank import BailianReranker, LocalReranker, build_bailian_reranker
from water_agent.retrieval.trivium import TriviumRetrievalError, TriviumRetriever

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
    p.add_argument("--mock-generator", action="store_true", help="用确定性 MockGenerator 代替真实 LLM（自测链路用）")
    p.add_argument("--interactive", action="store_true", help="进入交互式问答循环（输入 exit 退出）")
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


def build_app(*, args: argparse.Namespace, trace: JsonTraceRecorder | None = None) -> tuple[object, object, str]:
    """装配 检索(含 rerank) + 生成 + RAG 图。返回 (app, retriever, db_path_or_empty).

    ``trace`` 传入图内 recorder，使检索/生成/引用校验等事件可被记录。
    """
    fixture = load_clauses_jsonl(DEFAULT_FIXTURE)
    db_path = None
    if args.db_path and Path(args.db_path).exists():
        db_path = Path(args.db_path)
    elif not args.db_path and not args.mock_generator:
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
        generator = build_llm_generator(model=args.model, base_url=args.base_url)

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


def run_once(query: str, *, args: argparse.Namespace) -> dict:
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


def _print_result(payload: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    query = (args.query or "").strip()

    if args.interactive:
        print("交互式问答（输入 exit 退出）：")
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
                payload = run_once(line, args=args)
            except Exception as exc:  # noqa: BLE001 — 交互循环保持健壮，任一问题打印后继续
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
                continue
            _print_result(payload, json_output=args.json)
        return 0

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
    _print_result(payload, json_output=args.json)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

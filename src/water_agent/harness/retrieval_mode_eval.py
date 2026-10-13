"""检索模式评测：同一数据集、同一语料，分别跑 hybrid 与 text 两种检索（P7-S10，规格 P7-S10-A07）。

``text`` 是关键词（BM25）检索：不调用任何 embedding。只配了一个对话模型 Key 的部署走的就是它，所以
需要单独的质量证据——``rag_p1`` 的问法与条文措辞高度同构（字符二元组重合率中位数约 0.63），对关键词
检索偏乐观；``retrieval_colloquial_p0`` 为同一批 gold 条款各配一句口语化 / 同义改写问法（S10 Step 1
冻结），两套一起看。

每条用例记两条检索管线的前 5 条结果：

- ``retriever``：检索器本身 ``retrieve(query, top_k=5)``（Prompt 实测数字的口径）；
- ``local_rerank``：生产缺省路径——召回 20 条、本地确定性重排后取 5 条（``RerankingRetriever`` +
  ``LocalReranker``，与 agentic 检索工具同一组件）。

指标一律用 ``harness/retrieval_metrics.py``（Recall@k / MRR）计算；另记每条问法与 gold 条文的字符
二元组重合率（问法二元组里出现在 gold 条文中的比例）与最长公共子串，证明问法不是从条文抄的。

索引：``--db-path`` 显式给出时用它；否则 ``text`` 模式从 ``data/curated/clauses`` 现建一份纯文本索引
（零向量，不要求任何 Key，CI 干净克隆里也能跑——``data/indexes/`` 不入库），``hybrid`` 模式按
``resolve_index_path()`` 找真实索引。``hybrid`` 需要 embedding Key 与真实索引：缺任何一个即结构化
SKIP（``status=skipped`` + ``requires``）；有 Key 但调用失败记 ``blocked``、退出码 3，绝不包成 SKIP。

``mechanism``：运行时是否已有 ``text`` 检索模式（S10 Step 6 之后为 ``present``）。``absent`` 时 text 模式
用"零向量 + ``hybrid_alpha=0``"在未改动的检索器上取得同样的关键词排序——那是 Step 1 的"改之前"报告，
不是门禁对象。

退出码：0 跑完（含 SKIP）；1 有用例执行出错，或 text 模式发生了 embedding 调用；2 加载问题、0 条用例或
参数错误；3 hybrid 模式有 Key 但真实检索建不起来 / 调用失败（blocked）。
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import statistics
import sys
import tempfile
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from water_agent.harness.retrieval_metrics import (
    aggregate_retrieval_metrics,
    case_retrieval_metrics,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO_ROOT / "data" / "eval" / "retrieval_colloquial_p0.jsonl"
DEFAULT_CURATED_ROOT = REPO_ROOT / "data" / "curated"

GENERATED_BY = "water_agent.harness.retrieval_mode_eval"
REPORT_FORMAT = 1
MODES = ("text", "hybrid")
PIPELINES = ("retriever", "local_rerank")
TOP_K = 5
RECALL_TOP_K = 20
#: 现建纯文本索引的向量维度（向量全为零，维度只是库的形状要求）。与 build_index.py 的缺省一致。
TEXT_INDEX_DIMENSIONS = 512
#: 二元组重合率不高于它即"字面重合度低"（数据集要求至少一半用例满足）。
LOW_OVERLAP_THRESHOLD = 0.30
_RATE_KEYS = ("recall_at_1", "recall_at_3", "recall_at_5", "mrr")


class HarnessUsageError(RuntimeError):
    """参数或环境不对，评测无法开始（退出码 2）。"""


class RetrievalBlocked(RuntimeError):
    """有 Key 但真实检索建不起来或调用失败（退出码 3）。"""


# ---------------------------------------------------------------------------
# 字面重合度
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """NFKC + 只留字母数字（含汉字）+ 小写：标点、空白与全半角差异不计入重合。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if ch.isalnum()).lower()


def char_bigrams(text: str) -> set[str]:
    normalized = _normalize(text)
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def bigram_overlap(query: str, reference: str) -> float:
    """问法的字符二元组中出现在参照文本里的比例（0~1）；问法不足两个字符时为 0。"""
    grams = char_bigrams(query)
    if not grams:
        return 0.0
    return round(len(grams & char_bigrams(reference)) / len(grams), 4)


def longest_common_run(query: str, reference: str) -> int:
    """问法与参照文本的最长公共子串长度（规范化后按字符计）。"""
    left, right = _normalize(query), _normalize(reference)
    best = 0
    for start in range(len(left)):
        end = start + best + 1
        while end <= len(left) and left[start:end] in right:
            best = end - start
            end += 1
    return best


# ---------------------------------------------------------------------------
# 数据集与语料
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    id: str
    query: str
    gold: tuple[str, ...]
    phrasing: str


def load_cases(path: Path) -> tuple[list[RetrievalCase], list[str]]:
    """读用例（``query`` + ``gold_articles_any``，其余字段忽略）；任何一行坏掉都记为加载问题。"""

    cases: list[RetrievalCase] = []
    problems: list[str] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            problems.append(f"第 {line_no} 行不是合法 JSON：{exc.msg}")
            continue
        case_id = payload.get("id") if isinstance(payload, dict) else None
        query = payload.get("query") if isinstance(payload, dict) else None
        gold = payload.get("gold_articles_any") if isinstance(payload, dict) else None
        if not isinstance(case_id, str) or not case_id:
            problems.append(f"第 {line_no} 行缺 id")
            continue
        if case_id in seen:
            problems.append(f"用例 id 重复：{case_id}")
            continue
        if not isinstance(query, str) or not query.strip():
            problems.append(f"{case_id} 缺 query")
            continue
        if (
            not isinstance(gold, list)
            or not gold
            or not all(isinstance(v, str) and v for v in gold)
        ):
            problems.append(f"{case_id} 的 gold_articles_any 必须是非空字符串列表")
            continue
        seen.add(case_id)
        phrasing = payload.get("phrasing", "unspecified")
        cases.append(
            RetrievalCase(
                id=case_id,
                query=query,
                gold=tuple(gold),
                phrasing=phrasing if isinstance(phrasing, str) and phrasing else "unspecified",
            )
        )
    return cases, problems


def _clause_files(curated_root: Path) -> list[Path]:
    return sorted((curated_root / "clauses").glob("*.jsonl"))


def corpus_texts(curated_root: Path) -> dict[str, str]:
    """source_id（``<spec_id> <article>``）→ 该条款全部记录的正文（同一条款可能有正文与条文说明两条）。"""

    texts: dict[str, list[str]] = {}
    for path in _clause_files(curated_root):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            record = json.loads(raw)
            source_id = f"{record.get('spec_id', '')} {record.get('article', '')}".strip()
            texts.setdefault(source_id, []).append(str(record.get("text", "")))
    return {source_id: "\n".join(parts) for source_id, parts in texts.items()}


def corpus_sha256(curated_root: Path) -> str:
    digest = hashlib.sha256()
    for path in _clause_files(curated_root):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 检索器
# ---------------------------------------------------------------------------


def text_mode_implemented() -> bool:
    """运行时是否已有 ``text`` 检索模式（S10 Step 6）。只看签名，不 import 不存在的符号。"""

    from water_agent.retrieval.trivium import TriviumRetriever

    return "retrieval_mode" in inspect.signature(TriviumRetriever.__init__).parameters


def _zero_vector_provider(dimensions: int) -> Any:
    """现建纯文本索引用的占位 provider：只给维度元数据，embed 返回零向量（不做 embedding）。"""

    class _ZeroVector:
        config = type(
            "ZeroVectorConfig",
            (),
            {
                "model": "none",
                "dimensions": dimensions,
                "index_version": f"text-only__{dimensions}__curated-v1",
                "fallback_enabled": False,
            },
        )()

        @staticmethod
        def embed(text: str) -> list[float]:
            return [0.0] * dimensions

    return _ZeroVector()


def build_curated_text_index(curated_root: Path, db_path: Path, *, dimensions: int) -> None:
    """从受控语料现建一份纯文本索引（零向量；与真实索引的文本分量同源，见决策记录）。"""

    from water_agent.ingestion.pipeline import ingest_to_trivium

    files = _clause_files(curated_root)
    if not files:
        raise HarnessUsageError(f"{curated_root / 'clauses'} 下没有受控语料")
    provider = _zero_vector_provider(dimensions)
    for path in files:
        ingest_to_trivium(path, db_path, embedding_provider=provider, curated=True)


def _index_dimensions(db_path: Path) -> int:
    import triviumdb

    probe = triviumdb.TriviumDB(str(db_path), access_mode="read_only")
    try:
        return int(probe.dim())
    finally:
        probe.close()


def _session_args(db_path: Path, *, mode: str, dimensions: int) -> argparse.Namespace:
    return argparse.Namespace(
        db_path=str(db_path),
        dimensions=dimensions,
        hybrid_alpha=0.7,
        mock_generator=False,
        retrieval_mode=mode,
    )


def build_retriever(db_path: Path, *, mode: str) -> Any:
    """按生产装配（``runtime.agentic._build_retriever``）建检索器；text 模式机制缺失时用零向量等价物。"""

    dimensions = _index_dimensions(db_path)
    if mode == "text" and not text_mode_implemented():
        from water_agent.retrieval.trivium import TriviumRetriever

        return TriviumRetriever(
            db_path=db_path,
            embedding_fn=lambda _text: [0.0] * dimensions,
            hybrid_alpha=0.0,
            dim=dimensions,
        )
    from water_agent.runtime.agentic import _build_retriever

    retriever, _ = _build_retriever(_session_args(db_path, mode=mode, dimensions=dimensions))
    return retriever


@contextmanager
def count_embedding_calls() -> Iterator[list[int]]:
    """统计真实 embedding provider 的 ``embed`` 调用次数（text 模式必须为 0）。"""

    from water_agent import embedding

    provider_class = embedding.BailianEmbeddingProvider
    original = provider_class.embed
    counter = [0]

    def counted(self: Any, text: str) -> list[float]:
        counter[0] += 1
        return original(self, text)

    provider_class.embed = counted  # type: ignore[method-assign]
    try:
        yield counter
    finally:
        provider_class.embed = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 运行与报告
# ---------------------------------------------------------------------------


def _rank(retrieved: Sequence[str], gold: Sequence[str]) -> int | None:
    wanted = set(gold)
    for position, source_id in enumerate(retrieved, start=1):
        if source_id in wanted:
            return position
    return None


def run_case(case: RetrievalCase, retriever: Any, gold_text: str) -> dict[str, Any]:
    from water_agent.retrieval.rerank import LocalReranker, RerankingRetriever

    result: dict[str, Any] = {
        "id": case.id,
        "query": case.query,
        "phrasing": case.phrasing,
        "gold": list(case.gold),
        "overlap": bigram_overlap(case.query, gold_text),
        "max_common_run": longest_common_run(case.query, gold_text),
        "error": None,
    }
    pipelines = {
        "retriever": retriever,
        "local_rerank": RerankingRetriever(retriever, LocalReranker(), recall_top_k=RECALL_TOP_K),
    }
    for name, pipeline in pipelines.items():
        try:
            retrieved = [hit.source_id for hit in pipeline.retrieve(case.query, top_k=TOP_K)]
        except Exception as exc:  # noqa: BLE001 — 单条失败记进报告，不中断整套
            result["error"] = f"{name}: {type(exc).__name__}: {exc}"
            retrieved = []
        result[name] = {"retrieved": retrieved, "rank": _rank(retrieved, case.gold)}
    result["passed"] = result["error"] is None and result["retriever"]["rank"] is not None
    return result


def _overlap_summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {
            "median": None,
            "mean": None,
            "min": None,
            "max": None,
            "low_count": 0,
            "low_share": None,
        }
    low = sum(1 for value in values if value <= LOW_OVERLAP_THRESHOLD)
    return {
        "median": round(statistics.median(values), 4),
        "mean": round(statistics.fmean(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "low_count": low,
        "low_share": round(low / len(values), 4),
    }


def metrics_from_case_dicts(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """从报告里的用例明细算出全部指标。harness 写报告与比较器核对"报告没被手改"共用这一个函数。"""

    metrics: dict[str, Any] = {}
    for pipeline in PIPELINES:
        rows = [
            case_retrieval_metrics(
                case.get(pipeline, {}).get("retrieved", []), case.get("gold", [])
            )
            for case in cases
        ]
        aggregated = aggregate_retrieval_metrics(rows)
        metrics[pipeline] = {key: aggregated[key] for key in _RATE_KEYS}
        by_phrasing: dict[str, dict[str, float]] = {}
        for phrasing in sorted({str(case.get("phrasing", "unspecified")) for case in cases}):
            subset = [
                row
                for row, case in zip(rows, cases, strict=True)
                if case.get("phrasing") == phrasing
            ]
            sub = aggregate_retrieval_metrics(subset)
            by_phrasing[phrasing] = {"queries": sub["queries"], "recall_at_5": sub["recall_at_5"]}
        metrics[pipeline]["by_phrasing"] = by_phrasing
    metrics["overlap"] = _overlap_summary([float(case.get("overlap", 0.0)) for case in cases])
    metrics["max_common_run"] = max(
        (int(case.get("max_common_run", 0)) for case in cases), default=0
    )
    metrics["harness_error_count"] = sum(1 for case in cases if case.get("error") is not None)
    return metrics


def _dataset_label(dataset: Path) -> str:
    try:
        return dataset.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return dataset.name


def _base_report(dataset: Path, *, mode: str, curated_root: Path) -> dict[str, Any]:
    return {
        "suite": dataset.stem,
        "generated_by": GENERATED_BY,
        "report_format": REPORT_FORMAT,
        "mode": mode,
        "mechanism": "present" if text_mode_implemented() else "absent",
        "dataset": _dataset_label(dataset),
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "corpus_sha256": corpus_sha256(curated_root),
        "top_k": TOP_K,
        "recall_top_k": RECALL_TOP_K,
        "low_overlap_threshold": LOW_OVERLAP_THRESHOLD,
    }


def _skipped(base: dict[str, Any], requires: list[str], reason: str) -> dict[str, Any]:
    return {**base, "status": "skipped", "requires": requires, "reason": reason, "ok": True}


def _hybrid_index(db_path: Path | None) -> Path | None:
    if db_path is not None:
        return db_path
    from water_agent.retrieval.trivium import IndexSelectionError, resolve_index_path

    try:
        return resolve_index_path()
    except IndexSelectionError as exc:
        raise HarnessUsageError(str(exc)) from exc


def run(
    dataset: Path,
    *,
    mode: str,
    db_path: Path | None = None,
    curated_root: Path = DEFAULT_CURATED_ROOT,
) -> tuple[dict[str, Any] | None, list[str]]:
    if mode not in MODES:
        raise HarnessUsageError(f"--mode 只能是 {MODES}")
    if db_path is not None and not db_path.exists():
        raise HarnessUsageError(f"指定的索引不存在：{db_path}")
    cases, problems = load_cases(dataset)
    if problems:
        return None, problems
    if not cases:
        return None, ["0 条可执行用例"]
    texts = corpus_texts(curated_root)
    if missing := sorted({gold for case in cases for gold in case.gold if gold not in texts}):
        return None, [f"gold 条款不在受控语料里：{missing}"]
    base = _base_report(dataset, mode=mode, curated_root=curated_root)

    with tempfile.TemporaryDirectory(prefix="wa-retrieval-mode-") as tmp:
        if mode == "text":
            if db_path is None:
                index = Path(tmp) / "curated_text_only.db"
                build_curated_text_index(curated_root, index, dimensions=TEXT_INDEX_DIMENSIONS)
                index_source = "curated_text_only_build"
            else:
                index, index_source = db_path, "explicit"
        else:
            from water_agent.runtime.agentic import embedding_configured

            resolved = _hybrid_index(db_path)
            if resolved is None:
                return _skipped(
                    base,
                    ["retrieval_index"],
                    "没有真实检索索引（data/indexes/ 或 WATER_AGENT_INDEX_PATH）",
                ), []
            if not embedding_configured():
                return _skipped(base, ["DASHSCOPE_API_KEY"], "hybrid 模式需要 embedding Key"), []
            index, index_source = resolved, "explicit" if db_path is not None else "resolved"
        with count_embedding_calls() as counter:
            try:
                retriever = build_retriever(index, mode=mode)
            except Exception as exc:
                if mode == "hybrid":
                    raise RetrievalBlocked(f"{type(exc).__name__}: {exc}") from exc
                raise
            results = [
                run_case(case, retriever, " ".join(texts[g] for g in case.gold)) for case in cases
            ]
            close = getattr(retriever, "close", None)
            if callable(close):
                close()
        embedding_calls = counter[0]

    metrics = metrics_from_case_dicts(results)
    status = "ok"
    if mode == "hybrid" and metrics["harness_error_count"]:
        status = "blocked"
    return {
        **base,
        "status": status,
        "index_source": index_source,
        "embedding_calls": embedding_calls,
        "total": len(results),
        "passed": sum(1 for result in results if result["passed"]),
        "metrics": metrics,
        "ok": status == "ok"
        and not metrics["harness_error_count"]
        and not (mode == "text" and embedding_calls),
        "cases": results,
    }, []


def _print_summary(report: dict[str, Any]) -> None:
    head = f"{report['suite']} mode={report['mode']} mechanism={report['mechanism']} status={report['status']}"
    if report["status"] == "skipped":
        print(f"{head} requires={report['requires']}（{report['reason']}）")
        return
    print(f"{head} index={report['index_source']} embedding_calls={report['embedding_calls']}")
    for pipeline in PIPELINES:
        values = report["metrics"][pipeline]
        rates = " ".join(f"{key}={values[key]}" for key in _RATE_KEYS)
        phrasing = " ".join(
            f"{name}:R@5={item['recall_at_5']}({item['queries']})"
            for name, item in values["by_phrasing"].items()
        )
        print(f"  {pipeline}: {rates} | {phrasing}")
    print(
        f"  overlap: {report['metrics']['overlap']} max_common_run={report['metrics']['max_common_run']}"
    )
    misses = [case["id"] for case in report["cases"] if case["retriever"]["rank"] is None]
    print(f"  retriever 前 5 未命中（{len(misses)}）：{misses}")
    for case in report["cases"]:
        if case["error"]:
            print(f"  ERROR {case['id']}: {case['error']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or GENERATED_BY).splitlines()[0])
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--mode", choices=MODES, default="text")
    parser.add_argument("--db-path", type=Path, default=None, help="显式指定索引（缺省见模块说明）")
    parser.add_argument("--curated-root", type=Path, default=DEFAULT_CURATED_ROOT)
    parser.add_argument("--report-path", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        report, problems = run(
            args.dataset, mode=args.mode, db_path=args.db_path, curated_root=args.curated_root
        )
    except HarnessUsageError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2
    except RetrievalBlocked as exc:
        print(
            json.dumps(
                {"status": "blocked", "mode": args.mode, "error": str(exc)}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 3
    if report is None:
        for problem in problems:
            print(f"!! 加载问题：{problem}", file=sys.stderr)
        return 2
    if args.report_path is not None:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    _print_summary(report)
    if report["status"] == "blocked":
        return 3
    if report["status"] == "skipped":
        return 0
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GENERATED_BY",
    "LOW_OVERLAP_THRESHOLD",
    "PIPELINES",
    "REPORT_FORMAT",
    "bigram_overlap",
    "build_curated_text_index",
    "corpus_texts",
    "load_cases",
    "longest_common_run",
    "metrics_from_case_dicts",
    "run",
    "text_mode_implemented",
]

"""检索模式评测 ``retrieval_colloquial_p0``（P7-S10-A07，``docs/design/16-p7-plugin-mcp.md`` §7 S10）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from water_agent.harness import retrieval_mode_eval as rme

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = rme.DEFAULT_DATASET
RAG_P1 = REPO_ROOT / "data" / "eval" / "rag_p1.jsonl"
BASELINE = REPO_ROOT / "data" / "eval" / "retrieval_colloquial_p0_main.json"
#: S10 Step 1 冻结时的数据集摘要。有意修改必须同时重新固化入仓基线并在决策记录写明原因。
FROZEN_SHA256 = "704d82d8b2601b4c47e409e0d1a3994ebc725b888e667be6f807c1389491bd56"


@pytest.fixture(scope="module")
def text_report() -> dict:
    report, problems = rme.run(DATASET, mode="text")
    assert problems == [] and report is not None
    return report


def test_colloquial_set_is_frozen_and_measured(text_report: dict) -> None:
    """P7-S10-A07 口语化检索集先冻结、再测量：≥ 40 条、gold 都在受控语料里且与 rag_p1 是同一批条款、
    至少一半问法与 gold 条文的字符二元组重合率 ≤ 0.30、没有一条与条文的公共子串超过 5 个字（不是从条文
    抄的）；text 模式从受控语料现建纯文本索引即可跑（不要求任何 Key、零 embedding 调用），报告指标可由
    用例明细重算。"""

    assert hashlib.sha256(DATASET.read_bytes()).hexdigest() == FROZEN_SHA256
    cases, problems = rme.load_cases(DATASET)
    assert problems == []
    assert len(cases) >= 40
    assert {case.phrasing for case in cases} == {"colloquial", "paraphrase"}
    rag_p1_cases, _ = rme.load_cases(RAG_P1)
    rag_p1_gold = {gold for case in rag_p1_cases for gold in case.gold}
    assert {gold for case in cases for gold in case.gold} <= rag_p1_gold

    texts = rme.corpus_texts(rme.DEFAULT_CURATED_ROOT)
    overlaps = [
        rme.bigram_overlap(case.query, " ".join(texts[g] for g in case.gold)) for case in cases
    ]
    assert sum(1 for value in overlaps if value <= rme.LOW_OVERLAP_THRESHOLD) * 2 >= len(cases)
    assert all(
        rme.longest_common_run(case.query, " ".join(texts[g] for g in case.gold)) <= 5
        for case in cases
    )

    assert text_report["status"] == "ok"
    assert text_report["mechanism"] == "present", "S10 Step 6 起 text 模式是生产路径"
    assert text_report["index_source"] == "curated_text_only_build"
    assert text_report["embedding_calls"] == 0
    assert text_report["total"] == len(cases)
    assert text_report["metrics"] == rme.metrics_from_case_dicts(text_report["cases"])
    assert text_report["metrics"]["harness_error_count"] == 0
    assert text_report["metrics"]["overlap"]["low_count"] * 2 >= len(cases)
    assert json.loads(json.dumps(text_report, ensure_ascii=False)) == text_report


def test_overlap_helpers_ignore_punctuation_and_width() -> None:
    """P7-S10-A07 重合率只看字母数字（NFKC 规范化后），标点与全半角不计入。"""

    assert rme.bigram_overlap("排水管？", "排水管道") == 1.0
    assert rme.bigram_overlap("ＤＮ２００", "dn200 管") == 1.0
    assert rme.bigram_overlap("雨", "雨水") == 0.0
    assert rme.longest_common_run("跌水井落差", "跌水井的进水管") == 3


def test_hybrid_mode_skips_structurally_without_key_or_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P7-S10-A07 hybrid 模式没有真实索引或没有 embedding Key 时结构化 SKIP（带 ``requires``），不发请求。"""

    for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_API_KEY_FILE", "WATER_AGENT_INDEX_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", "/nonexistent-water-agent-credentials")
    monkeypatch.setattr(
        "water_agent.retrieval.trivium.DEFAULT_INDEX_DIR", REPO_ROOT / "tests" / "no-such-index-dir"
    )
    report, problems = rme.run(DATASET, mode="hybrid")
    assert problems == [] and report is not None
    assert report["status"] == "skipped"
    assert report["requires"] == ["retrieval_index"]


def _compare(head: dict, base: dict | None) -> dict:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "compare_retrieval_colloquial_p0",
        REPO_ROOT / "scripts" / "compare_retrieval_colloquial_p0.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compare_reports(head, base)


def test_colloquial_baseline_comparator(text_report: dict) -> None:
    """P7-S10-A07 入仓基线是同一数据集、同一语料上的 text 模式 present 报告，HEAD 与之对比通过；比较器拒绝
    手改指标、absent 机制、非 text 模式、发生 embedding 调用、数据集变化、用例消失与 PASS_TO_PASS 回退。"""

    import copy

    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert base["mechanism"] == "present" and base["mode"] == "text"
    assert base["dataset_sha256"] == FROZEN_SHA256
    result = _compare(text_report, base)
    assert result["passed"], result["violations"]

    def broken(**changes: object) -> dict:
        report = copy.deepcopy(base)
        report.update(changes)
        return report

    hand_edited = copy.deepcopy(base)
    hand_edited["metrics"]["retriever"]["recall_at_5"] = 1.0
    for head in (
        hand_edited,
        broken(mechanism="absent"),
        broken(mode="hybrid"),
        broken(embedding_calls=3),
        broken(dataset_sha256="0" * 64),
    ):
        assert not _compare(head, base)["passed"]

    shrunk = copy.deepcopy(base)
    shrunk["cases"] = shrunk["cases"][1:]
    shrunk["metrics"] = rme.metrics_from_case_dicts(shrunk["cases"])
    shrunk["total"] = len(shrunk["cases"])
    shrunk["passed"] = sum(1 for case in shrunk["cases"] if case["passed"])
    assert any("消失" in item for item in _compare(shrunk, base)["violations"])

    regressed = copy.deepcopy(base)
    case = next(case for case in regressed["cases"] if case["passed"])
    case["passed"] = False
    case["retriever"] = {"retrieved": [], "rank": None}
    regressed["metrics"] = rme.metrics_from_case_dicts(regressed["cases"])
    regressed["passed"] -= 1
    violations = _compare(regressed, base)["violations"]
    assert any("PASS_TO_PASS" in item for item in violations)
    assert any("变差" in item for item in violations)

"""rerank：混合检索召回 → 重排序 → 取 top_k。"""
from __future__ import annotations

from water_agent.domain.models import RetrievedClause
from water_agent.retrieval.rerank import BailianReranker, LocalReranker, Reranker, IdentityReranker


def _clause(spec: str, article: str, text: str, score: float = 0.0) -> RetrievedClause:
    return RetrievedClause(spec_id=spec, article=article, title="", text=text, score=score)


def test_reranker_is_abstract():
    try:
        Reranker()  # type: ignore[abstract]
        raised = False
    except TypeError:
        raised = True
    assert raised


def test_identity_reranker_keeps_order_and_caps():
    rr = IdentityReranker()
    clauses = [_clause("A", "1", "x"), _clause("B", "2", "y")]
    out = rr.rerank("q", clauses, top_k=1)
    assert len(out) == 1
    assert out[0].article == "1"


def test_local_reranker_moves_relevant_first_using_score_and_overlap():
    rr = LocalReranker(alpha=0.5)
    # 候选1：score 高但几乎不与 query 重叠；候选2：score 较低但与 query 高度重叠
    clauses = [
        _clause("GB50013-2018", "5.1.1", "水源勘察应结合当地水文地质条件进行。", score=0.9),
        _clause("GB50014-2021", "3.1.1", "排水体制（分流制或合流制）的选择应因地制宜。", score=0.5),
    ]
    # query 与候选2 高度重叠
    out = rr.rerank("排水体制 分流 合流 选择 因地制宜 规定", clauses, top_k=2)
    assert out[0].article == "3.1.1"


def test_local_reranker_caps_to_top_k():
    rr = LocalReranker()
    clauses = [_clause("A", str(i), f"条款正文 {i} 应执行", score=float(10 - i)) for i in range(5)]
    out = rr.rerank("条款 应执行", clauses, top_k=3)
    assert len(out) == 3
    scores = [c.score for c in out]
    assert scores == sorted(scores, reverse=True)


def test_local_reranker_handles_missing_scores():
    rr = LocalReranker()
    clauses = [_clause("A", "1", "关于设计流量的规定"), _clause("B", "2", "关于设计流量的规定")]
    out = rr.rerank("设计流量 规定", clauses, top_k=2)
    assert len(out) == 2


NATIVE_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"


def test_bailian_endpoint_normalizes_compatible_mode_url():
    rr = BailianReranker(api_key="sk-x", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert rr._endpoint() == f"https://dashscope.aliyuncs.com{NATIVE_PATH}"
    rr2 = BailianReranker(api_key="sk-x", base_url="https://dashscope.aliyuncs.com")
    assert rr2._endpoint() == f"https://dashscope.aliyuncs.com{NATIVE_PATH}"


def test_bailian_sends_native_payload_and_applies_results():
    captured: dict | None = None

    def fake_transport(payload: dict) -> dict:
        nonlocal captured
        captured = payload
        return {
            "output": {
                "results": [
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.30},
                ]
            },
            "usage": {"total_tokens": 40},
        }

    rr = BailianReranker(api_key="sk-x", transport=fake_transport)
    clauses = [_clause("A", "1", "条文甲"), _clause("B", "2", "条文乙")]
    out = rr.rerank("设计秒流量", clauses, top_k=1)

    assert captured["model"] == "gte-rerank"
    assert captured["input"]["query"] == "设计秒流量"
    assert captured["input"]["documents"] == ["条文甲", "条文乙"]
    assert captured["parameters"]["top_n"] == 1
    assert len(out) == 1
    assert out[0].article == "2"
    assert out[0].score == 0.95

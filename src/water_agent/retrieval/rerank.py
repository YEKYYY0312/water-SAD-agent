"""rerank：先在召回更多候选，再用 query 重排取 top_k。

本地确定性实现（无网络依赖）：IdentityReranker、LocalReranker。
云端模型实现：BailianReranker（百炼原生 text-rerank，默认 gte-rerank）。

任何云端 rerank 都必须显式提供 api_key（或 DASHSCOPE_API_KEY），未提供即抛错，
绝不自动本地兜底。

P2.5（Step 12）：``BailianReranker`` 的远程调用经 ``ProviderGateway``。
403 AccessDenied 属永久错误 → 零重试、抛 ``PROVIDER_AUTH_FAILED``，
**绝不静默回退 LocalReranker**（规格 §1.1 禁止 silent fallback）。
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any
from urllib import request

from water_agent.domain.models import RetrievedClause
from water_agent.retrieval.base import Retriever
from water_agent.runtime.governance.binding import default_gateway, local_request_context
from water_agent.runtime.governance.contracts import (
    CallCategory,
    Capability,
    ProviderCall,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway, ProviderUsage


class Reranker(ABC):
    """给定用户 query 与召回候选，重排并截断到 top_k。"""

    @abstractmethod
    def rerank(self, query: str, candidates: list[RetrievedClause], top_k: int) -> list[RetrievedClause]:
        raise NotImplementedError


def _text_of(clause: RetrievedClause) -> str:
    return " ".join([clause.title or "", clause.text or "", clause.section or "", clause.article or ""]).lower()


def _bigrams(text: str) -> set[str]:
    cleaned = "".join(ch for ch in text if not ch.isspace())
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def _overlap_score(query: str, clause: RetrievedClause) -> float:
    q_grams = _bigrams(query)
    t_grams = _bigrams(_text_of(clause))
    if not q_grams:
        return 0.0
    if q_grams == t_grams:
        return 1.0
    return len(q_grams & t_grams) / len(q_grams)


class IdentityReranker(Reranker):
    """原序截断。"""

    def rerank(self, query: str, candidates: list[RetrievedClause], top_k: int) -> list[RetrievedClause]:
        return candidates[:top_k]


class LocalReranker(Reranker):
    """组合 原始混合分 与 query/候选 bigram 重叠 的确定性重排。"""

    def __init__(self, alpha: float = 0.5) -> None:
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("alpha must be in [0,1]")
        self.alpha = alpha

    def rerank(self, query: str, candidates: list[RetrievedClause], top_k: int) -> list[RetrievedClause]:
        """重排并返回**副本**：不改写入参对象的 score。

        InMemoryRetriever / fixture 回落每次返回同一批对象，若原地改 score，下一条
        无关 query 会把上一问的重排分当"原始混合分"用（跨查询污染）。
        """
        scores = [c.score for c in candidates]
        max_score = max((s for s in scores if s is not None), default=0.0)
        scored = []
        for clause, base in zip(candidates, scores):
            normalized = base / max_score if (max_score and base is not None) else 0.0
            overlap = _overlap_score(query, clause)
            final = self.alpha * normalized + (1.0 - self.alpha) * overlap
            scored.append((final, clause))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [clause.model_copy(update={"score": final}) for final, clause in scored[:top_k]]


class BailianReranker(Reranker):
    """阿里云百炼真实 rerank 模型（原生 DashScope API）。

    正确端点：POST https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
    复用 DASHSCOPE_API_KEY。默认 gte-rerank，base_url / model 均可覆盖。
    transport 可注入用于测试；未注入走真实 urllib 调用（需 api_key）。
    为兼容历史传入的 compatible-mode URL，内部会自动归一到原生端点。
    """

    DEFAULT_MODEL = "gte-rerank"
    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
    DEFAULT_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 60.0,
        transport: Any = None,
        gateway: ProviderGateway | None = None,
        request_context: RequestContext | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("BailianReranker requires api_key (no local fallback)")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._gateway = gateway
        self._request_context = request_context

    def _resolve_gateway(self) -> ProviderGateway:
        return self._gateway if self._gateway is not None else default_gateway()

    def _resolve_context(self) -> RequestContext:
        if self._request_context is not None:
            return self._request_context
        return local_request_context(thread_id="rerank")

    def _endpoint(self) -> str:
        b = self.base_url.rstrip("/")
        if b.endswith(self.DEFAULT_PATH):
            return b
        if "compatible-mode" in b:
            return f"https://dashscope.aliyuncs.com{self.DEFAULT_PATH}"
        if b.endswith("/api/v1"):
            return f"{b}/services/rerank/text-rerank/text-rerank"
        return f"{b}{self.DEFAULT_PATH}"

    def _call(self, payload: dict) -> dict:
        """发一次 rerank 请求（**单一注入缝**：测试可替换 transport）。

        **不含重试**：重试/退避/熔断由 ``ProviderGateway`` 承担（Step 12）。
        HTTP 错误保留 ``HTTPError`` 原型抛出，让 gateway 能按状态码分类
        （403 AccessDenied = 永久错误，零重试）。
        """
        if self._transport is not None:
            return self._transport(payload)  # type: ignore[no-any-return]
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            self._endpoint(),
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=self._resolve_timeout()) as raw:
            return json.loads(raw.read().decode("utf-8"))  # type: ignore[no-any-return]

    def _resolve_timeout(self) -> float:
        """超时口径单一来源：gateway 的 provider 画像，取不到时回落构造参数。"""
        try:
            return self._resolve_gateway().timeout_for(ProviderKind.BAILIAN_RERANK)
        except KeyError:
            return self.timeout_seconds

    @staticmethod
    def _extract_results(response: dict) -> list[dict]:
        results = response.get("results")
        if isinstance(results, list):
            return results  # type: ignore[no-any-return]
        output = response.get("output") or {}
        results = output.get("results")
        if isinstance(results, list):
            return results  # type: ignore[no-any-return]
        raise ValueError("Bailian rerank response contains no results")

    def rerank(self, query: str, candidates: list[RetrievedClause], top_k: int) -> list[RetrievedClause]:
        """真实 rerank。

        P2.5 Step 12：经 ``ProviderGateway`` —— 限流、预算预留、有界重试
        （仅瞬时错误）、熔断与 usage 对账。**403 AccessDenied 是永久错误，
        零重试并抛 ``PROVIDER_AUTH_FAILED``；绝不静默回退 LocalReranker**
        （那正是规格 §1.1 禁止的 silent fallback）。
        """
        if not candidates:
            return []
        documents = [c.text or c.title or "" for c in candidates]
        payload = {
            "model": self.model,
            "input": {
                "query": query,
                "documents": documents,
            },
            "parameters": {
                "top_n": top_k,
            },
        }
        call = ProviderCall(
            provider=ProviderKind.BAILIAN_RERANK,
            category=CallCategory.RERANK,
            model=self.model,
            temperature=0.0,
            estimated_input_tokens=len(query) + sum(len(d) for d in documents),
            required_capability=Capability.RERANK,
        )

        def executor() -> tuple[dict, ProviderUsage]:
            response = self._call(payload)
            # rerank 接口通常不返回 token usage → unknown（不估算）
            return response, ProviderUsage.unknown()

        response = self._resolve_gateway().call(self._resolve_context(), call, executor).value
        results = self._extract_results(response)
        ordered = sorted(results, key=lambda r: float(r.get("relevance_score", 0.0)), reverse=True)
        out: list[RetrievedClause] = []
        for rank_item in ordered[:top_k]:
            idx = int(rank_item.get("index", -1))
            if idx < 0 or idx >= len(candidates):
                continue
            # 同 LocalReranker：返回副本，不改写入参对象
            out.append(candidates[idx].model_copy(update={"score": float(rank_item.get("relevance_score", 0.0))}))
        return out


def build_bailian_reranker(
    *,
    api_key: str | None = None,
    model: str = BailianReranker.DEFAULT_MODEL,
    base_url: str = BailianReranker.DEFAULT_BASE_URL,
    transport: Any = None,
) -> BailianReranker:
    key = api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY", "")
    return BailianReranker(api_key=key, model=model, base_url=base_url, transport=transport)


class RerankingRetriever(Retriever):
    """把"召回 recall_top_k → rerank 截断 top_k"包成一个 Retriever。

    legacy 图在 retrieve 节点里做这两步；agentic 图的 retrieve_clauses 工具只认
    ``Retriever.retrieve``，用这个适配器让两条路径的检索口径一致。
    """

    def __init__(self, inner: Retriever, reranker: Reranker, *, recall_top_k: int = 20) -> None:
        self._inner = inner
        self._reranker = reranker
        self._recall_top_k = recall_top_k

    def retrieve(self, query: str, top_k: int = 5) -> list[RetrievedClause]:
        candidates = self._inner.retrieve(query, top_k=max(self._recall_top_k, top_k))
        return self._reranker.rerank(query, candidates, top_k=top_k)

    def __getattr__(self, name: str):
        # is_fallback / db_error / text_index_ready / close 等透传给内层检索器
        return getattr(self._inner, name)

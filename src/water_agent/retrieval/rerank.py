"""rerank：先在召回更多候选，再用 query 重排取 top_k。

本地确定性实现（无网络依赖）：IdentityReranker、LocalReranker。
云端模型实现：BailianReranker（百炼原生 text-rerank，默认 gte-rerank）。

任何云端 rerank 都必须显式提供 api_key（或 DASHSCOPE_API_KEY），未提供即抛错，
绝不自动本地兜底。
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any
from urllib import request
from urllib.error import HTTPError

from water_agent.domain.models import RetrievedClause


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
    ) -> None:
        if not api_key.strip():
            raise ValueError("BailianReranker requires api_key (no local fallback)")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self._transport = transport

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
        if self._transport is not None:
            return self._transport(payload)
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
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as raw:
                return json.loads(raw.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001, S110 — 错误体读取失败时用空串即可
                pass
            raise RuntimeError(
                f"Bailian rerank HTTP {exc.code} {exc.reason}: {detail}"
            ) from exc

    @staticmethod
    def _extract_results(response: dict) -> list[dict]:
        results = response.get("results")
        if isinstance(results, list):
            return results
        output = response.get("output") or {}
        results = output.get("results")
        if isinstance(results, list):
            return results
        raise ValueError("Bailian rerank response contains no results")

    def rerank(self, query: str, candidates: list[RetrievedClause], top_k: int) -> list[RetrievedClause]:
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
        response = self._call(payload)
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

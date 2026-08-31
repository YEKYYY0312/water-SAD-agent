"""OpenAI 兼容 LLM 生成器（DeepSeek 官方 Claude 兼容接口）。

默认走 DeepSeek 官方 API：base_url=https://api.deepseek.com，model=deepseek-chat。
base_url / api_key / model 均可覆盖，便于接入任意 Claude 兼容服务。

引用只取自检索上下文，绝不编造来源；未配置 api_key 或构造即校验。
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import request

from water_agent.domain.models import Citation, GeneratedAnswer, RetrievedClause
from water_agent.generation.base import Generator

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MAX_CITATIONS = 5
DEFAULT_SYSTEM_PROMPT = (
    "你是给排水专业标准助手。请依据提供的检索条款回答问题，仅能引用出现在检索结果里的条款，"
    "不得编造规范号或条款内容。回答要专业、简洁、可追溯。"
)


@dataclass(frozen=True, slots=True)
class LLMConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 60.0
    temperature: float = 0.0
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_citations: int = DEFAULT_MAX_CITATIONS

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("LLM api_key must not be empty")
        if self.max_citations < 1:
            raise ValueError("max_citations must be >= 1")


class LLMGenerator(Generator):
    """基于 LLM（Claude 兼容 /chat/completions）生成带引用回答。"""

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self._transport = transport

    def _chat_request(self, query: str, context: list[RetrievedClause]) -> dict[str, Any]:
        if not context:
            return None  # 空上下文不发请求
        context_block = "\n\n".join(
            f"[{i + 1}] {c.source_id}  {c.title or ''}\n{c.text[:400]}"
            for i, c in enumerate(context)
        )
        user_message = (
            f"问题：{query}\n\n"
            f"检索到的条款：\n{context_block}\n\n"
            "请仅依据上述条款回答，并指出你引用了哪些（用其条款号）。"
        )
        return {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.config.system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": self.config.temperature,
        }

    def _completion(self, payload: dict[str, Any]) -> str:
        if self._transport is not None:
            response = self._transport(payload)
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = request.Request(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with request.urlopen(req, timeout=self.config.timeout_seconds) as raw:
                response = json.loads(raw.read().decode("utf-8"))
        choices = response.get("choices") or []
        if not choices:
            raise ValueError("LLM response contains no choices")
        return (choices[0].get("message") or {}).get("content", "")

    def generate(self, query: str, context: list[RetrievedClause]) -> GeneratedAnswer:
        if not context:
            return GeneratedAnswer(
                answer="未检索到相关条款，无法回答。请补充或调整提问。",
                citations=[],
                intent="rag",
            )
        payload = self._chat_request(query, context)
        answer = self._completion(payload)
        citations = [_clause_to_citation(c) for c in context[: self.config.max_citations]]
        return GeneratedAnswer(answer=answer, citations=citations, intent="rag")


def _clause_to_citation(clause: RetrievedClause) -> Citation:
    return Citation(
        spec_id=clause.spec_id,
        article=clause.article,
        title=clause.title,
        text_snippet=clause.text[:60],
    )


def build_llm_generator(
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> LLMGenerator:
    key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
    return LLMGenerator(
        LLMConfig(api_key=key, model=model, base_url=base_url),
        transport=transport,
    )

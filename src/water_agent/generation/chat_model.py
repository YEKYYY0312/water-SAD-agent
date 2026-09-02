"""真实 chat 模型装配（Step 7）：DeepSeek OpenAI 兼容 function calling。

本步只实现 DeepSeek 官方 OpenAI 兼容路径（``ChatOpenAI`` + 自定义
``base_url``），暂不引入多 provider 抽象。缺 Key 必须响亮失败（与
``generation/llm.py`` 的 ``LLMGenerator`` 风格一致），无 Key 时的离线闭环
由 ``--mock-generator`` / fake 承担，禁止静默降级。
"""
from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"


def build_chat_model(
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    temperature: float = 0.0,
) -> BaseChatModel:
    """构造 DeepSeek（OpenAI 兼容）chat model，供 agentic 循环使用。

    规范问答是事实问答（铁律②：回答可追溯、不编造）：``temperature`` 默认 0，
    调用方可显式覆盖。缺 Key 抛 ``ValueError`` 并指明环境变量名——"失败要响亮"，
    不允许静默降级为本地/离线模型。
    """
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"{api_key_env} must not be empty")
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=SecretStr(api_key),
        temperature=temperature,
    )
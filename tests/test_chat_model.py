"""Step 7：真实模型接入（DeepSeek function calling）——chat 模型装配测试。

断言：
1. ``build_chat_model`` 缺 Key 抛错（响亮失败，风格与 ``LLMGenerator`` 一致）；
2. 构造参数透传：model / base_url / api_key / temperature 原样交给 ``ChatOpenAI``；
3. 返回对象是 ``BaseChatModel`` 实例（可被 ``build_agentic_app`` 直接消费）。

全程离线：不发起任何网络请求（用替身类验证调用形状）。
"""
from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from water_agent.generation import chat_model as chat_model_module
from water_agent.generation.chat_model import build_chat_model


def test_build_chat_model_raises_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY must not be empty"):
        build_chat_model()


def test_build_chat_model_uses_custom_api_key_env(monkeypatch) -> None:
    monkeypatch.delenv("MY_LLM_KEY", raising=False)
    with pytest.raises(ValueError, match="MY_LLM_KEY must not be empty"):
        build_chat_model(api_key_env="MY_LLM_KEY")


def _spy_chat_openai(monkeypatch, captured: dict[str, object]) -> type[ChatOpenAI]:
    """用记录构造参数的 ChatOpenAI 子类顶替模块内的 ChatOpenAI。"""

    class _SpyChatOpenAI(ChatOpenAI):
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(chat_model_module, "ChatOpenAI", _SpyChatOpenAI)
    return _SpyChatOpenAI


def test_build_chat_model_passes_through_constructor_args(monkeypatch) -> None:
    captured: dict[str, object] = {}
    _spy_chat_openai(monkeypatch, captured)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-123")
    model = build_chat_model(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        temperature=0.0,
    )
    assert isinstance(model, BaseChatModel), "返回值必须是可被 agentic 图消费的 chat model"
    assert captured["model"] == "deepseek-chat"
    assert captured["base_url"] == "https://api.deepseek.com"
    secret = captured["api_key"]
    assert isinstance(secret, SecretStr), "api_key 应以 SecretStr 交给 ChatOpenAI"
    assert secret.get_secret_value() == "sk-test-123"
    assert captured["temperature"] == 0.0


def test_build_chat_model_defaults_temperature_zero(monkeypatch) -> None:
    captured: dict[str, object] = {}
    _spy_chat_openai(monkeypatch, captured)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-123")
    build_chat_model()
    assert captured["temperature"] == 0.0, "规范问答是事实问答：temperature 必须默认为 0"


def test_build_chat_model_enables_stream_usage(monkeypatch) -> None:
    """流式时让最后一个 chunk 带 usage：自定义 base_url 下 langchain-openai 不会自动开启。

    没有它，流式调用在网关账本里恒为 USAGE_UNKNOWN、预算按 0 结清；端到端效果见
    tests/test_governed_chat_streaming.py::TestChatOpenAIOverLocalSSE。
    """
    captured: dict[str, object] = {}
    _spy_chat_openai(monkeypatch, captured)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-123")
    model = build_chat_model()
    assert captured["stream_usage"] is True
    assert model.inner.stream_usage is True
    assert captured["max_retries"] == 0, "重试只由 ProviderGateway 承担"

"""对话模型与 embedding 端点的解析：唯一来源（P7-S10，规格 §3.11）。

协议层本来就是 OpenAI 兼容（对话走 ``/chat/completions``，检索走 ``/embeddings``），写死的是配置。本模块
只回答"这次该连哪个端点、用哪个模型、读哪把 Key"，**不发任何请求**；CLI / Web / runner / docgen
润色 / judge / 兼容性探针 / 预检全部经它取值——此前预检认一处、真跑用另一处，正是 chat 与
embedding 预检都踩过的坑。

优先级（对话端点）：调用方显式给出（CLI ``--model`` / ``--base-url``）> 运维环境变量
``WATER_AGENT_LLM_BASE_URL`` / ``WATER_AGENT_LLM_MODEL`` > Web 设置面板写的 ``<凭据卷>/llm_endpoint.json``
（管理员，只收 https 与列明的本机例外）> 缺省 DeepSeek。都没设 = 现状，已有部署零变化。embedding 端点对称（``WATER_AGENT_EMBEDDING_*``，缺省百炼 ``qwen3.7-text-embedding``）。

**密钥只发给它所属的服务商**：``WATER_AGENT_LLM_API_KEY`` 未配置时，只有端点仍是 DeepSeek 才回落
``DEEPSEEK_API_KEY``；端点指向别处就不回落（否则会把 DeepSeek 的 Key 发给别的服务商），解析结果
标出缺哪一把。embedding 同理（``WATER_AGENT_EMBEDDING_API_KEY`` → ``DASHSCOPE_API_KEY``）。

``LOCAL_MODEL_*``（P5 Step 46）保留且语义不变：它启用时对话端点就是本地端点；它与
``WATER_AGENT_LLM_*`` 同时启用是配置错误（二选一）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from water_agent.config.credentials import resolve_secret

Getenv = Callable[[str], str | None]

DEFAULT_CHAT_BASE_URL = "https://api.deepseek.com"
DEFAULT_CHAT_MODEL = "deepseek-chat"
DEFAULT_CHAT_API_KEY = "DEEPSEEK_API_KEY"
CHAT_BASE_URL_ENV = "WATER_AGENT_LLM_BASE_URL"
CHAT_MODEL_ENV = "WATER_AGENT_LLM_MODEL"
CHAT_API_KEY = "WATER_AGENT_LLM_API_KEY"

DEFAULT_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_EMBEDDING_MODEL = "qwen3.7-text-embedding"
DEFAULT_EMBEDDING_API_KEY = "DASHSCOPE_API_KEY"
EMBEDDING_BASE_URL_ENV = "WATER_AGENT_EMBEDDING_BASE_URL"
EMBEDDING_MODEL_ENV = "WATER_AGENT_EMBEDDING_MODEL"
EMBEDDING_API_KEY = "WATER_AGENT_EMBEDDING_API_KEY"

#: 端点从哪里来。``explicit`` = 调用方参数（CLI）；``env`` = 运维环境变量；``local_model`` = P5 Step 46；
#: ``default`` = 缺省。
SOURCE_DEFAULT = "default"
SOURCE_ENV = "env"
SOURCE_EXPLICIT = "explicit"
SOURCE_LOCAL_MODEL = "local_model"
SOURCE_PANEL = "panel"

#: Web 设置面板写的对话端点（非密钥配置，与凭据同卷、0600）。
PANEL_ENDPOINT_FILE = "llm_endpoint.json"
#: 面板写端点时 ``http://`` 只收这些主机（本机与 compose 里的 ``local-model`` 服务）；其余一律 https。
PANEL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "local-model"})
MAX_MODEL_NAME_LENGTH = 200
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:/@+\-]+$")


class EndpointConfigError(ValueError):
    """端点配置本身不成立（URL 不合法、两条路径同时启用）。启动期即响亮失败。"""


class PanelEndpointError(EndpointConfigError):
    """面板写的 ``llm_endpoint.json`` 读不出或不成立。不让进程起不来（管理员要能在面板里改正它），
    但解析与预检照样失败（不静默回到缺省端点）。"""


def normalize_base_url(value: str) -> str:
    """去首尾空白与末尾 ``/``，scheme 与主机名小写（路径大小写原样保留）。"""

    cleaned = value.strip().rstrip("/")
    parts = urlsplit(cleaned)
    if not parts.scheme or not parts.netloc:
        return cleaned
    rest = cleaned[len(parts.scheme) + 3 + len(parts.netloc) :]
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{rest}"


def validate_base_url(value: str, *, label: str) -> str:
    """校验并规范化 base_url：只收 http(s)，拒绝 userinfo 与 fragment（Key 或凭据不得藏在 URL 里）。"""

    cleaned = normalize_base_url(value)
    try:
        parts = urlsplit(cleaned)
        port = parts.port
    except ValueError as exc:
        raise EndpointConfigError(f"{label} 不是合法的 URL") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise EndpointConfigError(f"{label} 必须是 http:// 或 https:// 开头的完整地址")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise EndpointConfigError(f"{label} 不得包含用户名或密码")
    if parts.fragment or "#" in cleaned:
        raise EndpointConfigError(f"{label} 不得包含 #fragment")
    if port is not None and not 0 < port < 65536:
        raise EndpointConfigError(f"{label} 的端口不合法")
    return cleaned


def validate_model_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_MODEL_NAME_LENGTH or not _MODEL_NAME_RE.fullmatch(cleaned):
        raise EndpointConfigError(
            f"模型名只能含字母、数字与 . _ : / @ + -，长度 1–{MAX_MODEL_NAME_LENGTH}"
        )
    return cleaned


def validate_panel_base_url(value: str) -> str:
    """面板可写的 base_url：只收 ``https://``（``http://localhost`` / ``127.0.0.1`` / ``local-model`` 例外）。

    面板能写端点就等于能决定把用户问题与检索条文发到哪——与"面板改不了 MCP command"同一级风险，所以只收
    https、要求管理员、改了就必须重新验证（兼容性报告按端点 + 模型精确匹配）。
    """

    cleaned = validate_base_url(value, label="base_url")
    parts = urlsplit(cleaned)
    if parts.scheme == "https":
        return cleaned
    if (parts.hostname or "").lower() in PANEL_HTTP_HOSTS:
        return cleaned
    raise EndpointConfigError(
        "base_url 只接受 https://（本机 http://localhost、http://127.0.0.1 与 compose 服务 http://local-model 例外）"
    )


def panel_endpoint_path() -> Path:
    from water_agent.config.credentials import credentials_dir

    return credentials_dir() / PANEL_ENDPOINT_FILE


def read_panel_endpoint() -> tuple[str, str] | None:
    """面板写的 ``(base_url, model)``；文件不存在为 ``None``，读不出或不成立即 :class:`PanelEndpointError`。"""

    path = panel_endpoint_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"base_url", "model"}:
            raise ValueError("字段必须恰好是 base_url 与 model")
        return validate_panel_base_url(str(payload["base_url"])), validate_model_name(str(payload["model"]))
    except (OSError, ValueError, TypeError) as exc:
        raise PanelEndpointError(f"{PANEL_ENDPOINT_FILE} 不可用（{exc}）；请在设置面板重新保存对话端点") from exc


def write_panel_endpoint(base_url: str, model: str) -> tuple[str, str]:
    """校验并原子写出面板端点（同目录临时文件 + ``os.replace``，0600）。凭据卷不可写时抛 ``OSError``。"""

    cleaned = (validate_panel_base_url(base_url), validate_model_name(model))
    target = panel_endpoint_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(
            descriptor,
            json.dumps({"base_url": cleaned[0], "model": cleaned[1]}, ensure_ascii=False).encode("utf-8"),
        )
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    return cleaned


def endpoint_host(base_url: str) -> str:
    """只取主机名（审计、日志、面板展示用；不带路径与查询串）。"""

    return (urlsplit(base_url).hostname or "").lower()


def _env(getenv: Getenv, name: str) -> str:
    return (getenv(name) or "").strip()


@dataclass(frozen=True, slots=True)
class ChatEndpoint:
    """一次装配要连的对话端点。不含密钥值，只含 Key 的**名字**。"""

    base_url: str
    model: str
    #: 要读的凭据名；``None`` 只出现在本地模型路径（它用固定的占位 Key）。
    api_key_name: str | None
    source: str
    #: 本地模型路径的占位 Key（``ollama``，不是密钥）。其余路径一律为 ``None``。
    api_key_literal: str | None = None

    @property
    def is_default(self) -> bool:
        """端点与模型都与缺省 DeepSeek 相同（兼容性门槛唯一的免检情形）。"""

        return (
            self.source != SOURCE_LOCAL_MODEL
            and normalize_base_url(self.base_url) == DEFAULT_CHAT_BASE_URL
            and self.model == DEFAULT_CHAT_MODEL
        )

    @property
    def host(self) -> str:
        return endpoint_host(self.base_url)

    @property
    def cache_digest(self) -> str:
        """回答缓存键的端点因子：缺省 DeepSeek 端点为空串（与历史键同键），其余为 base_url 的摘要。"""

        normalized = normalize_base_url(self.base_url)
        if normalized == DEFAULT_CHAT_BASE_URL and self.source != SOURCE_LOCAL_MODEL:
            return ""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def api_key(self, *, getenv: Getenv = os.getenv) -> str | None:
        if self.api_key_literal is not None:
            return self.api_key_literal
        if self.api_key_name is None:
            return None
        return resolve_secret(self.api_key_name, getenv=getenv)

    def missing_key(self, *, getenv: Getenv = os.getenv) -> str | None:
        """缺的那把 Key 的名字；已配置时为 ``None``。"""

        return None if self.api_key(getenv=getenv) else self.api_key_name


@dataclass(frozen=True, slots=True)
class EmbeddingEndpoint:
    """检索要连的 embedding 端点（维度由索引决定，不在这里）。"""

    base_url: str
    model: str
    api_key_name: str
    source: str

    @property
    def is_default(self) -> bool:
        return (
            normalize_base_url(self.base_url) == DEFAULT_EMBEDDING_BASE_URL
            and self.model == DEFAULT_EMBEDDING_MODEL
        )

    @property
    def host(self) -> str:
        return endpoint_host(self.base_url)

    def api_key(self, *, getenv: Getenv = os.getenv) -> str | None:
        return resolve_secret(self.api_key_name, getenv=getenv)

    def missing_key(self, *, getenv: Getenv = os.getenv) -> str | None:
        return None if self.api_key(getenv=getenv) else self.api_key_name


def _key_name(
    *, specific: str, fallback: str, base_url: str, default_base_url: str, getenv: Getenv
) -> str:
    """专用 Key 配了就用它；没配时只有端点仍是缺省服务商才回落旧 Key——密钥只发给所属服务商。"""

    if resolve_secret(specific, getenv=getenv):
        return specific
    if normalize_base_url(base_url) == default_base_url:
        return fallback
    return specific


def chat_env_configured(getenv: Getenv = os.getenv) -> bool:
    """运维是否经环境变量选了对话端点或模型（与本地模型路径互斥的判断用）。"""

    return bool(_env(getenv, CHAT_BASE_URL_ENV) or _env(getenv, CHAT_MODEL_ENV))


def resolve_chat_endpoint(
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    getenv: Getenv = os.getenv,
) -> ChatEndpoint:
    """解析本次要连的对话端点。参数是调用方的显式覆盖（CLI），``None`` 表示没给。

    ``api_key_env`` 显式给出时（CLI ``--api-key-env``）就读那个名字，不再套回落规则——那是调用方
    自己指定的 Key。
    """

    from water_agent.generation.local_model import (
        LOCAL_MODEL_DEFAULT_API_KEY,
        local_model_configured,
        local_model_settings,
    )

    explicit_base = (base_url or "").strip()
    explicit_model = (model or "").strip()
    env_base = _env(getenv, CHAT_BASE_URL_ENV)
    env_model = _env(getenv, CHAT_MODEL_ENV)
    panel = None if env_base or env_model else read_panel_endpoint()
    if local_model_configured(getenv):
        if env_base or env_model:
            raise EndpointConfigError(
                f"LOCAL_MODEL_ENABLED 与 {CHAT_BASE_URL_ENV} / {CHAT_MODEL_ENV} 不能同时启用："
                "本地模型路径（P5 Step 46）与 OpenAI 兼容端点二选一"
            )
        if panel is not None:
            raise PanelEndpointError(
                "LOCAL_MODEL_ENABLED 与设置面板配置的对话端点不能同时启用：二选一（关掉本地模型，或在面板恢复缺省）"
            )
        if not explicit_base and not explicit_model:
            settings = local_model_settings(getenv)
            return ChatEndpoint(
                base_url=validate_base_url(str(settings["base_url"]), label="LOCAL_MODEL_BASE_URL"),
                model=str(settings["model"]),
                api_key_name=None,
                source=SOURCE_LOCAL_MODEL,
                api_key_literal=LOCAL_MODEL_DEFAULT_API_KEY,
            )

    panel_base, panel_model = panel if panel is not None else ("", "")
    if explicit_base or explicit_model:
        source = SOURCE_EXPLICIT
    elif env_base or env_model:
        source = SOURCE_ENV
    elif panel is not None:
        source = SOURCE_PANEL
    else:
        source = SOURCE_DEFAULT
    resolved_base = validate_base_url(
        explicit_base or env_base or panel_base or DEFAULT_CHAT_BASE_URL,
        label="--base-url" if explicit_base else CHAT_BASE_URL_ENV,
    )
    resolved_model = explicit_model or env_model or panel_model or DEFAULT_CHAT_MODEL
    key_name = (api_key_env or "").strip() or _key_name(
        specific=CHAT_API_KEY,
        fallback=DEFAULT_CHAT_API_KEY,
        base_url=resolved_base,
        default_base_url=DEFAULT_CHAT_BASE_URL,
        getenv=getenv,
    )
    return ChatEndpoint(
        base_url=resolved_base, model=resolved_model, api_key_name=key_name, source=source
    )


def resolve_embedding_endpoint(*, getenv: Getenv = os.getenv) -> EmbeddingEndpoint:
    """解析检索要连的 embedding 端点（运维环境变量 > 缺省百炼）。"""

    env_base = _env(getenv, EMBEDDING_BASE_URL_ENV)
    env_model = _env(getenv, EMBEDDING_MODEL_ENV)
    resolved_base = validate_base_url(
        env_base or DEFAULT_EMBEDDING_BASE_URL, label=EMBEDDING_BASE_URL_ENV
    )
    return EmbeddingEndpoint(
        base_url=resolved_base,
        model=env_model or DEFAULT_EMBEDDING_MODEL,
        api_key_name=_key_name(
            specific=EMBEDDING_API_KEY,
            fallback=DEFAULT_EMBEDDING_API_KEY,
            base_url=resolved_base,
            default_base_url=DEFAULT_EMBEDDING_BASE_URL,
            getenv=getenv,
        ),
        source=SOURCE_ENV if env_base or env_model else SOURCE_DEFAULT,
    )


__all__ = [
    "CHAT_API_KEY",
    "CHAT_BASE_URL_ENV",
    "CHAT_MODEL_ENV",
    "DEFAULT_CHAT_API_KEY",
    "DEFAULT_CHAT_BASE_URL",
    "DEFAULT_CHAT_MODEL",
    "DEFAULT_EMBEDDING_API_KEY",
    "DEFAULT_EMBEDDING_BASE_URL",
    "DEFAULT_EMBEDDING_MODEL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_BASE_URL_ENV",
    "EMBEDDING_MODEL_ENV",
    "ChatEndpoint",
    "EmbeddingEndpoint",
    "EndpointConfigError",
    "PanelEndpointError",
    "chat_env_configured",
    "endpoint_host",
    "normalize_base_url",
    "panel_endpoint_path",
    "read_panel_endpoint",
    "resolve_chat_endpoint",
    "resolve_embedding_endpoint",
    "validate_base_url",
    "validate_model_name",
    "validate_panel_base_url",
    "write_panel_endpoint",
]

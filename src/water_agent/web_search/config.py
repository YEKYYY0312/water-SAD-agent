"""联网搜索的部署配置（P7-S12，规格 ``docs/design/17-p7-external-capabilities.md`` §5.2）。

两道门里的第一道——**运维**的部署级开关。全部来自环境变量（运维写死），Web 面板**不能**写其中任何一项
（被盗的管理员账号不能把用户问题改道，SDD D3）；面板最多写密钥槽位。

- ``WATER_AGENT_WEB_SEARCH``：``off``（缺省，空值同）/ ``on``；写错（``yes`` / ``1`` ……）装配期响亮失败；
- ``WATER_AGENT_WEB_SEARCH_PROVIDER``：``searxng`` / ``brave``；拼错装配期失败，**不静默回退**到别的 provider；
- ``WATER_AGENT_WEB_SEARCH_BASE_URL``：搜索服务的基址（**唯一**的出口，模型无法影响）；
- ``WATER_AGENT_WEB_SEARCH_API_KEY``：有密钥的 provider（brave）的凭据槽位（env > ``_FILE`` > 凭据卷，
  ``config/credentials.py`` 三层解析）；无密钥的 provider（searxng）**不读它**——不因别的 provider 有密钥而被自动选用；
- ``WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE``：运维显式 ``1`` 才放行回环与私有网段（searxng 自托管指向内网）与明文 http；
  链路本地与云元数据地址**永远**拒绝（``open_exchange`` 既有语义）。

缺任一必需项 → ``status="skipped"``（能力不可用就不暴露，沿用 ``read_drawing`` 的策略）；开关取值非法 / provider id 拼错 →
抛 :class:`WebSearchConfigError`（稳定码 ``WEB_SEARCH_CONFIG_INVALID``）。基址的形状与目的地只在请求期判定（见下）。

本模块不 import 网络原语、模型或插件，只读环境。
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from water_agent.config.credentials import resolve_secret

SWITCH_ENV = "WATER_AGENT_WEB_SEARCH"
PROVIDER_ENV = "WATER_AGENT_WEB_SEARCH_PROVIDER"
BASE_URL_ENV = "WATER_AGENT_WEB_SEARCH_BASE_URL"
API_KEY_ENV = "WATER_AGENT_WEB_SEARCH_API_KEY"
ALLOW_PRIVATE_ENV = "WATER_AGENT_WEB_SEARCH_ALLOW_PRIVATE"

#: 稳定错误码（装配期）。
WEB_SEARCH_CONFIG_INVALID = "WEB_SEARCH_CONFIG_INVALID"

#: 支持的 provider。哪些要密钥写在这里（单一来源）：适配器模块只管请求 / 响应，不管凭据。
PROVIDERS_NEEDING_KEY: frozenset[str] = frozenset({"brave"})
PROVIDERS: frozenset[str] = frozenset({"searxng", "brave"})

SwitchState = Literal["off", "on"]
Status = Literal["off", "ready", "skipped"]


class WebSearchConfigError(ValueError):
    """部署配置非法（开关取值错、provider id 拼错）。不是"缺配置"——缺配置只是 skipped。"""

    code = WEB_SEARCH_CONFIG_INVALID


@dataclass(frozen=True, slots=True)
class WebSearchConfig:
    """一次解析的结果。``status``：``off`` 开关关闭；``skipped`` 开关开了但缺项（``detail`` 写缺什么）；
    ``ready`` 可以注册工具。``api_key`` 只在 ``ready`` 且 provider 需要密钥时非空，且**永不**进 ``repr`` / trace。"""

    status: Status
    provider: str = ""
    base_url: str = ""
    allow_private: bool = False
    detail: str = ""
    api_key: str = ""

    def __repr__(self) -> str:  # 不泄漏密钥 / 基址
        return f"WebSearchConfig(status={self.status!r}, provider={self.provider!r}, detail={self.detail!r})"

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def host(self) -> str:
        """基址的主机名（审计 / 熔断键 / 面板只读状态用）；不含路径、端口外的任何部分。"""

        if not self.base_url:
            return ""
        from urllib.parse import urlsplit

        try:
            return (urlsplit(self.base_url).hostname or "").lower()
        except ValueError:
            return ""


def resolve_switch(environ: Mapping[str, str]) -> SwitchState:
    """读部署开关。空值即 ``off``；写错不静默当成关闭或打开。"""

    value = (environ.get(SWITCH_ENV) or "").strip().lower()
    if value in ("", "off"):
        return "off"
    if value == "on":
        return "on"
    raise WebSearchConfigError(f"{SWITCH_ENV} 只接受 off / on，收到 {value!r}")


def resolve_web_search_config(
    environ: Mapping[str, str] | None = None,
    *,
    getenv: Callable[[str], str | None] | None = None,
) -> WebSearchConfig:
    """解析部署配置。``environ`` 缺省取 ``os.environ``；密钥经三层解析（env > ``_FILE`` > 凭据卷）。"""

    env: Mapping[str, str] = os.environ if environ is None else environ
    lookup = getenv if getenv is not None else (lambda name: env.get(name))
    if resolve_switch(env) == "off":
        return WebSearchConfig(status="off")

    provider = (env.get(PROVIDER_ENV) or "").strip().lower()
    if provider and provider not in PROVIDERS:
        raise WebSearchConfigError(f"{PROVIDER_ENV} 只接受 {'/'.join(sorted(PROVIDERS))}，收到 {provider!r}")
    base_url = (env.get(BASE_URL_ENV) or "").strip()
    allow_private = (env.get(ALLOW_PRIVATE_ENV) or "").strip() == "1"

    if not provider:
        return WebSearchConfig(status="skipped", detail="provider_missing")
    if not base_url:
        return WebSearchConfig(status="skipped", provider=provider, detail="base_url_missing")
    # 基址的形状与目的地**不在这里校验**：只在每次请求时由 governed_get（parse_server_url + open_exchange）判定，
    # 禁止即零字节、映射成 WEB_SEARCH_UNAVAILABLE。装配期再校验一遍是第二个真源，而且会让联网搜索的一个 URL 配错
    # 拖垮所有没勾选联网的会话（装配期失败）。
    api_key = ""
    if provider in PROVIDERS_NEEDING_KEY:
        api_key = resolve_secret(API_KEY_ENV, getenv=lookup) or ""
        if not api_key:
            return WebSearchConfig(
                status="skipped",
                provider=provider,
                base_url=base_url,
                allow_private=allow_private,
                detail="api_key_missing",
            )
    return WebSearchConfig(
        status="ready",
        provider=provider,
        base_url=base_url,
        allow_private=allow_private,
        api_key=api_key,
    )


__all__ = [
    "ALLOW_PRIVATE_ENV",
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "PROVIDERS",
    "PROVIDERS_NEEDING_KEY",
    "PROVIDER_ENV",
    "SWITCH_ENV",
    "WEB_SEARCH_CONFIG_INVALID",
    "WebSearchConfig",
    "WebSearchConfigError",
    "resolve_switch",
    "resolve_web_search_config",
]

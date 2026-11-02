"""回合污点与终答网址追溯（P7-S11，规格 ``docs/design/17-p7-external-capabilities.md`` §4.3 / §5.6）。

工具声明了 ``result_source=SourceTag.WEB_CONTENT`` ⇒ 本回合 ``web_tainted``。污点的确定性效应都在别处执行，这里只提供
纯函数：

- :data:`DISCLOSURE_LINE`：披露行文案。**由程序追加，不由模型生成**（模型自己写的文本里没有它）；
- :func:`normalize_url` / :func:`extract_urls` / :func:`unverified_urls`：终答里出现的 http(s) 网址必须来自本 thread 工具返回的
  网址集合（``web_refs``）或用户自己消息里的网址，否则回流一次、再失败即 ``failed``（复用引用校验的回流机制）；
- :func:`web_tool_names`：注册表里声明了 ``WEB_CONTENT`` 的工具名。

**局限**：网址抽取是正则，会漏掉被刻意拆开 / 编码的写法；这条校验防的是"模型凭空编出一个网址当依据"，
不是"任意方式的外泄"（外泄由 S12 的查询出口守卫与前端不加载外部图片分别处理）。
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from water_agent.runtime.governance.contracts import SourceTag

#: 披露行（固定文案，程序追加；前端同样据此展示徽记）。
DISCLOSURE_LINE = "【联网信息】本回答引用了联网搜索摘录，未经规范核验，不构成设计依据。"

#: 回流给模型的校验反馈（保持稳定，供测试与观测识别）。不含任何网址。
WEB_URL_UNVERIFIED_FEEDBACK = (
    "回答里出现了不是本次联网工具返回、也不是用户提供的网址。不要编造或凭记忆写网址；"
    "只能引用工具结果里出现过的网址，或删去该网址后重写。"
)

_URL_RE = re.compile(r"https?://[^\s<>\"'`　，。；：！？、）】》」』]+", re.IGNORECASE)
#: 网址末尾常被正文标点粘住（句号、右括号、逗号……），不属于网址本身。
_TRAILING = ".,;:!?)]}>'\"、，。；：！？）】》」』"


def normalize_url(url: str) -> str:
    """规范化：去 fragment、scheme 与 host 小写、去尾部标点与尾斜杠（根路径保留）。无法解析的原样（去标点）返回。"""

    cleaned = url.strip().rstrip(_TRAILING)
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return cleaned
    if not parts.scheme or not parts.netloc:
        return cleaned
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else parts.path
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def extract_urls(text: str) -> list[str]:
    """文本里出现的 http(s) 网址（规范化后，保序去重）。"""

    seen: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = normalize_url(match.group(0))
        if url and url not in seen:
            seen.append(url)
    return seen


def unverified_urls(answer: str, web_refs: Iterable[str], *, user_text: str = "") -> list[str]:
    """终答里不在 ``web_refs`` ∪ 用户消息网址里的网址（规范化后）。空列表 = 全部可追溯。"""

    allowed = {normalize_url(ref) for ref in web_refs} | set(extract_urls(user_text))
    return [url for url in extract_urls(answer) if url not in allowed]


def web_tool_names(registry: object) -> frozenset[str]:
    """注册表里声明了 ``result_source=WEB_CONTENT`` 的工具名。``registry`` 只需要 ``list_all_names()`` / ``get()``。"""

    names = registry.list_all_names()  # type: ignore[attr-defined]
    return frozenset(
        name for name in names if getattr(registry.get(name), "result_source", None) is SourceTag.WEB_CONTENT  # type: ignore[attr-defined]
    )


__all__ = [
    "DISCLOSURE_LINE",
    "WEB_URL_UNVERIFIED_FEEDBACK",
    "extract_urls",
    "normalize_url",
    "unverified_urls",
    "web_tool_names",
]

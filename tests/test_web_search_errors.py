"""P7-S12 错误模板：7 个稳定码；S11 内核的 ``EXTERNAL_*`` 一一映射；文本只含码与一句固定的话。"""
from __future__ import annotations

import re

import pytest

from water_agent.external.http_get import EXTERNAL_ERROR_CODES
from water_agent.web_search.errors import (
    WEB_SEARCH_AUTH,
    WEB_SEARCH_BAD_RESPONSE,
    WEB_SEARCH_ERROR_CODES,
    WEB_SEARCH_NOT_EXPOSED,
    WEB_SEARCH_QUERY_REJECTED,
    WEB_SEARCH_QUOTA,
    WEB_SEARCH_RATE_LIMITED,
    WEB_SEARCH_UNAVAILABLE,
    code_from_external,
    error_result,
    message_for,
)

_URLISH = re.compile(r"https?://|ftp://|www\.|\.com\b|\.cn\b|\d+\.\d+\.\d+\.\d+")


def test_the_code_set_is_exactly_seven() -> None:
    """P7-S12-A09 稳定码恰为 7 个（改名即破坏前端与评测的契约）。"""

    assert frozenset({
        WEB_SEARCH_UNAVAILABLE, WEB_SEARCH_AUTH, WEB_SEARCH_RATE_LIMITED, WEB_SEARCH_BAD_RESPONSE,
        WEB_SEARCH_QUERY_REJECTED, WEB_SEARCH_QUOTA, WEB_SEARCH_NOT_EXPOSED,
    }) == WEB_SEARCH_ERROR_CODES
    assert len(WEB_SEARCH_ERROR_CODES) == 7


def test_every_kernel_code_maps_into_the_web_search_namespace() -> None:
    """P7-S12-A09 S11 的 7 个 ``EXTERNAL_*`` 都映射到 ``WEB_SEARCH_*``（不泄漏内核命名空间）；未知码一律"不可用"。"""

    assert len(EXTERNAL_ERROR_CODES) == 7
    for code in EXTERNAL_ERROR_CODES:
        mapped = code_from_external(code)
        assert mapped in WEB_SEARCH_ERROR_CODES and not mapped.startswith("EXTERNAL_")
    assert code_from_external("EXTERNAL_AUTH") == WEB_SEARCH_AUTH
    assert code_from_external("EXTERNAL_RATE_LIMITED") == WEB_SEARCH_RATE_LIMITED
    assert code_from_external("EXTERNAL_DESTINATION_FORBIDDEN") == WEB_SEARCH_UNAVAILABLE
    assert code_from_external("EXTERNAL_TOO_LARGE") == WEB_SEARCH_BAD_RESPONSE
    assert code_from_external("SOMETHING_NEW") == WEB_SEARCH_UNAVAILABLE
    assert code_from_external("") == WEB_SEARCH_UNAVAILABLE


@pytest.mark.parametrize("code", sorted(WEB_SEARCH_ERROR_CODES))
def test_messages_are_fixed_and_contain_no_upstream_material(code: str) -> None:
    """P7-S12-A09 每个码的文案非空、固定、不含网址 / 主机 / 内核码前缀。"""

    text = message_for(code)
    assert text.strip()
    assert not _URLISH.search(text), text
    assert "EXTERNAL_" not in text and "MCP_" not in text


def test_error_result_shape_and_unknown_codes() -> None:
    """P7-S12-A09 错误结果只有 ``ok`` / ``error`` / ``code`` / ``message``（没有 ``urls``：请求没成功）；未知码 ``ValueError``。"""

    result = error_result(WEB_SEARCH_AUTH)
    assert result == {"ok": False, "error": True, "code": WEB_SEARCH_AUTH, "message": message_for(WEB_SEARCH_AUTH)}
    assert "urls" not in result
    with pytest.raises(ValueError):
        error_result("WEB_SEARCH_SOMETHING")

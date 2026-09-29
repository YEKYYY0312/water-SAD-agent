"""全局测试夹具。

P5-S52：文本层为空的 PDF 会触发视觉审阅——一次**计费的远程调用**。本机/CI 只要设了
DASHSCOPE_API_KEY，任何上传扫描件 PDF 的测试都会悄悄花钱、变慢、依赖外网。默认关掉；
需要走视觉链路的测试显式 ``monkeypatch.setenv("UPLOAD_VISION_ENABLED", "true")`` 并注入
假的 reviewer / transport。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_billable_vision_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_VISION_ENABLED", "false")

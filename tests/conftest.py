"""全局测试夹具。

P5-S52：文本层为空的 PDF 会触发视觉审阅——一次**计费的远程调用**。本机/CI 只要设了
DASHSCOPE_API_KEY，任何上传扫描件 PDF 的测试都会悄悄花钱、变慢、依赖外网。默认关掉；
需要走视觉链路的测试显式 ``monkeypatch.setenv("UPLOAD_VISION_ENABLED", "true")`` 并注入
假的 reviewer / transport。

P7-S0 H0：测的必须是**本检出**的代码。worktree 里 ``_venv`` 的 editable install 指向主检出，
不设 ``PYTHONPATH=src`` 时 ``import water_agent`` 拿到的是主检出的源码——测试照样全绿，
验证的却是别的代码。``pytest_configure`` 在收集任何测试之前核对导入位置，不一致直接
报错退出。这里只用 ``importlib.util.find_spec`` 定位，不 import ``water_agent``：守卫自身
不能依赖被检查的那份代码。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_CHECKOUT_SRC = (Path(__file__).resolve().parents[1] / "src").resolve()


def water_agent_import_location_error() -> str | None:
    """``water_agent`` 不是从本检出的 ``src/`` 导入时返回说明，否则返回 ``None``。"""

    spec = importlib.util.find_spec("water_agent")
    origin = Path(spec.origin).resolve() if spec is not None and spec.origin else None
    expected = _CHECKOUT_SRC / "water_agent" / "__init__.py"
    if origin == expected:
        return None
    return (
        f"water_agent 导入自 {origin}，不是本检出的 {expected}。"
        "在 worktree 里跑测试请先设 PYTHONPATH=src（门禁 scripts/ci_gate.sh 已自动设置）。"
    )


def pytest_configure(config: pytest.Config) -> None:
    error = water_agent_import_location_error()
    if error is not None:
        raise pytest.UsageError(error)


@pytest.fixture(autouse=True)
def _no_billable_vision_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_VISION_ENABLED", "false")

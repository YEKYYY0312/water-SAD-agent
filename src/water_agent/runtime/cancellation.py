"""协作式取消：取消请求在内层工具循环的下一步就生效。

Web 的取消此前只在 ``astream`` 的事件回调里检查：内层 react 子图是同步节点、跑在执行器
线程里，取消之后它照样把剩下的模型调用和工具调用全部跑完（review 2026-09-30 实测：取消
时已调用模型 1 次，最终跑满 5 次），继续计费、继续执行工具。

检查函数经 ``ContextVar`` 下传：LangGraph 在执行器线程与子图任务里都会复制调用方的
context，所以在 ``AgenticSession.aask`` 里设一次，模型调用（``_BudgetChatModel``）、工具
执行（``ds_adapters``）和流式分块之间都能读到。没有设置时 :func:`raise_if_cancelled`
什么都不做（CLI、harness 不受影响）。
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CANCEL_CHECK: ContextVar[Callable[[], bool] | None] = ContextVar(
    "water_agent_cancel_check", default=None
)


class RunCancelled(RuntimeError):
    """Raised cooperatively when a Web run is cancelled."""


@contextmanager
def cancellation_scope(check: Callable[[], bool] | None) -> Iterator[None]:
    """在当前 context 内登记取消检查函数；退出时恢复原值。"""

    token = _CANCEL_CHECK.set(check)
    try:
        yield
    finally:
        _CANCEL_CHECK.reset(token)


def raise_if_cancelled() -> None:
    """已请求取消则抛 :class:`RunCancelled`；未登记检查函数时什么都不做。"""

    check = _CANCEL_CHECK.get()
    if check is not None and check():
        raise RunCancelled()


__all__ = ["RunCancelled", "cancellation_scope", "raise_if_cancelled"]

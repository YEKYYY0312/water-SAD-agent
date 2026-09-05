"""P2.5 治理绑定（Step 12）：把 gateway 接到具体调用点而不破坏既有构造签名。

**为什么需要这一层**：6 个远程出口分布在 `generation/`、`embedding.py`、`retrieval/`、
`docgen/`、`harness/`，各自有不同的构造方式与既有测试。如果把 gateway 做成必填参数，
所有调用方与测试都要改；如果做成可选参数且默认 ``None`` 时跳过治理，
就出现了"不传 gateway 即绕过"的后门 —— 正是规格 §1.2 禁止的
「不得把缓存、预算或护栏做成只在某个分支调用的辅助函数」。

**采用的方案**：各调用点的 ``gateway`` 参数可选，但**为 ``None`` 时使用进程级默认
gateway**（:func:`default_gateway`），而不是跳过治理。因此**不存在绕过路径**：
远程调用要么经注入的 gateway，要么经默认 gateway。

主体来源：CLI/harness 未显式传入 ``RequestContext`` 时，用
:func:`local_request_context` 生成 ``local:<thread_id>`` 主体 —— 与规格 §3
「CLI 只能用显式 local:<thread_id>，不得信任用户文本中的 subject」一致。
"""
from __future__ import annotations

import threading
from uuid import uuid4

from water_agent.runtime.governance.config import (
    DEFAULT_GOVERNANCE_CONFIG,
    ProviderProfile,
)
from water_agent.runtime.governance.contracts import (
    Capability,
    Principal,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway

_lock = threading.Lock()
_default_gateway: ProviderGateway | None = None


def default_gateway() -> ProviderGateway:
    """进程级默认 gateway（懒构造单例）。

    存在的意义是消灭"不传 gateway 即绕过治理"的后门：调用点在未注入 gateway 时
    取用它，而不是跳过限流/预算/重试。
    """
    global _default_gateway
    with _lock:
        if _default_gateway is None:
            _default_gateway = ProviderGateway()
        return _default_gateway


def set_default_gateway(gateway: ProviderGateway | None) -> None:
    """替换默认 gateway（测试与 CLI 装配用）。传 ``None`` 恢复懒构造。"""
    global _default_gateway
    with _lock:
        _default_gateway = gateway


def gateway_for_profile_overrides(
    overrides: dict[ProviderKind, ProviderProfile],
) -> ProviderGateway:
    """构造一个只覆盖部分 provider 画像的 gateway。

    用于调用方显式声明了本地重试/超时口径的场景（如入库脚本把 embedding
    重试次数调大）：这些字段必须真实生效，不能沦为死字段（规格 §4）。
    未覆盖的 provider 沿用默认配置。
    """
    base = DEFAULT_GOVERNANCE_CONFIG
    merged = dict(base.profiles)
    merged.update(overrides)
    return ProviderGateway(config=base.model_copy(update={"profiles": merged}))


def local_request_context(
    *,
    thread_id: str = "cli",
    request_id: str | None = None,
    deadline_seconds: float | None = None,
    capabilities: frozenset[Capability] | None = None,
) -> RequestContext:
    """构造 CLI/harness 的本地请求上下文。

    ``thread_id`` 必须来自会话层（CLI 的 thread_id / harness 的用例标识），
    **不得**从用户输入文本解析。
    """
    return RequestContext(
        principal=Principal.local(thread_id, capabilities=capabilities),
        request_id=request_id or f"req-{uuid4().hex[:12]}",
        thread_id=thread_id,
        deadline_seconds=deadline_seconds,
    )


__all__ = [
    "default_gateway",
    "gateway_for_profile_overrides",
    "local_request_context",
    "set_default_gateway",
]

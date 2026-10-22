"""联网搜索的审计出口（P7-S12，规格 §5.9）。

审计事件 ``web_search.performed`` 是长期留存的，比 trace 更敏感：**只记**查询哈希 + 长度、主体哈希、provider id、
provider 主机名、条数、结果码；**不记**查询原文、网址、密钥。字段白名单由 :data:`AUDIT_FIELDS` 固定，
:func:`emit_performed` 会丢弃白名单之外的键（调用方多传了东西也进不了审计）。

审计函数本身在 ``webapp/auth/audit.py``（与其他审计事件同一个出口）。这里只持有一个可替换的 sink——与
``mcp/oauth.py::set_event_sink`` 同一做法，让 ``web_search/`` 不反向依赖 ``webapp/``。没有 sink（CLI / 评测的
纯会话路径）时是空操作。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

#: 审计事件的字段白名单（不含任何原文、网址、密钥）。
AUDIT_FIELDS: frozenset[str] = frozenset(
    {"principal_hash", "query_hash", "query_length", "provider", "host", "count", "result"}
)

AuditSink = Callable[..., None]
_sink: AuditSink | None = None


def set_audit_sink(sink: AuditSink | None) -> None:
    """登记（或清除）审计 sink。``webapp/auth/audit.py`` 在导入时登记自己的 ``audit_web_search_performed``。"""

    global _sink
    _sink = sink


def query_hash(query: str) -> str:
    """与 ``JsonTraceRecorder.hash_input`` 同口径（sha256 前 16 位）。"""

    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def emit_performed(**fields: Any) -> None:
    """发一条 ``web_search.performed``。白名单之外的键被丢弃；sink 抛错也不影响搜索（审计失败不改变运行结果）。"""

    sink = _sink
    if sink is None:
        return
    safe = {key: value for key, value in fields.items() if key in AUDIT_FIELDS}
    try:
        sink(**safe)
    except Exception:  # noqa: BLE001, S110 — 审计失败不得改变搜索结果
        pass


__all__ = ["AUDIT_FIELDS", "AuditSink", "emit_performed", "query_hash", "set_audit_sink"]

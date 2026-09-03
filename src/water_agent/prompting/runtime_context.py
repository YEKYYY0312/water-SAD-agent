"""运行时上下文投影（Step 8）。

渲染「当前 cwd / 版本状态 / 时间 / 会话引用」快照，**与上次相同则跳过注入**
（省 token），变化才作为一条消息注入。这是上下文工程里"变化才注入"的最小实现。

用法（每轮调用前渲染一次）：

    proj = RuntimeContextProjection()
    text = proj.project(ctx_dict)   # ctx 变化 -> 返回渲染文本；未变化 -> None
    if text:
        inject(HumanMessage(content=f"[运行时上下文]\\n{text}"))
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

# 参与哈希/渲染的上下文键（与渲染顺序一致，保证渲染稳定利于对比）
_RENDER_ORDER: tuple[str, ...] = ("cwd", "version_status", "session_refs", "extra")
_LABELS: dict[str, str] = {
    "cwd": "工作目录",
    "version_status": "规范版本状态",
    "session_refs": "会话引用",
    "extra": "附加信息",
}


class RuntimeContextProjection:
    """运行时上下文投影器：上下文不变则 ``project`` 返回 None（不注入）。"""

    def __init__(self) -> None:
        self._last_hash: str | None = None

    def project(self, current: dict[str, Any] | None = None) -> str | None:
        """投影当前上下文。

        - 上下文与上次相同：返回 ``None``（调用方跳过注入）；
        - 上下文变化（或首次调用）：返回渲染文本并记录新哈希。
        """
        rendered = self.render(current or {})
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        if digest == self._last_hash:
            return None
        self._last_hash = digest
        return rendered

    def render(self, context: dict[str, Any]) -> str:
        """把上下文字典渲染成稳定顺序的文本快照（不含时间戳等易变项）。"""
        parts: list[str] = []
        for key in _RENDER_ORDER:
            value = context.get(key)
            label = _LABELS.get(key, key)
            if key == "version_status":
                lines = self._render_version_status(value)
                if lines:
                    parts.append(f"{label}：\n" + "\n".join(lines))
            elif key == "session_refs":
                if value:
                    parts.append(f"{label}：{', '.join(str(v) for v in value)}")
            elif value not in (None, "", {}):
                parts.append(f"{label}：{value}")
        return "\n".join(parts)

    def _render_version_status(self, value: Any) -> list[str]:
        if not isinstance(value, dict) or not value:
            return []
        return [f"  {spec}: {status}" for spec, status in sorted(value.items())]

    def reset(self) -> None:
        """重置记忆（新会话时用，保证首条上下文一定注入）。"""
        self._last_hash = None


def default_context_builder(
    *,
    version_registry: Any | None = None,
    session_refs: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建默认运行时上下文字典（cwd + 版本快照 + 会话引用）。

    注意：**不含时间戳**——时间戳每次都变，会让"变化才注入"永远触发，
    失去省 token 的意义。确需时间信息由调用方自行加入 ``extra`` 并理解代价。
    """
    version_status: dict[str, str] = {}
    registry = version_registry
    if registry is None:
        try:
            from water_agent.ingestion.version_registry import get_version_registry

            registry = get_version_registry()
        except Exception:  # noqa: BLE001 — 注册表不可用时快照留空，不阻塞
            registry = None
    if registry is not None and hasattr(registry, "list_all"):
        for record in registry.list_all():
            spec_id = record.get("spec_id", "")
            version = record.get("version", "")
            if spec_id:
                # 键带版本号：同一规范多版本（现行+被取代）各自成行，避免后写覆盖先写
                key = f"{spec_id}-{version}" if version else spec_id
                version_status[key] = record.get("status", "unknown")
    context: dict[str, Any] = {
        "cwd": str(Path.cwd()),
        "version_status": version_status,
    }
    if session_refs:
        context["session_refs"] = session_refs
    if extra:
        context["extra"] = extra
    return context

"""Step 8 测试：运行时上下文投影（RuntimeContextProjection）。

覆盖：不变不注入 / 变化注入一次 / 稳定渲染顺序 / reset 后重新注入。
"""
from __future__ import annotations

from water_agent.prompting.runtime_context import (
    RuntimeContextProjection,
    default_context_builder,
)


class TestRuntimeContextProjection:
    def test_first_call_always_injects(self) -> None:
        proj = RuntimeContextProjection()
        first = proj.project({"cwd": "/tmp/a", "version_status": {}})
        assert first is not None
        assert "工作目录" in first

    def test_unchanged_returns_none(self) -> None:
        proj = RuntimeContextProjection()
        ctx = {"cwd": "/tmp/a", "version_status": {}}
        assert proj.project(ctx) is not None
        assert proj.project(ctx) is None  # 未变化 → 不注入

    def test_change_injects_once(self) -> None:
        proj = RuntimeContextProjection()
        ctx_a = {"cwd": "/tmp/a", "version_status": {}}
        ctx_b = {"cwd": "/tmp/b", "version_status": {}}
        assert proj.project(ctx_a) is not None
        assert proj.project(ctx_a) is None
        changed = proj.project(ctx_b)
        assert changed is not None
        assert "工作目录" in changed and "/tmp/b" in changed

    def test_empty_context_injects_then_stops(self) -> None:
        proj = RuntimeContextProjection()
        assert proj.project({}) is not None  # 空串也算首次注入（建立基线）
        assert proj.project({}) is None

    def test_reset_reinjects(self) -> None:
        proj = RuntimeContextProjection()
        ctx = {"cwd": "/tmp/a", "version_status": {}}
        assert proj.project(ctx) is not None
        assert proj.project(ctx) is None
        proj.reset()
        assert proj.project(ctx) is not None  # reset 后重新注入

    def test_version_status_renders_stable_order(self) -> None:
        proj = RuntimeContextProjection()
        ctx = {"cwd": "/tmp/a", "version_status": {"GB50015": "current", "GB50013": "current"}}
        first = proj.project(ctx)
        assert first is not None
        # 稳定排序：GB50013 在 GB50015 之前
        assert first.index("GB50013") < first.index("GB50015")


class TestDefaultContextBuilder:
    def test_builds_cwd_and_version_status(self) -> None:
        ctx = default_context_builder()
        assert "cwd" in ctx
        assert "version_status" in ctx
        assert ctx["cwd"]

    def test_session_refs_included_when_given(self) -> None:
        ctx = default_context_builder(session_refs=["t1", "t2"])
        assert ctx["session_refs"] == ["t1", "t2"]

    def test_extra_included(self) -> None:
        ctx = default_context_builder(extra={"project": "给排水改造"})
        assert ctx["extra"] == {"project": "给排水改造"}

    def test_multi_version_same_spec_keeps_distinct_keys(self) -> None:
        """同一规范多版本（现行+被取代）各自成行，不被后写覆盖（S8-01 回归）。"""

        class FakeRegistry:
            def list_all(self):
                return [
                    {"spec_id": "GB50015", "version": "2019", "status": "current"},
                    {"spec_id": "GB50015", "version": "2003", "status": "superseded"},
                ]

        ctx = default_context_builder(version_registry=FakeRegistry())
        assert ctx["version_status"] == {
            "GB50015-2019": "current",
            "GB50015-2003": "superseded",
        }
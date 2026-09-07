"""P3 Step 18：读/写目录白名单语义分离的正反例。

核心断言：**可读 ≠ 可写**。配了 `allowed_read_dirs` 的目录不因此获得导出权限；
配了 `allowed_export_dirs` 的目录不因此获得读取权限。两者皆空时读写都拒
（fail closed）。理由见 `docs/design/p3-step-17-decision.md` D4 ——
用户让 Agent 看图纸，不等于允许它往图纸目录里写东西。

白名单按 **capability** 选，不按参数名启发式：参数名启发式可被换个参数名绕过，
capability 由 `inventory.TOOL_CAPABILITIES` 静态登记、模型无法影响。

本文件用 `declared_capabilities` 声明测试工具的能力（`GuardrailPipeline` 为
测试夹具预留的装配期通道，不放松任何检查），因此不依赖 Step 20 才登记的
`read_drawing` 是否已进治理清单。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from water_agent.runtime.governance.contracts import (
    DANGEROUS_CAPABILITIES,
    DEFAULT_LOCAL_CAPABILITIES,
    Capability,
    Principal,
)
from water_agent.runtime.governance.pipeline import (
    DEFAULT_TOOL_POLICY,
    GuardrailPipeline,
    ToolPolicy,
)

#: 声明式测试工具：一个只读图纸工具 + 一个导出工具，各自走不同白名单。
_DECLARED = {
    "probe_read_drawing": Capability.READ_DRAWING,
    "probe_export": Capability.EXPORT_TO_ALLOWED_DIR,
}


@pytest.fixture
def principal() -> Principal:
    return Principal.local("step18")


def _pipe(
    *,
    allowed_read_dirs: frozenset[Path] = frozenset(),
    allowed_export_dirs: frozenset[Path] = frozenset(),
) -> GuardrailPipeline:
    return GuardrailPipeline(
        policy=ToolPolicy(
            allowed_read_dirs=allowed_read_dirs,
            allowed_export_dirs=allowed_export_dirs,
        ),
        declared_capabilities=_DECLARED,
    )


class TestReadDrawingCapabilityIsGrantedButNotDangerous:
    def test_read_drawing_in_default_local_capabilities(self) -> None:
        """本地 CLI 主体默认持有只读图纸能力（否则 P3 工具永远不可用）。"""
        assert Capability.READ_DRAWING in DEFAULT_LOCAL_CAPABILITIES
        assert Principal.local("t").has(Capability.READ_DRAWING)

    def test_read_drawing_is_not_dangerous(self) -> None:
        """只读不是危险能力：它不含写入/网络/shell，可在 P4 授权前使用。"""
        assert Capability.READ_DRAWING not in DANGEROUS_CAPABILITIES

    def test_read_drawing_does_not_imply_write(self) -> None:
        """只读能力**不**蕴含写入能力 —— 两者是独立的枚举值。"""
        assert Capability.READ_DRAWING != Capability.WRITE_FILE
        assert Capability.WRITE_FILE not in DEFAULT_LOCAL_CAPABILITIES


class TestDefaultPolicyDeniesBothReadAndWrite:
    def test_default_policy_has_both_allowlists_empty(self) -> None:
        assert DEFAULT_TOOL_POLICY.allowed_read_dirs == frozenset()
        assert DEFAULT_TOOL_POLICY.allowed_export_dirs == frozenset()

    @pytest.mark.parametrize("tool", sorted(_DECLARED))
    def test_absolute_path_denied_when_both_empty(
        self, principal: Principal, tmp_path: Path, tool: str
    ) -> None:
        """两个白名单都空 → 读写路径参数一律拒（fail closed）。"""
        outcome = _pipe().validate_tool_call(tool, {"path": str(tmp_path / "x.dxf")}, principal)
        assert not outcome.allowed
        assert outcome.decision.rule_id == "tool.path_not_allowed"


class TestReadWriteAllowlistsAreSeparate:
    """本类是 Step 18 的核心：两个白名单互不蕴含。"""

    def test_read_dir_grants_read(self, principal: Principal, tmp_path: Path) -> None:
        outcome = _pipe(allowed_read_dirs=frozenset({tmp_path})).validate_tool_call(
            "probe_read_drawing", {"path": str(tmp_path / "plan.dxf")}, principal
        )
        assert outcome.allowed

    def test_read_dir_does_not_grant_export(
        self, principal: Principal, tmp_path: Path
    ) -> None:
        """**反例（最小权限）**：配了读目录，写出到同一目录仍必须被拒。

        若此断言失效，说明读白名单被当成了通用路径白名单 —— 用户只想让
        Agent 看图纸，却顺带授予了往图纸目录写文件的权限。
        """
        outcome = _pipe(allowed_read_dirs=frozenset({tmp_path})).validate_tool_call(
            "probe_export", {"path": str(tmp_path / "out.xlsx")}, principal
        )
        assert not outcome.allowed
        assert outcome.decision.rule_id == "tool.path_not_allowed"

    def test_export_dir_grants_export(self, principal: Principal, tmp_path: Path) -> None:
        outcome = _pipe(allowed_export_dirs=frozenset({tmp_path})).validate_tool_call(
            "probe_export", {"path": str(tmp_path / "out.xlsx")}, principal
        )
        assert outcome.allowed

    def test_export_dir_does_not_grant_read(
        self, principal: Principal, tmp_path: Path
    ) -> None:
        """**反例（反向不蕴含）**：可写目录未必是用户想让模型读的目录。"""
        outcome = _pipe(allowed_export_dirs=frozenset({tmp_path})).validate_tool_call(
            "probe_read_drawing", {"path": str(tmp_path / "plan.dxf")}, principal
        )
        assert not outcome.allowed
        assert outcome.decision.rule_id == "tool.path_not_allowed"

    def test_both_configured_each_tool_uses_its_own_list(
        self, principal: Principal, tmp_path: Path
    ) -> None:
        """两个白名单指向**不同**目录时，各工具只能碰自己那个。"""
        read_dir = tmp_path / "drawings"
        write_dir = tmp_path / "exports"
        read_dir.mkdir()
        write_dir.mkdir()
        pipe = _pipe(
            allowed_read_dirs=frozenset({read_dir}),
            allowed_export_dirs=frozenset({write_dir}),
        )
        assert pipe.validate_tool_call(
            "probe_read_drawing", {"path": str(read_dir / "a.dxf")}, principal
        ).allowed
        assert pipe.validate_tool_call(
            "probe_export", {"path": str(write_dir / "a.xlsx")}, principal
        ).allowed
        # 交叉使用必须被拒
        assert not pipe.validate_tool_call(
            "probe_read_drawing", {"path": str(write_dir / "a.dxf")}, principal
        ).allowed
        assert not pipe.validate_tool_call(
            "probe_export", {"path": str(read_dir / "a.xlsx")}, principal
        ).allowed


class TestReadAllowlistStillBlocksTraversal:
    def test_traversal_out_of_read_dir_denied(
        self, principal: Principal, tmp_path: Path
    ) -> None:
        """读白名单同样在**解析后**比较，否则 `allowed/../../etc` 会通过。"""
        allowed = tmp_path / "drawings"
        allowed.mkdir()
        outcome = _pipe(allowed_read_dirs=frozenset({allowed})).validate_tool_call(
            "probe_read_drawing",
            {"path": str(allowed / ".." / ".." / "etc" / "passwd")},
            principal,
        )
        assert not outcome.allowed
        assert outcome.decision.rule_id == "tool.path_not_allowed"

    def test_symlink_escaping_read_dir_denied(
        self, principal: Principal, tmp_path: Path
    ) -> None:
        """符号链接逃逸：`resolve()` 会跟随链接，故链接到白名单外必须被拒。"""
        allowed = tmp_path / "drawings"
        outside = tmp_path / "secrets"
        allowed.mkdir()
        outside.mkdir()
        (outside / "real.dxf").write_text("x", encoding="utf-8")
        link = allowed / "link.dxf"
        link.symlink_to(outside / "real.dxf")
        outcome = _pipe(allowed_read_dirs=frozenset({allowed})).validate_tool_call(
            "probe_read_drawing", {"path": str(link)}, principal
        )
        assert not outcome.allowed
        assert outcome.decision.rule_id == "tool.path_not_allowed"


class TestCapabilityDrivenSelectionNotArgName:
    """白名单按 capability 选，不看参数名 —— 换参数名不能换到另一套权限。"""

    @pytest.mark.parametrize("arg_name", ("path", "dest", "output", "target", "src"))
    def test_arg_name_does_not_change_which_allowlist_applies(
        self, principal: Principal, tmp_path: Path, arg_name: str
    ) -> None:
        """只读工具的任意参数名都查读白名单：配了读目录就放行，不看叫什么。"""
        pipe = _pipe(allowed_read_dirs=frozenset({tmp_path}))
        assert pipe.validate_tool_call(
            "probe_read_drawing", {arg_name: str(tmp_path / "a.dxf")}, principal
        ).allowed

    @pytest.mark.parametrize("arg_name", ("path", "dest", "output", "target", "src"))
    def test_export_tool_never_falls_back_to_read_list(
        self, principal: Principal, tmp_path: Path, arg_name: str
    ) -> None:
        """反向：导出工具不论参数叫什么，都不会回落到读白名单。"""
        pipe = _pipe(allowed_read_dirs=frozenset({tmp_path}))
        assert not pipe.validate_tool_call(
            "probe_export", {arg_name: str(tmp_path / "a.xlsx")}, principal
        ).allowed


class TestNoFalsePositivesOnEngineeringStrings:
    """规格 §2.2：工程技术信息不得因看起来像路径而被误拦。"""

    @pytest.mark.parametrize(
        "value",
        (
            "DN200/DN150",
            "GB 50015-2019 3.1.1",
            "球墨铸铁 K9",
            "W-给水-DN200",
            "1.5 m/s",
            "i=0.005",
        ),
    )
    def test_engineering_strings_pass_read_path_check(
        self, principal: Principal, value: str
    ) -> None:
        """即使读白名单为空，这些正常字符串也不得被判为路径。"""
        outcome = _pipe().validate_tool_call(
            "probe_read_drawing", {"query": value}, principal
        )
        assert outcome.allowed, f"正常工程字符串被误拦：{value!r} → {outcome.decision.detail}"

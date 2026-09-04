"""回归测试：pyproject.toml 运行时依赖与 uv.lock 一致（防 lock 漂移）。

背景：2026-09-04 发现 openpyxl>=3.1,<4 已入 pyproject 但 uv.lock 未同步
（water-agent 条目缺 openpyxl，openpyxl/et-xmlfile 不在 lock 中），
uv sync 后材料 .xlsx 导出环境不完整。本测试守卫「声明即锁定」，
发现漂移时报错并提示 uv lock。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_lock() -> dict:
    with (ROOT / "uv.lock").open("rb") as handle:
        return tomllib.load(handle)


def _load_pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _canonical(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    if match is None:
        raise ValueError(f"无法解析依赖声明：{requirement!r}")
    return _canonical(match.group(1))


def test_all_runtime_dependencies_present_in_lock() -> None:
    """pyproject [project.dependencies] 每一项都必须有对应 [[package]] 锁定。"""
    pyproject = _load_pyproject()
    lock = _load_lock()
    locked_names = {_canonical(pkg["name"]) for pkg in lock["package"]}
    declared = pyproject["project"]["dependencies"]
    missing = [dep for dep in declared if _requirement_name(dep) not in locked_names]
    assert not missing, (
        f"pyproject 声明了但 uv.lock 缺失（运行 uv lock 同步）：{missing}"
    )


def test_water_agent_requires_dist_matches_pyproject_dependencies() -> None:
    """lock 中 water-agent 的 requires-dist 必须逐项反映 pyproject 运行时依赖。"""
    pyproject = _load_pyproject()
    lock = _load_lock()
    water_agent = next(pkg for pkg in lock["package"] if _canonical(pkg["name"]) == "water-agent")
    requires_dist = water_agent.get("metadata", {}).get("requires-dist") or []
    locked_specs = {
        _canonical(entry["name"]): entry.get("specifier", "")
        for entry in requires_dist
    }
    for dep in pyproject["project"]["dependencies"]:
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)", dep)
        assert match is not None, dep
        name, specifier = _canonical(match.group(1)), match.group(2)
        assert name in locked_specs, f"water-agent requires-dist 缺 {name}"
        assert locked_specs[name] == specifier, (
            f"{name} 版本策略漂移：pyproject={specifier!r} lock={locked_specs[name]!r}"
        )


def test_openpyxl_locked_with_et_xmlfile_dependency() -> None:
    """材料 .xlsx 导出依赖：openpyxl 锁定且其 et-xmlfile 传递依赖也在 lock 中。"""
    lock = _load_lock()
    packages = {_canonical(pkg["name"]): pkg for pkg in lock["package"]}
    assert "openpyxl" in packages, "openpyxl 不在 uv.lock（P2 材料 Excel 导出会 ImportError）"
    assert "et-xmlfile" in packages, "et-xmlfile 不在 uv.lock（openpyxl 传递依赖未锁定）"
    openpyxl_deps = {_canonical(dep["name"]) for dep in packages["openpyxl"].get("dependencies", [])}
    assert "et-xmlfile" in openpyxl_deps, "openpyxl 条目未声明 et-xmlfile 依赖"

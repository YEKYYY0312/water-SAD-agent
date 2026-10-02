"""P7-S0 H2：规格 ↔ 测试追溯（``docs/design/16-p7-plugin-mcp.md`` §6.3）。

第一条测试检查真实规格；其余用外来前缀 ``X9`` 的夹具规格验证检查器本身的鉴别力
（外来前缀不会触发对本仓库 ``tests/`` 的反向扫描）。
"""
from __future__ import annotations

from pathlib import Path

from water_agent.harness.spec_trace import check_traceability, parse_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "docs" / "design" / "16-p7-plugin-mcp.md"


def test_spec_traceability_holds() -> None:
    """P7-S0-A18 本规格的追溯关系成立：delivered 行的测试存在、未 skip、docstring 写明 ID；
    ``tests/`` 里出现的 P7 ID 都在规格里有定义。"""

    problems = check_traceability(SPEC, REPO_ROOT)
    assert problems == [], "\n".join(problems)
    rows = parse_spec(SPEC.read_text(encoding="utf-8"))
    assert sum(row.kind == "invariant" for row in rows) == 7
    assert any(row.status == "delivered" for row in rows)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

_TESTS = '''
import pytest


def test_good():
    """X9-S1-A01 有声明。"""


def test_no_id():
    """这里没写 ID。"""


@pytest.mark.skip(reason="later")
def test_skipped():
    """X9-S1-A03 被跳过。"""


def test_skips_inside():
    """X9-S1-A04 函数体里跳过。"""
    pytest.skip("no env")


@pytest.mark.parametrize("word", ["skip", "xfail"])
def test_parametrized(word):
    """X9-S1-A05 参数里出现 skip 字样不算跳过。"""


class TestGroup:
    def test_inner(self):
        """X9-S1-A06 类内节点。"""
'''


def _spec(rows: list[str], uncovered: str = "") -> str:
    header = "| ID | 验收 | 验证 | 状态 |\n|---|---|---|---|\n"
    return (
        "# 夹具规格\n\n## 7. 验收\n\n" + header + "\n".join(rows)
        + "\n\n### 未覆盖项（显式声明）\n\n" + uncovered + "\n\n## 8. 其他\n"
    )


def _layout(tmp_path: Path, spec_text: str, tests_text: str = _TESTS) -> tuple[Path, Path]:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fixture.py").write_text(tests_text, encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ci_gate.sh").write_text("run_suite fixture_p0 x.jsonl\n", encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text(spec_text, encoding="utf-8")
    return spec, tmp_path


def _check(tmp_path: Path, rows: list[str], uncovered: str = "",
           tests_text: str = _TESTS) -> list[str]:
    spec, root = _layout(tmp_path, _spec(rows, uncovered), tests_text)
    return check_traceability(spec, root, prefix="X9")


def test_delivered_rows_must_point_at_live_tests(tmp_path: Path) -> None:
    """P7-S0-A19 delivered 行：测试缺失、docstring 未声明 ID、被 skip、函数体内 skip、
    gate 项不存在都会被报告；类内节点、参数里出现 skip 字样不误报。"""

    rows = [
        "| X9-S1-A01 | 正常 | `tests/test_fixture.py::test_good` | delivered |",
        "| X9-S1-A02 | 没声明 | `tests/test_fixture.py::test_no_id` | delivered |",
        "| X9-S1-A03 | 跳过 | `tests/test_fixture.py::test_skipped` | delivered |",
        "| X9-S1-A04 | 体内跳过 | `tests/test_fixture.py::test_skips_inside` | delivered |",
        "| X9-S1-A05 | 参数 | `tests/test_fixture.py::test_parametrized` | delivered |",
        "| X9-S1-A06 | 类内 | `tests/test_fixture.py::TestGroup::test_inner` | delivered |",
        "| X9-S1-A07 | 缺函数 | `tests/test_fixture.py::test_missing` | delivered |",
        "| X9-S1-A08 | 缺文件 | `tests/test_gone.py::test_x` | delivered |",
        "| X9-S1-A09 | 门禁 | `gate:fixture_p0` | delivered |",
        "| X9-S1-A10 | 门禁缺失 | `gate:nope_p0` | delivered |",
    ]
    problems = _check(tmp_path, rows)
    joined = "\n".join(problems)
    for flagged in ("A02", "A03", "A04", "A07", "A08", "A10"):
        assert f"X9-S1-{flagged}" in joined, (flagged, joined)
    for clean in ("A01", "A05", "A06", "A09"):
        assert f"X9-S1-{clean}" not in joined, (clean, joined)


def test_unknown_ids_in_tests_are_reported(tmp_path: Path) -> None:
    """P7-S0-A20 测试里引用了规格没有定义的 ID → 报告（反向追溯）。"""

    rows = ["| X9-S1-A01 | 正常 | `tests/test_fixture.py::test_good` | delivered |"]
    tests_text = _TESTS.replace("X9-S1-A06", "X9-S9-A99")
    problems = _check(tmp_path, rows, tests_text=tests_text)
    assert any("X9-S9-A99" in line and "未定义" in line for line in problems), problems


def test_malformed_spec_tables_are_reported(tmp_path: Path) -> None:
    """P7-S0-A21 重复 ID、未知状态、delivered 没有验证项、untested 未在未覆盖项说明 → 报告；
    planned 行只查格式，验证项可以尚不存在。"""

    rows = [
        "| X9-S1-A01 | 正常 | `tests/test_fixture.py::test_good` | delivered |",
        "| X9-S1-A01 | 重复 | `tests/test_fixture.py::test_good` | delivered |",
        "| X9-S1-A02 | 状态错 | `tests/test_fixture.py::test_good` | done |",
        "| X9-S1-A03 | 空验证 | 无 | delivered |",
        "| X9-S1-A04 | 没说明 | 无 | untested |",
        "| X9-S1-A05 | 有说明 | 无 | untested |",
        "| X9-S1-A06 | 计划中 | `tests/test_future.py::test_later` | planned |",
        "| X9-I01 | 不变量 | 某处 | planned |",
    ]
    problems = _check(tmp_path, rows, uncovered="- **X9-S1-A05**：需要真实环境。")
    joined = "\n".join(problems)
    assert "X9-S1-A01 重复定义" in joined
    assert "X9-S1-A02 的状态 'done' 未知" in joined
    assert "X9-S1-A03 状态为 delivered，却没有任何验证项" in joined
    assert "X9-S1-A04 标为 untested" in joined
    assert "X9-S1-A05" not in joined
    assert "X9-S1-A06" not in joined
    assert "X9-I01" not in joined

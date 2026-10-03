"""规格 ↔ 测试追溯检查（P7-S0 H2，``docs/design/16-p7-plugin-mcp.md`` §6.3）。

规格里的验收表与不变量表是机器可读的：

- 不变量行：``| P7-I01 | 不变量 | 强制位置 | 状态 |``；
- 验收行：``| P7-S0-A01 | 验收 | 验证项 | 状态 |``，验证项用反引号列出，可以是 pytest 节点
  ``path::[Class::]name``，也可以是 ``gate:<名>``（``scripts/ci_gate.sh`` 里的套件）。

状态只有三种：``delivered``（验证项必须真实存在、没被跳过，pytest 节点的 docstring 必须写上
该 ID）、``planned``（只查格式）、``untested``（必须在"未覆盖项"一节写明原因）。反过来，
``tests/`` 里出现的任何 ID 都必须在规格里有定义。

这里只用 AST 读测试源码，不 import、不收集——检查本身不能依赖被检查的代码能跑起来。
它证明的是"测试存在且声明了覆盖"，**不证明断言写得对**。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

STATUSES = frozenset({"delivered", "planned", "untested"})
UNCOVERED_HEADING = "未覆盖项"
_BACKTICKED = re.compile(r"`([^`]+)`")


@dataclass(frozen=True, slots=True)
class SpecRow:
    id: str
    kind: Literal["acceptance", "invariant"]
    text: str
    checks: tuple[str, ...]
    status: str
    line_no: int


def _patterns(prefix: str) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    escaped = re.escape(prefix)
    acceptance = re.compile(rf"^{escaped}-S\d+-A\d{{2}}$")
    invariant = re.compile(rf"^{escaped}-I\d{{2}}$")
    mention = re.compile(rf"(?<![\w-]){escaped}-(?:S\d+-A\d{{2}}|I\d{{2}})(?![\w-])")
    return acceptance, invariant, mention


def parse_spec(text: str, *, prefix: str = "P7") -> list[SpecRow]:
    """取出规格里首列是 ID 的表格行。"""

    acceptance, invariant, _ = _patterns(prefix)
    rows: list[SpecRow] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        if acceptance.match(first):
            kind: Literal["acceptance", "invariant"] = "acceptance"
        elif invariant.match(first):
            kind = "invariant"
        else:
            continue
        checks = tuple(_BACKTICKED.findall(cells[2])) if kind == "acceptance" and len(cells) > 3 else ()
        rows.append(
            SpecRow(
                id=first,
                kind=kind,
                text=cells[1] if len(cells) > 1 else "",
                checks=checks,
                status=cells[-1] if len(cells) > 1 else "",
                line_no=line_no,
            )
        )
    return rows


def _uncovered_section(text: str) -> str:
    """「未覆盖项」标题之后、下一个同级或更高级标题之前的内容。"""

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#") and UNCOVERED_HEADING in line:
            level = len(line) - len(line.lstrip("#"))
            body: list[str] = []
            for follow in lines[index + 1:]:
                stripped = follow.lstrip()
                if stripped.startswith("#") and len(stripped) - len(stripped.lstrip("#")) <= level:
                    break
                body.append(follow)
            return "\n".join(body)
    return ""


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_skip_marker(node: ast.AST) -> bool:
    """``pytest.mark.skip`` / ``skipif`` / ``xfail``、``unittest.skip`` 这类标记（含列表里的）。

    只看表达式本身的名字，不做子串匹配：``parametrize`` 的参数里出现 "skip" 字样不算。
    """

    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_is_skip_marker(element) for element in node.elts)
    name = _dotted_name(node)
    return bool(name) and name.rsplit(".", 1)[-1] in {"skip", "skipif", "xfail"}


def _check_node(row_id: str, check: str, repo_root: Path) -> list[str]:
    file_part, _, qual = check.partition("::")
    path = repo_root / file_part
    if not qual or not path.is_file():
        return [f"{row_id}：验证项 {check} 指向的文件不存在"]
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    problems: list[str] = []
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ) and _is_skip_marker(statement.value):
            problems.append(f"{row_id}：{file_part} 的 pytestmark 把整个模块标成了 skip/xfail")
    scope: list[ast.stmt] = module.body
    found: ast.AST | None = None
    for name in qual.split("::"):
        found = next(
            (
                node
                for node in scope
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == name
            ),
            None,
        )
        if found is None:
            return [f"{row_id}：验证项 {check} 不存在"]
        if any(_is_skip_marker(decorator) for decorator in found.decorator_list):
            problems.append(f"{row_id}：验证项 {check} 被 skip/xfail")
        scope = found.body if isinstance(found, ast.ClassDef) else []
    assert found is not None
    if not isinstance(found, (ast.FunctionDef, ast.AsyncFunctionDef)):
        problems.append(f"{row_id}：验证项 {check} 不是测试函数")
        return problems
    if row_id not in (ast.get_docstring(found) or ""):
        problems.append(f"{row_id}：验证项 {check} 的 docstring 没有写明 {row_id}")
    # 函数体里的 pytest.skip(...)：哪怕是条件跳过，也会让这条验收在某些环境里悄悄不被验证
    for node in ast.walk(found):
        if isinstance(node, ast.Call) and _dotted_name(node) in {
            "pytest.skip", "pytest.xfail", "pytest.importorskip"
        }:
            problems.append(f"{row_id}：验证项 {check} 在函数体里调用了 {_dotted_name(node)}")
            break
    return problems


def check_traceability(
    spec_path: Path,
    repo_root: Path,
    *,
    prefix: str = "P7",
    tests_dir: Path | None = None,
    gate_script: Path | None = None,
) -> list[str]:
    """返回全部追溯问题；空列表表示规格与测试一致。"""

    text = spec_path.read_text(encoding="utf-8")
    rows = parse_spec(text, prefix=prefix)
    tests_root = tests_dir if tests_dir is not None else repo_root / "tests"
    gate_path = gate_script if gate_script is not None else repo_root / "scripts" / "ci_gate.sh"
    gate_text = gate_path.read_text(encoding="utf-8") if gate_path.is_file() else ""
    uncovered = _uncovered_section(text)

    problems: list[str] = []
    first_seen: dict[str, int] = {}
    for row in rows:
        if row.id in first_seen:
            problems.append(f"{row.id} 重复定义（第 {first_seen[row.id]} 行与第 {row.line_no} 行）")
        first_seen.setdefault(row.id, row.line_no)
        if row.status not in STATUSES:
            problems.append(
                f"{row.id} 的状态 {row.status!r} 未知（只允许 {', '.join(sorted(STATUSES))}）"
            )

    for row in rows:
        if row.kind != "acceptance":
            continue
        if row.status == "untested" and row.id not in uncovered:
            problems.append(f"{row.id} 标为 untested，但没有在「{UNCOVERED_HEADING}」一节说明原因")
        if row.status != "delivered":
            continue
        if not row.checks:
            problems.append(f"{row.id} 状态为 delivered，却没有任何验证项")
        for check in row.checks:
            if check.startswith("gate:"):
                if not check[len("gate:"):] or check[len("gate:"):] not in gate_text:
                    problems.append(f"{row.id}：验证项 {check} 不在 scripts/ci_gate.sh 里")
            elif "::" in check:
                problems.extend(_check_node(row.id, check, repo_root))
            else:
                problems.append(f"{row.id}：无法识别的验证项 {check!r}（应为 path::name 或 gate:名）")

    _, _, mention = _patterns(prefix)
    if tests_root.is_dir():
        for path in sorted(tests_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            unknown = sorted(
                {match.group(0) for match in mention.finditer(path.read_text(encoding="utf-8"))}
                - set(first_seen)
            )
            for identifier in unknown:
                problems.append(f"{path.relative_to(repo_root).as_posix()} 引用了规格未定义的 {identifier}")
    return problems


__all__ = ["STATUSES", "SpecRow", "check_traceability", "parse_spec"]

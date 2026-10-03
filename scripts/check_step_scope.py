"""改动范围守卫（P7-S0 H3，``docs/design/16-p7-plugin-mcp.md`` §6.4）。

每一步的执行 Prompt 里有一个 ``<!-- step-scope -->`` 标记的 JSON 块，声明这一步允许改哪些
文件、禁止改哪些文件、哪些断言不许动、哪些 JSON 片段必须与 base 完全相同。本脚本把当前
分支相对 base 的改动与之核对。

关键约定
--------
- **范围块从 merge-base 上读**：分支改不了自己的范围。工作区里的范围块与 merge-base
  版本不一致直接判失败。首次引入该 Prompt 的步骤用 ``--bootstrap`` 从工作区读。
- **改动集合** = ``git diff --no-renames --name-only -z <merge-base>``（已提交 + 暂存 +
  未暂存，含删除；不识别重命名，移走的文件按旧路径计入）∪ 未跟踪文件。``-z`` 防止中文
  路径被转义。
- **glob**：``**`` 匹配 0 到多段目录，``*`` / ``?`` 不跨 ``/``，整串匹配（``fnmatch`` 的
  ``*`` 会跨 ``/``，不能用）。路径必须命中 allow 且不命中 forbid，forbid 优先。
- **protect**：``{"node": "tests/x.py::Class::test_y", "contains": "assert cost < 4500"}``。
  测试节点必须存在、不能被 skip/xfail；去掉注释后按 token 比较，仍须包含该字面量。
- **frozen_json**：``{"path": "...json", "pointers": ["/profiles/*/surface"]}``。命中的
  JSON 片段必须与 merge-base 版本完全相同（pointer 段内支持 ``*`` 通配）。

``--auto`` 供 CI 用：分支名形如 ``YEK/p7-s<n>-...`` 时，在 merge-base 上找
``docs/execution-prompts/P7-S<n>-*.md``；S0 没有执行 Prompt（范围块在任务书里，用
``--bootstrap`` 手工跑），直接跳过；n≥1 却找不到 Prompt 判失败——该步的执行 Prompt
必须先合入 base。

退出码：0 通过 / 不适用；1 有越界或违规；2 无法判定（base 不存在、浅克隆、范围块缺失或坏）。

只用标准库：CI 上不需要装项目依赖就能跑。
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCOPE_MARKER = "<!-- step-scope -->"
_SCOPE_KEYS = {"step", "allow", "forbid", "protect", "frozen_json"}
_AUTO_BRANCH = re.compile(r"^(?:refs/heads/)?YEK/p7-s(\d+)-")
_SKIP_WORDS = ("skip", "xfail", "importorskip")


class ScopeError(Exception):
    """无法判定（exit 2），不是"有越界"。"""


class ScopeViolation(Exception):
    """在确定范围之前就能判定的违规（exit 1）：分支改了自己的范围块、该步执行 Prompt 未先合入 base。"""


@dataclass
class Scope:
    step: str
    allow: list[str]
    forbid: list[str] = field(default_factory=list)
    protect: list[dict[str, str]] = field(default_factory=list)
    frozen_json: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def extract_scope_block(text: str) -> str:
    """取标记之后的第一个 fenced code block 的内容。"""

    index = text.find(SCOPE_MARKER)
    if index < 0:
        raise ScopeError(f"没有找到 {SCOPE_MARKER} 标记")
    rest = text[index + len(SCOPE_MARKER):]
    match = re.search(r"^```[^\n]*\n(.*?)^```", rest, flags=re.MULTILINE | re.DOTALL)
    if match is None:
        raise ScopeError(f"{SCOPE_MARKER} 之后没有 fenced code block")
    return match.group(1)


def parse_scope(text: str) -> Scope:
    raw = extract_scope_block(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScopeError(f"范围块不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ScopeError("范围块必须是 JSON object")
    unknown = set(data) - _SCOPE_KEYS
    if unknown:
        raise ScopeError(f"范围块有未知字段：{sorted(unknown)}")
    step = data.get("step")
    allow = data.get("allow")
    if not isinstance(step, str) or not step:
        raise ScopeError("范围块缺少 step")
    if not isinstance(allow, list) or not allow or not all(isinstance(g, str) for g in allow):
        raise ScopeError("allow 必须是非空字符串列表")
    forbid = data.get("forbid", [])
    if not isinstance(forbid, list) or not all(isinstance(g, str) for g in forbid):
        raise ScopeError("forbid 必须是字符串列表")
    protect = data.get("protect", [])
    if not isinstance(protect, list) or not all(
        isinstance(item, dict)
        and set(item) == {"node", "contains"}
        and all(isinstance(v, str) and v for v in item.values())
        for item in protect
    ):
        raise ScopeError('protect 每项必须是 {"node": ..., "contains": ...}')
    frozen = data.get("frozen_json", [])
    if not isinstance(frozen, list) or not all(
        isinstance(item, dict)
        and set(item) == {"path", "pointers"}
        and isinstance(item["path"], str)
        and isinstance(item["pointers"], list)
        and item["pointers"]
        and all(isinstance(p, str) and p.startswith("/") for p in item["pointers"])
        for item in frozen
    ):
        raise ScopeError('frozen_json 每项必须是 {"path": ..., "pointers": ["/..."]}')
    return Scope(step=step, allow=allow, forbid=forbid, protect=protect, frozen_json=frozen)


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


def _segment_regex(segment: str) -> str:
    out: list[str] = []
    for char in segment:
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
    return "".join(out)


def compile_glob(pattern: str) -> re.Pattern[str]:
    segments = pattern.strip("/").split("/")
    regex = ""
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            regex += ".+" if last else "(?:[^/]+/)*"
        else:
            regex += _segment_regex(segment) + ("" if last else "/")
    return re.compile(rf"\A{regex}\Z")


def glob_match(path: str, pattern: str) -> bool:
    return compile_glob(pattern).match(path) is not None


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if check and result.returncode != 0:
        raise ScopeError(
            f"git {' '.join(args)} 失败：{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result


def _git_text(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.decode("utf-8").strip()


def resolve_merge_base(repo: Path, base: str) -> str:
    if _git_text(repo, "rev-parse", "--is-shallow-repository") == "true":
        raise ScopeError("浅克隆无法可靠计算 merge-base；CI 上用 fetch-depth: 0，本机先 git fetch --unshallow")
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).returncode:
        raise ScopeError(f"base {base!r} 不存在；先 git fetch origin main")
    return _git_text(repo, "merge-base", base, "HEAD")


def changed_paths(repo: Path, merge_base: str) -> list[str]:
    tracked = _git(repo, "diff", "--no-renames", "--name-only", "-z", merge_base).stdout
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout
    paths = {
        item.decode("utf-8") for item in (tracked + untracked).split(b"\0") if item
    }
    return sorted(paths)


def show_at(repo: Path, revision: str, path: str) -> str | None:
    result = _git(repo, "show", f"{revision}:{path}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8")


# ---------------------------------------------------------------------------
# protect
# ---------------------------------------------------------------------------


def _tokens(source: str) -> list[str]:
    """去掉注释与排版 token 后的 token 串；无法完整分词时退回空白归一。"""

    skip = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
            tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER}
    try:
        return [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in skip
        ]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source.split()


def _contains_tokens(haystack: list[str], needle: list[str]) -> bool:
    if not needle:
        return False
    width = len(needle)
    return any(haystack[i:i + width] == needle for i in range(len(haystack) - width + 1))


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


def check_protected(repo: Path, item: dict[str, str]) -> str | None:
    """返回违规说明；``None`` 表示受保护断言完好。"""

    file_part, _, qual = item["node"].partition("::")
    path = repo / file_part
    if not qual or not path.is_file():
        return f"受保护的测试节点不存在：{item['node']}"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in statement.targets
        ) and _is_skip_marker(statement.value):
            return f"{file_part} 的 pytestmark 把整个模块标成了 skip/xfail"
    scope: list[ast.stmt] = module.body
    target: ast.AST | None = None
    parts = qual.split("::")
    for depth, name in enumerate(parts):
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
            return f"受保护的测试节点不存在：{item['node']}"
        if any(_is_skip_marker(decorator) for decorator in found.decorator_list):
            return f"受保护的测试节点被 skip/xfail：{item['node']}"
        if depth < len(parts) - 1:
            if not isinstance(found, ast.ClassDef):
                return f"受保护的测试节点不存在：{item['node']}"
            scope = found.body
        target = found
    assert target is not None
    body_tokens = _tokens(ast.get_source_segment(source, target) or "")
    for index in range(len(body_tokens) - 2):
        if body_tokens[index] == "pytest" and body_tokens[index + 1] == "." and (
            body_tokens[index + 2] in _SKIP_WORDS
        ):
            return f"受保护的测试节点在函数体里调用了 pytest.{body_tokens[index + 2]}：{item['node']}"
    if not _contains_tokens(body_tokens, _tokens(item["contains"])):
        return f"受保护的断言被改动或删除：{item['node']} 不再包含 {item['contains']!r}"
    return None


# ---------------------------------------------------------------------------
# frozen_json
# ---------------------------------------------------------------------------


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _expand(document: Any, pointer: str) -> dict[str, Any]:
    """把带 ``*`` 的 pointer 展开成 {具体 pointer: 值}。"""

    results: dict[str, Any] = {"": document}
    for raw in pointer.strip("/").split("/"):
        token = _unescape(raw)
        next_results: dict[str, Any] = {}
        for prefix, value in results.items():
            if isinstance(value, dict):
                keys = [key for key in value if fnmatch.fnmatchcase(str(key), token)]
            elif isinstance(value, list):
                keys = [str(i) for i in range(len(value)) if fnmatch.fnmatchcase(str(i), token)]
            else:
                keys = []
            for key in keys:
                child = value[key] if isinstance(value, dict) else value[int(key)]
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                next_results[f"{prefix}/{escaped}"] = child
        results = next_results
    return results


def check_frozen(repo: Path, merge_base: str, item: dict[str, Any]) -> list[str]:
    path = item["path"]
    base_text = show_at(repo, merge_base, path)
    head_file = repo / path
    if base_text is None:
        return [f"frozen_json 文件在 merge-base 上不存在：{path}"]
    if not head_file.is_file():
        return [f"frozen_json 文件被删除：{path}"]
    try:
        base_doc = json.loads(base_text)
        head_doc = json.loads(head_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"frozen_json 文件不是合法 JSON：{path}（{exc}）"]
    violations: list[str] = []
    for pointer in item["pointers"]:
        base_values = _expand(base_doc, pointer)
        head_values = _expand(head_doc, pointer)
        if not base_values:
            violations.append(f"{path}{pointer} 在 merge-base 上没有匹配任何片段（pointer 写错了？）")
        for concrete in sorted(set(base_values) | set(head_values)):
            if base_values.get(concrete, ...) != head_values.get(concrete, ...):
                violations.append(f"{path}{concrete} 必须与 merge-base 相同，但被改动了")
    return violations


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


@dataclass
class Report:
    step: str
    base: str
    merge_base: str
    commits: int
    changed: list[str]
    violations: list[str]


def load_scope(repo: Path, prompt: str, merge_base: str, *, bootstrap: bool) -> Scope:
    head_file = repo / prompt
    base_text = show_at(repo, merge_base, prompt)
    if bootstrap:
        if base_text is not None:
            raise ScopeError(f"{prompt} 已在 merge-base 上，不得用 --bootstrap（范围块从 base 读）")
        if not head_file.is_file():
            raise ScopeError(f"找不到 {prompt}")
        return parse_scope(head_file.read_text(encoding="utf-8"))
    if base_text is None:
        raise ScopeError(
            f"{prompt} 不在 merge-base 上：该步的执行 Prompt 必须先合入 base（首次引入用 --bootstrap）"
        )
    scope = parse_scope(base_text)
    if not head_file.is_file():
        raise ScopeError(f"{prompt} 在工作区里被删除了")
    head_block = extract_scope_block(head_file.read_text(encoding="utf-8"))
    if head_block != extract_scope_block(base_text):
        raise ScopeViolation(f"{prompt} 的范围块在本分支被改动；范围只能经 base 修改")
    return scope


def evaluate(repo: Path, prompt: str, *, base: str, bootstrap: bool = False) -> Report:
    merge_base = resolve_merge_base(repo, base)
    try:
        scope = load_scope(repo, prompt, merge_base, bootstrap=bootstrap)
    except ScopeViolation as exc:
        return Report("?", base, merge_base, 0, [], [str(exc)])
    changed = changed_paths(repo, merge_base)
    violations: list[str] = []
    for path in changed:
        if any(glob_match(path, pattern) for pattern in scope.forbid):
            violations.append(f"改动了禁止改的文件：{path}")
        elif not any(glob_match(path, pattern) for pattern in scope.allow):
            violations.append(f"改动超出允许范围：{path}")
    for item in scope.protect:
        problem = check_protected(repo, item)
        if problem is not None:
            violations.append(problem)
    for item in scope.frozen_json:
        violations.extend(check_frozen(repo, merge_base, item))
    commits = int(_git_text(repo, "rev-list", "--count", f"{merge_base}..HEAD") or 0)
    return Report(scope.step, base, merge_base, commits, changed, violations)


def auto_prompt(repo: Path, base: str, branch: str) -> str | None:
    """CI 用：按分支名找该步的执行 Prompt。返回 None 表示本分支不适用。"""

    match = _AUTO_BRANCH.match(branch)
    if match is None:
        return None
    step = int(match.group(1))
    if step == 0:
        return None
    merge_base = resolve_merge_base(repo, base)
    listing = _git(repo, "ls-tree", "--name-only", "-z", merge_base, "docs/execution-prompts/")
    names = [item.decode("utf-8") for item in listing.stdout.split(b"\0") if item]
    candidates = [
        name for name in names if fnmatch.fnmatchcase(Path(name).name, f"P7-S{step}-*.md")
    ]
    if len(candidates) != 1:
        raise ScopeViolation(
            f"分支 {branch} 对应 P7-S{step}，但 merge-base 上找到 {len(candidates)} 份执行 Prompt"
            f"（{candidates}）：该步的执行 Prompt 必须先合入 base，且只能有一份"
        )
    return candidates[0]


def _current_branch(repo: Path) -> str:
    return os.environ.get("GITHUB_HEAD_REF") or _git_text(repo, "rev-parse", "--abbrev-ref", "HEAD")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P7 改动范围守卫")
    parser.add_argument("prompt", nargs="?", help="含范围块的文档（仓库相对路径）")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--bootstrap", action="store_true", help="首次引入该文档的步骤：从工作区读范围块")
    parser.add_argument("--auto", action="store_true", help="CI：按分支名推断该步的执行 Prompt")
    parser.add_argument("--repo", default=None, help="仓库根目录（默认取当前目录所在仓库）")
    args = parser.parse_args(argv)

    try:
        repo = Path(args.repo) if args.repo else Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
        prompt = args.prompt
        if args.auto:
            branch = _current_branch(repo)
            prompt = auto_prompt(repo, args.base, branch)
            if prompt is None:
                print(f"分支 {branch} 不适用 P7 范围守卫（不是 YEK/p7-s<n>- 分支，或是 S0），跳过")
                return 0
        if not prompt:
            parser.error("需要给出 prompt 路径，或使用 --auto")
        report = evaluate(repo, prompt, base=args.base, bootstrap=args.bootstrap)
    except ScopeViolation as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 1
    except (ScopeError, subprocess.CalledProcessError, OSError) as exc:
        print(f"!! 无法判定：{exc}", file=sys.stderr)
        return 2

    print(
        f"step={report.step} base={report.base} merge-base={report.merge_base[:12]} "
        f"commits={report.commits} changed_files={len(report.changed)}"
    )
    for path in report.changed:
        print(f"  {path}")
    if report.violations:
        for line in report.violations:
            print(f"!! {line}", file=sys.stderr)
        print(f"!! 范围守卫未通过：{len(report.violations)} 处违规", file=sys.stderr)
        return 1
    print("范围守卫通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

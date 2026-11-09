"""P7-S0 H3：改动范围守卫 ``scripts/check_step_scope.py``（``docs/design/16-p7-plugin-mcp.md`` §6.4）。

全部在临时 git 仓库里跑；全局 git 配置隔离（``GIT_CONFIG_GLOBAL=/dev/null``、关闭签名），
本机的 hooks / 签名 / 默认分支设置影响不到结果。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_step_scope", REPO_ROOT / "scripts" / "check_step_scope.py"
)
assert _SPEC is not None and _SPEC.loader is not None
scope_guard = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("check_step_scope", scope_guard)
_SPEC.loader.exec_module(scope_guard)

_PROTECTED_TEST = '''
import pytest


def test_budget() -> None:
    cost = 10
    assert cost < 4500, "budget"


class TestNested:
    def test_inner(self) -> None:
        assert 1 + 1 == 2
'''


def _scope_doc(scope: dict[str, Any]) -> str:
    return (
        "# 执行 Prompt（夹具）\n\n正文。\n\n<!-- step-scope -->\n```json\n"
        + json.dumps(scope, ensure_ascii=False, indent=2)
        + "\n```\n\n尾注。\n"
    )


_DEFAULT_SCOPE: dict[str, Any] = {
    "step": "X9-S1",
    "allow": ["src/**", "docs/prompt.md", "data/snap.json"],
    "forbid": ["src/tools/**"],
    "protect": [{"node": "tests/test_budget.py::test_budget", "contains": "assert cost < 4500"}],
    "frozen_json": [{"path": "data/snap.json", "pointers": ["/profiles/*/surface"]}],
}


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(key, raising=False)


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _write(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, "src/app.py", "VALUE = 1\n")
    _write(root, "src/tools/x.py", "TOOL = 1\n")
    _write(root, "tests/test_budget.py", _PROTECTED_TEST)
    _write(root, "docs/prompt.md", _scope_doc(_DEFAULT_SCOPE))
    _write(root, "docs/中文说明.md", "原文\n")
    snap = {"profiles": {"a": {"surface": {"n": 1}, "lifecycle": {"m": 1}},
                         "b": {"surface": {"n": 2}, "lifecycle": {"m": 2}}}}
    _write(root, "data/snap.json", json.dumps(snap))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    return root


def _run(repo: Path, *extra: str, prompt: str = "docs/prompt.md") -> tuple[int, Any]:
    args = [prompt, "--base", "main", "--repo", str(repo), *extra] if prompt else [
        "--base", "main", "--repo", str(repo), *extra
    ]
    return scope_guard.main(args), None


def _report(repo: Path, **kwargs: Any) -> Any:
    return scope_guard.evaluate(repo, "docs/prompt.md", base="main", **kwargs)


# ---------------------------------------------------------------------------
# glob 与解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/**", "src/a.py", True),
        ("src/**", "src/pkg/deep/a.py", True),
        ("src/**", "srcx/a.py", False),
        ("tests/test_*.py", "tests/test_a.py", True),
        ("tests/test_*.py", "tests/sub/test_a.py", False),
        ("**/test_*.py", "test_a.py", True),
        ("**/test_*.py", "tests/sub/test_a.py", True),
        ("docs/**/decision.md", "docs/decision.md", True),
        ("docs/**/decision.md", "docs/a/b/decision.md", True),
        ("data/eval/*_main.json", "data/eval/x/y_main.json", False),
        ("a?.md", "ab.md", True),
        ("a?.md", "a/.md", False),
        ("docs/中文*.md", "docs/中文说明.md", True),
    ],
)
def test_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    """P7-S0-A22 glob：``**`` 匹配 0 到多段目录，``*``/``?`` 不跨 ``/``，整串匹配。"""

    assert scope_guard.glob_match(path, pattern) is expected


def test_parse_scope_rejects_malformed_blocks() -> None:
    """P7-S0-A22 范围块缺标记、不是 JSON、有未知字段、allow 为空 → 无法判定（ScopeError）。"""

    with pytest.raises(scope_guard.ScopeError):
        scope_guard.parse_scope("没有标记\n```json\n{}\n```\n")
    with pytest.raises(scope_guard.ScopeError):
        scope_guard.parse_scope("<!-- step-scope -->\n```json\n{not json}\n```\n")
    with pytest.raises(scope_guard.ScopeError):
        scope_guard.parse_scope(_scope_doc({"step": "X9", "allow": ["a"], "typo": []}))
    with pytest.raises(scope_guard.ScopeError):
        scope_guard.parse_scope(_scope_doc({"step": "X9", "allow": []}))
    parsed = scope_guard.parse_scope(_scope_doc(_DEFAULT_SCOPE))
    assert parsed.step == "X9-S1" and parsed.forbid == ["src/tools/**"]


# ---------------------------------------------------------------------------
# 改动集合
# ---------------------------------------------------------------------------


def test_clean_branch_passes(repo: Path) -> None:
    """P7-S0-A23 没有任何改动时通过，改动文件数为 0。"""

    report = _report(repo)
    assert report.changed == [] and report.violations == []
    assert _run(repo)[0] == 0


def test_change_outside_allow_fails(repo: Path) -> None:
    """P7-S0-A23 改了 allow 之外的文件 → 违规（已提交的改动同样计入）。"""

    _write(repo, "tests/test_budget.py", _PROTECTED_TEST + "\n# 改动\n")
    _git(repo, "commit", "-qam", "touch test")
    report = _report(repo)
    assert any("超出允许范围：tests/test_budget.py" in line for line in report.violations)
    assert _run(repo)[0] == 1


def test_forbid_wins_over_allow(repo: Path) -> None:
    """P7-S0-A23 同时命中 allow 与 forbid 时按禁止处理。"""

    _write(repo, "src/tools/x.py", "TOOL = 2\n")
    report = _report(repo)
    assert report.violations == ["改动了禁止改的文件：src/tools/x.py"]


def test_rename_out_of_forbidden_dir_and_deletion_are_counted(repo: Path) -> None:
    """P7-S0-A24 移出禁区的重命名按旧路径计入；删除也算改动。"""

    _git(repo, "mv", "src/tools/x.py", "src/x_moved.py")
    _git(repo, "rm", "-q", "data/snap.json")
    report = _report(repo)
    assert "src/tools/x.py" in report.changed and "src/x_moved.py" in report.changed
    assert "data/snap.json" in report.changed
    assert "改动了禁止改的文件：src/tools/x.py" in report.violations
    assert any("frozen_json 文件被删除：data/snap.json" in line for line in report.violations)


def test_untracked_and_chinese_paths_are_counted(repo: Path) -> None:
    """P7-S0-A24 未跟踪文件计入；中文路径按原文报告（不被 git 转义）。"""

    _write(repo, "notes/new.txt", "x\n")
    _write(repo, "docs/中文说明.md", "改了\n")
    report = _report(repo)
    assert "notes/new.txt" in report.changed
    assert "docs/中文说明.md" in report.changed
    assert "改动超出允许范围：docs/中文说明.md" in report.violations


# ---------------------------------------------------------------------------
# protect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutated", "expected"),
    [
        (_PROTECTED_TEST.replace("assert cost < 4500", "assert cost < 4600"), "被改动或删除"),
        (_PROTECTED_TEST.replace("    assert cost < 4500", "    # assert cost < 4500"),
         "被改动或删除"),
        (_PROTECTED_TEST.replace("def test_budget", "@pytest.mark.skip\ndef test_budget"),
         "skip/xfail"),
        (_PROTECTED_TEST.replace("    cost = 10", "    pytest.skip('later')\n    cost = 10"),
         "pytest.skip"),
        (_PROTECTED_TEST.replace("def test_budget", "def test_budget_renamed"), "节点不存在"),
        ("pytestmark = pytest.mark.skip\n" + _PROTECTED_TEST, "pytestmark"),
    ],
)
def test_protected_assertion_must_survive(repo: Path, mutated: str, expected: str) -> None:
    """P7-S0-A25 受保护断言被改值、注释掉、skip、改名、整模块 skip → 违规。"""

    scope = {**_DEFAULT_SCOPE, "allow": [*_DEFAULT_SCOPE["allow"], "tests/**"]}
    _write(repo, "docs/prompt.md", _scope_doc(scope))
    _git(repo, "checkout", "-q", "main")
    _git(repo, "commit", "-qam", "widen scope on base")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--ff-only", "main")
    _write(repo, "tests/test_budget.py", mutated)

    report = _report(repo)
    assert any(expected in line for line in report.violations), report.violations


def test_protected_assertion_tolerates_formatting(repo: Path) -> None:
    """P7-S0-A25 只改排版（空白、换行、加注释）不算动了受保护断言；类内节点也能定位。"""

    reformatted = _PROTECTED_TEST.replace(
        'assert cost < 4500, "budget"', 'assert cost  <  4500, (  # 预算\n        "budget"\n    )'
    )
    item = {"node": "tests/test_budget.py::test_budget", "contains": "assert cost < 4500"}
    _write(repo, "tests/test_budget.py", reformatted)
    assert scope_guard.check_protected(repo, item) is None
    nested = {"node": "tests/test_budget.py::TestNested::test_inner", "contains": "1 + 1 == 2"}
    assert scope_guard.check_protected(repo, nested) is None


# ---------------------------------------------------------------------------
# frozen_json
# ---------------------------------------------------------------------------


def test_frozen_json_blocks_frozen_parts_only(repo: Path) -> None:
    """P7-S0-A26 frozen 片段改了 → 违规；同一文件里未冻结的片段可以改。"""

    snap = json.loads((repo / "data/snap.json").read_text(encoding="utf-8"))
    snap["profiles"]["a"]["lifecycle"]["m"] = 99
    _write(repo, "data/snap.json", json.dumps(snap))
    assert _report(repo).violations == []

    snap["profiles"]["b"]["surface"]["n"] = 99
    _write(repo, "data/snap.json", json.dumps(snap))
    assert _report(repo).violations == [
        "data/snap.json/profiles/b/surface 必须与 merge-base 相同，但被改动了"
    ]


def test_frozen_json_pointer_that_matches_nothing_is_reported(repo: Path) -> None:
    """P7-S0-A26 pointer 在 base 上一个片段都没匹配到 → 报告（防止写错 pointer 让冻结形同虚设）。"""

    item = {"path": "data/snap.json", "pointers": ["/profiles/*/surfaces"]}
    merge_base = scope_guard.resolve_merge_base(repo, "main")
    problems = scope_guard.check_frozen(repo, merge_base, item)
    assert any("没有匹配任何片段" in line for line in problems)


# ---------------------------------------------------------------------------
# 范围块来源、base、--bootstrap、--auto
# ---------------------------------------------------------------------------


def test_scope_block_edited_in_branch_fails(repo: Path) -> None:
    """P7-S0-A27 分支里放宽自己的范围块 → 违规（exit 1）；范围只能经 base 修改。"""

    widened = {**_DEFAULT_SCOPE, "allow": ["**"]}
    _write(repo, "docs/prompt.md", _scope_doc(widened))
    _write(repo, "tests/test_budget.py", _PROTECTED_TEST + "\n# 越界\n")

    assert _run(repo)[0] == 1
    report = _report(repo)
    assert report.violations and "范围块在本分支被改动" in report.violations[0]


def test_text_outside_the_block_may_change(repo: Path) -> None:
    """P7-S0-A27 执行 Prompt 正文（范围块以外）可以在分支里补充，例如追加执行记录。"""

    text = (repo / "docs/prompt.md").read_text(encoding="utf-8") + "\n## 执行记录\n- 已完成\n"
    _write(repo, "docs/prompt.md", text)
    assert _report(repo).violations == []


def test_missing_base_or_shallow_clone_cannot_be_judged(repo: Path, tmp_path: Path) -> None:
    """P7-S0-A28 base 不存在、浅克隆 → exit 2（无法判定，不是通过）。"""

    assert scope_guard.main(["docs/prompt.md", "--base", "no-such-ref", "--repo", str(repo)]) == 2

    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow))
    assert scope_guard.main(["docs/prompt.md", "--base", "origin/main", "--repo", str(shallow)]) == 2


def test_bootstrap_only_for_prompts_not_yet_on_base(repo: Path) -> None:
    """P7-S0-A29 ``--bootstrap``：新文档从工作区读范围块；已在 base 上的文档不得 bootstrap。"""

    _write(repo, "docs/new_prompt.md", _scope_doc({**_DEFAULT_SCOPE, "allow": ["docs/**"]}))
    assert scope_guard.main(
        ["docs/new_prompt.md", "--base", "main", "--repo", str(repo)]
    ) == 2  # 不在 base 上且没 bootstrap → 无法判定
    assert scope_guard.main(
        ["docs/new_prompt.md", "--base", "main", "--repo", str(repo), "--bootstrap"]
    ) == 0
    assert scope_guard.main(
        ["docs/prompt.md", "--base", "main", "--repo", str(repo), "--bootstrap"]
    ) == 2


def test_auto_mode_maps_branch_to_step_prompt(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S0-A30 ``--auto``：非 P7 分支与 S0 跳过；S<n≥1> 找不到 base 上的执行 Prompt → 违规。"""

    def auto() -> int:
        return scope_guard.main(["--auto", "--base", "main", "--repo", str(repo)])

    monkeypatch.setenv("GITHUB_HEAD_REF", "feature")
    assert auto() == 0
    monkeypatch.setenv("GITHUB_HEAD_REF", "YEK/p7-s0-spec-harness")
    assert auto() == 0
    monkeypatch.setenv("GITHUB_HEAD_REF", "YEK/p7-s1-confirmation-gate")
    assert auto() == 1

    _git(repo, "checkout", "-q", "main")
    _write(
        repo,
        "docs/execution-prompts/P7-S1-confirmation-gate.md",
        _scope_doc({**_DEFAULT_SCOPE, "allow": ["src/app.py"]}),
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add S1 prompt")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--ff-only", "main")
    _write(repo, "src/app.py", "VALUE = 2\n")
    assert auto() == 0
    _write(repo, "src/other.py", "x = 1\n")
    assert auto() == 1


# ---------------------------------------------------------------------------
# 本仓库的执行 Prompt
# ---------------------------------------------------------------------------


def _repo_scope_documents() -> list[Path]:
    prompts = sorted((REPO_ROOT / "docs" / "execution-prompts").glob("P7-S*.md"))
    return [REPO_ROOT / "docs" / "给排水agentP7插件化与MCP扩展任务书.md", *prompts]


def test_repo_scope_blocks_are_usable() -> None:
    """P7-S0-A31 本仓库任务书与 P7 执行 Prompt 的范围块都能解析；protect 节点当前完好，
    frozen_json 的 pointer 在当前基线上都能匹配到片段。"""

    documents = [d for d in _repo_scope_documents() if d.is_file()]
    for document in documents:
        scope = scope_guard.parse_scope(document.read_text(encoding="utf-8"))
        for item in scope.protect:
            assert scope_guard.check_protected(REPO_ROOT, item) is None, (document.name, item)
        for item in scope.frozen_json:
            target = json.loads((REPO_ROOT / item["path"]).read_text(encoding="utf-8"))
            for pointer in item["pointers"]:
                assert scope_guard._expand(target, pointer), (document.name, pointer)


def test_ci_runs_scope_guard_on_p7_pull_requests() -> None:
    """P7-S0-A32 PR 上以完整历史运行范围守卫（浅克隆会让 merge-base 不可信），只用标准库。"""

    workflow = (REPO_ROOT / ".github" / "workflows" / "p7-scope.yml").read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "fetch-depth: 0" in workflow
    assert 'python scripts/check_step_scope.py --auto --base "origin/${{ github.base_ref }}"' in workflow
    assert "uv sync" not in workflow and "pip install" not in workflow
    imported = {
        line.split()[1].split(".")[0]
        for line in (REPO_ROOT / "scripts" / "check_step_scope.py").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.startswith(("import ", "from "))
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported

"""P7-S0 H0：测试与门禁验证的必须是本检出的代码（``docs/design/16-p7-plugin-mcp.md`` §6.1）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_conftest_rejects_a_foreign_water_agent(tmp_path: Path) -> None:
    """P7-S0-A01 ``water_agent`` 从别处导入时，pytest 在收集前就报错退出（反证）。

    在 PYTHONPATH 最前面放一个假的 ``water_agent`` 包，模拟 worktree 里导入了主检出的代码。
    """

    fake = tmp_path / "foreign" / "water_agent"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(fake.parent)}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/test_import_guard.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "不是本检出" in output, output


def test_current_session_imports_this_checkout() -> None:
    """P7-S0-A01 本次 pytest 进程里的 ``water_agent`` 来自本检出的 ``src/``。"""

    import water_agent

    assert Path(water_agent.__file__).resolve() == (
        REPO_ROOT / "src" / "water_agent" / "__init__.py"
    ).resolve()


def test_gate_pins_import_location() -> None:
    """P7-S0-A02 门禁把 PYTHONPATH 钉到本检出，并在跑任何检查前核对实际导入位置。"""

    gate = (REPO_ROOT / "scripts" / "ci_gate.sh").read_text(encoding="utf-8")
    assert 'export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"' in gate
    pinned = gate.index("export PYTHONPATH=")
    checked = gate.index("water_agent 导入自")
    first_step = gate.index('step "pytest')
    assert pinned < checked < first_step

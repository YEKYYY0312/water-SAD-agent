"""Desktop acceptance harness must be isolated from real user data."""
from pathlib import Path

import pytest

from desktop.macos.packaging.smoke import require_disposable_root, safe_ready_url


def test_smoke_rejects_real_or_nonempty_user_root(tmp_path: Path):
    real = Path.home() / "Library/Application Support/WaterAgent"
    with pytest.raises(ValueError):
        require_disposable_root(real)
    populated = tmp_path / "数据"
    populated.mkdir()
    (populated / "existing.txt").write_text("user data")
    with pytest.raises(ValueError):
        require_disposable_root(populated)
    disposable = tmp_path / "新测试 数据"
    assert require_disposable_root(disposable) == disposable.resolve()


@pytest.mark.parametrize("url", [
    "http://example.com:1024", "http://127.0.0.1:8000", "http://127.0.0.1:8001",
    "http://user:password@127.0.0.1:19090", "file:///etc/passwd",
    "http://127.0.0.1:19090/path", "http://127.0.0.1:19090?secret=value",
])
def test_smoke_never_targets_production_or_foreign_origin(url: str):
    with pytest.raises(ValueError):
        safe_ready_url(url)


def test_ready_accepts_only_bare_loopback_dynamic_endpoint():
    assert safe_ready_url("http://127.0.0.1:49153") == "http://127.0.0.1:49153"

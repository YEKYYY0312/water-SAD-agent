"""模型目录（P7-S15-A01，``docs/design/18-p7-multi-model.md`` §3.1）。"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from water_agent.generation import model_catalog as mc


@pytest.fixture(autouse=True)
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "credentials"
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(directory))
    return directory


def _entry(model_id: str = "flash-a", **overrides: object) -> dict[str, object]:
    return {"id": model_id, "label": "快速", "tier": "fast", **overrides}


def test_no_catalog_file_means_an_empty_catalog_without_error() -> None:
    """P7-S15-A01 缺省不配目录 = 空目录、无错误；没有任何模型的额外装配参数——行为与 S15 之前逐字相同。"""

    state = mc.load_catalog()
    assert state.entries == () and state.error is None
    assert mc.entry_for("flash-a") is None
    assert mc.extra_body_for("flash-a") is None
    assert mc.extra_body_for(None) is None


def test_write_then_read_round_trips_with_a_private_atomic_file(credentials: Path) -> None:
    """P7-S15-A01 原子写（同目录临时文件 + replace，0600）；读回与写入逐项相等，没有残留临时文件。"""

    written = mc.write_catalog(
        mc.parse_entries(
            [
                _entry("flash-a", thinking="off"),
                _entry(
                    "pro-b",
                    label="高级",
                    tier="advanced",
                    thinking="roundtrip",
                    allowed_roles=["admin"],
                    price={"input_per_1m": 0.5, "output_per_1m": 2.0},
                ),
            ]
        )
    )
    path = credentials / mc.CATALOG_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(credentials.glob("*.tmp"))
    state = mc.load_catalog()
    assert state.error is None and state.entries == written
    assert state.find("pro-b").allowed_roles == ("admin",)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == mc.CATALOG_FORMAT


@pytest.mark.parametrize(
    "raw",
    [
        [_entry("a"), _entry("a")],  # id 重复
        [_entry("bad name!")],  # id 字符集
        [_entry("x" * 201)],  # id 过长
        [_entry("a", label="  ")],  # 空 label
        [_entry("a", label="含\x00控制字符")],
        [_entry("a", tier="best")],  # 未知档位
        [_entry("a", thinking="max")],  # 未知思考配置
        [_entry("a", allowed_roles=[])],  # 空角色列表
        [_entry("a", allowed_roles=["admin", "admin"])],  # 角色重复
        [_entry("a", allowed_roles=["ADMIN"])],  # 角色名格式
        [_entry("a", base_url="https://evil.example/v1")],  # 目录不接受端点字段
        [_entry("a", api_key="sk-secret-0000000")],  # 目录不接受密钥
        [_entry("a", price={"input_per_1m": -1, "output_per_1m": 1})],
        [_entry(f"m{i}") for i in range(mc.MAX_ENTRIES + 1)],  # 超过条数上限
    ],
)
def test_invalid_catalogs_are_rejected_without_echoing_values(raw: list[dict[str, object]]) -> None:
    """P7-S15-A01 id / label / 档位 / 思考配置 / 角色 / 价格 / 条数逐项校验；**端点与密钥字段一律拒绝**
    （P7-I15：目录不收 base_url / api_key）；错误说明只写第几项与字段名，不回显值。"""

    with pytest.raises(mc.CatalogError) as raised:
        mc.parse_entries(raw)
    message = str(raised.value)
    assert "evil.example" not in message and "sk-secret" not in message


def test_a_corrupt_or_foreign_file_fails_closed_with_an_error(credentials: Path) -> None:
    """P7-S15-A01 文件读不出 / 字段不对 / 版本不对 → 空目录 + error（不当作"有模型可选"），也不抛异常拖垮装配。"""

    credentials.mkdir(parents=True)
    path = credentials / mc.CATALOG_FILE
    for text in (
        "not json",
        json.dumps({"version": 1}),
        json.dumps({"version": 1, "models": [], "extra": 1}),
        json.dumps({"version": 99, "models": []}),
        json.dumps({"version": 1, "models": [_entry("a", base_url="https://x.example/v1")]}),
    ):
        path.write_text(text, encoding="utf-8")
        state = mc.load_catalog()
        assert state.entries == () and state.error, text
    # 读不出的目录上，全局模型的装配行为不变（没有思考配置附加）
    assert mc.extra_body_for("a") is None


def test_thinking_profiles_map_to_the_request_body(credentials: Path) -> None:
    """P7-S15-A01/A03 ``off`` 显式关闭思考；``default`` 与 ``roundtrip`` 在 S15 里什么都不传；目录外的 id 什么都不传。
    装配与兼容性探针共用 ``extra_body_for``——同一个模型两处带同一份请求体。"""

    mc.write_catalog(
        mc.parse_entries(
            [
                _entry("off-model", thinking="off"),
                _entry("plain-model"),
                _entry("thinker", tier="advanced", thinking="roundtrip"),
            ]
        )
    )
    assert mc.extra_body_for("off-model") == {"thinking": {"type": "disabled"}}
    assert mc.extra_body_for("plain-model") is None
    assert mc.extra_body_for("thinker") is None
    assert mc.extra_body_for("not-in-catalog") is None


def test_members_only_see_identity_fields() -> None:
    """P7-S15-A01 成员可见 id / label / tier / thinking；价格与角色限制是管理员的配置细节。"""

    entry = mc.CatalogEntry.model_validate(
        _entry("a", allowed_roles=["admin"], price={"input_per_1m": 1, "output_per_1m": 2})
    )
    assert entry.public_view() == {"id": "a", "label": "快速", "tier": "fast", "thinking": "default"}

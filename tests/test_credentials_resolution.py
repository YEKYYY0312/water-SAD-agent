"""LLM 凭据解析（env → {ENV}_FILE → 凭据卷）与写入的测试。

重点是那道曾经存在的裂缝：``real_llm_configured()`` 认 ``DEEPSEEK_API_KEY_FILE``
而 ``build_chat_model()`` 只认环境变量，于是手工配了 secret 文件的部署会得到
「capabilities 报 real_llm=true、424 门放行、真跑时抛 ValueError 被捕成 503」这种
自相矛盾的结果。下面的 test_secret_file_alone_lets_chat_model_build 是它的回归测试。

全程离线：不发起任何网络请求。
"""
from __future__ import annotations

import os
import stat

import pytest
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from water_agent.config.credentials import (
    CREDENTIALS,
    CREDENTIALS_DIR_ENV,
    SOURCE_ENV,
    SOURCE_NONE,
    SOURCE_SECRET_FILE,
    SOURCE_VOLUME,
    CredentialStoreUnavailableError,
    InvalidCredentialError,
    UnknownCredentialError,
    credential_status,
    credentials_dir,
    delete_credential,
    forget_injected_credentials,
    load_credentials_into_environ,
    resolve_secret,
    write_credential,
)
from water_agent.embedding import build_embedding_provider
from water_agent.generation import chat_model as chat_model_module
from water_agent.generation.chat_model import build_chat_model
from water_agent.runtime.agentic import real_llm_configured

DEEPSEEK = "DEEPSEEK_API_KEY"
DASHSCOPE = "DASHSCOPE_API_KEY"


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch) -> None:
    """给每个测试一个独立凭据卷，并保证密钥不会漏进后续测试的环境。"""
    monkeypatch.setenv(CREDENTIALS_DIR_ENV, str(tmp_path / "credentials"))
    for env_name in CREDENTIALS:
        monkeypatch.delenv(env_name, raising=False)
        monkeypatch.delenv(f"{env_name}_FILE", raising=False)
    # 本地模型会短路 real_llm_configured，显式关掉以便测远端密钥分支。
    monkeypatch.delenv("LOCAL_MODEL_ENABLED", raising=False)
    forget_injected_credentials()
    yield
    # write_credential 用 os.environ[...] 直接赋值，monkeypatch 追踪不到，
    # 必须手工清掉，否则密钥会污染后续测试。注入记录是模块级的，同理。
    for env_name in CREDENTIALS:
        os.environ.pop(env_name, None)
    forget_injected_credentials()


def _write_secret_file(tmp_path, name: str, value: str) -> str:
    path = tmp_path / name
    path.write_text(value, encoding="utf-8")
    return str(path)


def _seed_volume_file(env_name: str, value: str) -> None:
    """直接把值落进凭据卷，绕开 ``write_credential`` 的环境注入副作用。

    用 ``write_credential`` 来"准备卷内容"会顺带写 ``os.environ``，把待测的
    优先级前提破坏掉；测优先级时必须只动文件。
    """
    directory = credentials_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / CREDENTIALS[env_name]).write_text(value, encoding="utf-8")


# ---------------------------------------------------------------------------
# 优先级
# ---------------------------------------------------------------------------


def test_environment_variable_wins_over_every_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(DEEPSEEK, "sk-from-environment")
    monkeypatch.setenv(DEEPSEEK + "_FILE", _write_secret_file(tmp_path, "sf", "sk-from-secret-file"))
    _seed_volume_file(DEEPSEEK, "sk-from-volume")

    assert resolve_secret(DEEPSEEK) == "sk-from-environment"
    assert credential_status(DEEPSEEK).source == SOURCE_ENV


def test_secret_file_wins_over_the_volume(tmp_path, monkeypatch) -> None:
    _seed_volume_file(DEEPSEEK, "sk-from-volume")
    monkeypatch.setenv(DEEPSEEK + "_FILE", _write_secret_file(tmp_path, "sf", "sk-from-secret-file"))

    assert resolve_secret(DEEPSEEK) == "sk-from-secret-file"
    assert credential_status(DEEPSEEK).source == SOURCE_SECRET_FILE


def test_volume_is_used_when_nothing_else_is_configured() -> None:
    _seed_volume_file(DEEPSEEK, "sk-from-volume")

    assert resolve_secret(DEEPSEEK) == "sk-from-volume"
    status = credential_status(DEEPSEEK)
    assert status.source == SOURCE_VOLUME
    assert status.updated_at is not None, "卷内文件应带 mtime，便于面板显示更新时间"


def test_nothing_configured_reports_unconfigured() -> None:
    assert resolve_secret(DEEPSEEK) is None
    status = credential_status(DEEPSEEK)
    assert status.configured is False
    assert status.source == SOURCE_NONE
    assert status.updated_at is None


def test_empty_and_whitespace_only_files_count_as_unconfigured(tmp_path, monkeypatch) -> None:
    """Compose 的 `file:` 要求文件存在，因此空占位文件必须等同于未配置。"""
    monkeypatch.setenv(DEEPSEEK + "_FILE", _write_secret_file(tmp_path, "empty", ""))
    assert resolve_secret(DEEPSEEK) is None

    monkeypatch.setenv(DEEPSEEK + "_FILE", _write_secret_file(tmp_path, "blank", "  \n "))
    assert resolve_secret(DEEPSEEK) is None
    assert credential_status(DEEPSEEK).configured is False


def test_missing_file_path_does_not_raise(monkeypatch) -> None:
    monkeypatch.setenv(DEEPSEEK + "_FILE", "/nonexistent/path/to/key")
    assert resolve_secret(DEEPSEEK) is None


def test_unknown_names_resolve_to_none_instead_of_raising(monkeypatch) -> None:
    """CLI 的 --api-key-env 允许任意变量名，解析器不能因此报错。"""
    monkeypatch.setenv("MY_CUSTOM_LLM_KEY", "sk-custom-value")
    assert resolve_secret("MY_CUSTOM_LLM_KEY") == "sk-custom-value"
    assert resolve_secret("TOTALLY_UNSET_KEY") is None


def test_credential_status_rejects_unknown_names() -> None:
    with pytest.raises(UnknownCredentialError):
        credential_status("NOT_A_MANAGED_CREDENTIAL")


# ---------------------------------------------------------------------------
# 裂缝回归：只有 secret 文件时，两条路必须结论一致
# ---------------------------------------------------------------------------


def test_real_llm_configured_agrees_with_resolve_secret(tmp_path, monkeypatch) -> None:
    assert real_llm_configured() is False

    monkeypatch.setenv(DEEPSEEK + "_FILE", _write_secret_file(tmp_path, "sf", "sk-secret-file"))
    assert real_llm_configured() is True
    assert resolve_secret(DEEPSEEK) is not None


def test_real_llm_configured_sees_the_volume(monkeypatch) -> None:
    write_credential(DEEPSEEK, "sk-written-through-panel")
    monkeypatch.delenv(DEEPSEEK, raising=False)
    assert real_llm_configured() is True


def test_secret_file_alone_lets_chat_model_build(tmp_path, monkeypatch) -> None:
    """裂缝回归：以前只配 _FILE 会让 build_chat_model 抛 ValueError。"""
    captured: dict[str, object] = {}

    class _SpyChatOpenAI(ChatOpenAI):
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(chat_model_module, "ChatOpenAI", _SpyChatOpenAI)
    monkeypatch.setenv(DEEPSEEK + "_FILE", _write_secret_file(tmp_path, "sf", "sk-secret-file"))

    build_chat_model()

    secret = captured["api_key"]
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == "sk-secret-file"


def test_chat_model_error_message_names_all_three_ways(monkeypatch) -> None:
    monkeypatch.delenv(DEEPSEEK, raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY must not be empty") as error:
        build_chat_model()
    message = str(error.value)
    assert "DEEPSEEK_API_KEY_FILE" in message
    assert "设置面板" in message


# ---------------------------------------------------------------------------
# 写入 / 删除
# ---------------------------------------------------------------------------


def test_write_credential_persists_and_injects_environment() -> None:
    write_credential(DEEPSEEK, "sk-panel-written")

    assert (credentials_dir() / CREDENTIALS[DEEPSEEK]).read_text(encoding="utf-8") == (
        "sk-panel-written"
    )
    assert os.environ[DEEPSEEK] == "sk-panel-written", (
        "必须注入进程环境：embedding.py 与 rerank.py 仍直接读 os.environ"
    )


def test_panel_written_credential_reports_the_volume_not_the_environment() -> None:
    """回归：注入 env 曾让来源一律显示为 env，用户便看不出密钥配在哪。"""
    write_credential(DEEPSEEK, "sk-panel-written")

    status = credential_status(DEEPSEEK)

    assert status.source == SOURCE_VOLUME
    assert status.updated_at is not None


def test_backfilled_secret_file_still_reports_the_file(tmp_path, monkeypatch) -> None:
    """回归：启动回填写了 env，来源仍须追到真正的 Compose secret。"""
    monkeypatch.setenv(DEEPSEEK + "_FILE", _write_secret_file(tmp_path, "sf", "sk-secret-file"))

    load_credentials_into_environ()

    assert os.environ[DEEPSEEK] == "sk-secret-file"
    assert credential_status(DEEPSEEK).source == SOURCE_SECRET_FILE


def test_explicit_environment_variable_is_not_mistaken_for_injection(monkeypatch) -> None:
    """用户显式设的 env 必须仍报 env：回填不会覆盖它，也不该被归为注入。"""
    monkeypatch.setenv(DEEPSEEK, "sk-set-by-operator")
    _seed_volume_file(DEEPSEEK, "sk-in-volume")

    load_credentials_into_environ()

    assert credential_status(DEEPSEEK).source == SOURCE_ENV
    assert resolve_secret(DEEPSEEK) == "sk-set-by-operator"


def test_delete_leaves_an_operator_set_environment_variable_alone(monkeypatch) -> None:
    """面板删除不得清掉运维显式设置的环境变量——那超出面板的管辖范围。"""
    monkeypatch.setenv(DEEPSEEK, "sk-set-by-operator")

    delete_credential(DEEPSEEK)

    assert os.environ[DEEPSEEK] == "sk-set-by-operator"
    assert credential_status(DEEPSEEK).source == SOURCE_ENV


def test_written_credential_file_is_owner_only() -> None:
    write_credential(DEEPSEEK, "sk-panel-written")
    mode = (credentials_dir() / CREDENTIALS[DEEPSEEK]).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH


def test_write_credential_leaves_no_temporary_file() -> None:
    write_credential(DEEPSEEK, "sk-panel-written")
    leftovers = list(credentials_dir().glob("*.tmp"))
    assert leftovers == [], f"原子写不应留下临时文件：{leftovers}"


def test_write_credential_overwrites_the_previous_value() -> None:
    write_credential(DEEPSEEK, "sk-first-value")
    write_credential(DEEPSEEK, "sk-second-value")
    assert resolve_secret(DEEPSEEK) == "sk-second-value"


def test_write_credential_rejects_unknown_names() -> None:
    with pytest.raises(UnknownCredentialError):
        write_credential("NOT_A_MANAGED_CREDENTIAL", "sk-whatever")


@pytest.mark.parametrize(
    "bad_value",
    ["", "   ", "short", "sk-with space", "sk-with\nnewline", "export KEY=sk-pasted-whole-line"],
)
def test_write_credential_rejects_malformed_values(bad_value: str) -> None:
    with pytest.raises(InvalidCredentialError):
        write_credential(DEEPSEEK, bad_value)


def test_delete_credential_removes_file_and_environment_value() -> None:
    write_credential(DEEPSEEK, "sk-panel-written")
    delete_credential(DEEPSEEK)

    assert not (credentials_dir() / CREDENTIALS[DEEPSEEK]).exists()
    assert DEEPSEEK not in os.environ
    assert resolve_secret(DEEPSEEK) is None


def test_delete_credential_is_idempotent() -> None:
    delete_credential(DEEPSEEK)  # 从未写过也不应报错
    assert resolve_secret(DEEPSEEK) is None


def test_delete_credential_does_not_hide_a_secret_file(tmp_path, monkeypatch) -> None:
    """卷内删除不影响 Compose secret：那一层不由本进程管理。"""
    monkeypatch.setenv(DEEPSEEK + "_FILE", _write_secret_file(tmp_path, "sf", "sk-secret-file"))
    write_credential(DEEPSEEK, "sk-panel-written")

    delete_credential(DEEPSEEK)

    assert resolve_secret(DEEPSEEK) == "sk-secret-file"
    assert credential_status(DEEPSEEK).source == SOURCE_SECRET_FILE


def test_unwritable_store_raises_store_unavailable(tmp_path, monkeypatch) -> None:
    """裸跑开发（未挂载 volume）时必须给出可映射为 503 的错误，而不是崩溃。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    monkeypatch.setenv(CREDENTIALS_DIR_ENV, str(blocked / "credentials"))

    with pytest.raises(CredentialStoreUnavailableError):
        write_credential(DEEPSEEK, "sk-panel-written")


# ---------------------------------------------------------------------------
# 启动回填：让 embedding / rerank 无需改动即可生效
# ---------------------------------------------------------------------------


def test_backfill_fills_only_missing_variables(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(DEEPSEEK, "sk-already-in-environment")
    monkeypatch.setenv(DASHSCOPE + "_FILE", _write_secret_file(tmp_path, "ds", "sk-bailian-file"))

    filled = load_credentials_into_environ()

    assert filled == [DASHSCOPE]
    assert os.environ[DEEPSEEK] == "sk-already-in-environment", "已有值不得被覆盖"
    assert os.environ[DASHSCOPE] == "sk-bailian-file"


def test_backfill_reads_the_volume() -> None:
    write_credential(DASHSCOPE, "sk-bailian-from-panel")
    os.environ.pop(DASHSCOPE, None)

    assert load_credentials_into_environ() == [DASHSCOPE]
    assert os.environ[DASHSCOPE] == "sk-bailian-from-panel"


def test_backfill_is_a_noop_when_nothing_is_configured() -> None:
    assert load_credentials_into_environ() == []


def test_backfill_makes_embedding_provider_constructible(tmp_path, monkeypatch) -> None:
    """embedding.py 零改动的依据：回填后它读到的 os.environ 已有值。"""
    monkeypatch.setenv(DASHSCOPE + "_FILE", _write_secret_file(tmp_path, "ds", "sk-bailian-file"))

    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        build_embedding_provider()  # 回填前：只认 os.environ，看不见 _FILE

    load_credentials_into_environ()

    provider = build_embedding_provider()
    assert provider is not None


# ---------------------------------------------------------------------------
# 不泄漏
# ---------------------------------------------------------------------------


def test_status_never_carries_the_secret_value() -> None:
    write_credential(DEEPSEEK, "sk-super-secret-value")
    rendered = repr(credential_status(DEEPSEEK))
    assert "sk-super-secret-value" not in rendered
    assert "super-secret" not in rendered

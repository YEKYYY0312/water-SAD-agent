"""LLM 凭据解析：环境变量 → Compose secret 文件 → 可写凭据卷。

同一个密钥可能来自三处，优先级从高到低：

1. 环境变量本身（如 ``DEEPSEEK_API_KEY``），CLI 与 harness 一直走这条；
2. ``{ENV}_FILE`` 指向的文件，即 Compose secret，挂在 ``/run/secrets`` 下只读；
3. 凭据卷内的同名文件，Web 设置面板写入的位置，可读写。

``secrets/README.md`` 早已约定第 2 条的文件名，但 Compose 从未挂载它们，于是
``real_llm_configured()`` 认 ``_FILE``、``build_chat_model()`` 只认环境变量——
手工配了文件反而得到「424 放行、真跑时 503」。本模块把三条路收敛到
:func:`resolve_secret`：对话模型、embedding、rerank、视觉审阅与各处预检共用
同一判断。

启动回填与面板写入/清除仍会把**有效值**同步进当前进程环境。本进程内的消费方
已不依赖它；保留同步是为了 os.environ 里不残留过时或被顶替的值，仍按变量名读
环境的代码（CLI / harness 的旧路径）看到的也是有效值。子进程拿不到这些密钥：
MCP 与 DWG 转换都只透传白名单变量。同步只动本模块注入的值：运维显式设置的
环境变量是最高优先级，面板既不覆盖也不清除它。
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# env 变量名 → 凭据卷内的文件名。这张表同时是白名单：不在表内的名字一律
# 拒绝，因此落盘文件名永不来自调用方输入，无需再做路径穿越校验。
#
# P7-S10：``WATER_AGENT_LLM_API_KEY`` / ``WATER_AGENT_EMBEDDING_API_KEY`` 是 OpenAI 兼容对话 / embedding
# 端点的专用 Key（端点由 generation/endpoints.py 解析）。端点仍是 DeepSeek / 百炼时它们未配置会回落
# 到前两把；端点改到别家就不回落——密钥只发给它所属的服务商。
CREDENTIALS: Mapping[str, str] = {
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "DASHSCOPE_API_KEY": "dashscope_api_key",
    "WATER_AGENT_LLM_API_KEY": "water_agent_llm_api_key",
    "WATER_AGENT_EMBEDDING_API_KEY": "water_agent_embedding_api_key",
    # P7-S12：联网搜索服务（brave 等有密钥的 provider）的凭据槽位。搜索服务的**地址 / provider / 开关**只由运维的
    # 环境变量决定（``web_search/config.py``），面板最多写这把密钥（只写不读）。
    "WATER_AGENT_WEB_SEARCH_API_KEY": "water_agent_web_search_api_key",
}

# 凭据卷挂载点。它刻意不在 /var/lib/water-agent 之下：scripts/backup.sh 打包
# 的是那个目录整体（tar -C /var/lib/water-agent .），挂进去会把密钥明文写进
# 备份包。挂在外面则备份脚本无需改动且天然不含凭据。
DEFAULT_CREDENTIALS_DIR = Path("/var/lib/water-agent-credentials")
CREDENTIALS_DIR_ENV = "WATER_AGENT_CREDENTIALS_DIR"

# 仅用于挡住误填（空串、把提示文字粘进来）；不设上限，避免将来密钥变长时误拒。
MIN_CREDENTIAL_LENGTH = 8

SOURCE_ENV = "env"
SOURCE_SECRET_FILE = "secret_file"
SOURCE_VOLUME = "volume"
SOURCE_NONE = "none"

# 环境变量里由本模块注入（而非用户显式设置）的变量名。
#
# 写入与启动回填都会写 os.environ，而 os.environ 是解析优先级最高的一层；若不
# 把这件事记下来，:func:`credential_status` 会把每一种来源都报成 "env"，用户便
# 再也看不出密钥究竟配在 Compose secret 还是设置面板里。:func:`_sync_environ`
# 也靠它区分「可以改写的注入值」与「不得触碰的运维环境变量」。这是进程内状态，
# 与单 worker 部署下的配置生命周期一致。
_INJECTED: set[str] = set()


class UnknownCredentialError(KeyError):
    """请求的凭据名不在 :data:`CREDENTIALS` 白名单内。"""


class InvalidCredentialError(ValueError):
    """凭据值为空、含空白字符或过短。"""


class CredentialStoreUnavailableError(RuntimeError):
    """凭据卷不存在或不可写（例如未挂载 volume 的裸跑开发环境）。"""


@dataclass(frozen=True)
class CredentialStatus:
    """一个凭据的配置状态。**刻意不含密钥值本身，也不含末尾几位。**"""

    name: str
    configured: bool
    source: str
    updated_at: str | None


def credentials_dir() -> Path:
    """凭据卷路径；``WATER_AGENT_CREDENTIALS_DIR`` 可覆盖（测试与裸跑开发用）。"""
    override = os.environ.get(CREDENTIALS_DIR_ENV, "").strip()
    return Path(override) if override else DEFAULT_CREDENTIALS_DIR


def _read_secret_file(path_value: str | Path | None) -> str | None:
    """读密钥文件，空文件与读取失败一律视为未配置。

    与 ``storage/database.py`` 的同名函数同形：本仓库的既有风格是各处保留
    一份小副本，而不是为 6 行逻辑建跨层依赖。
    """
    if not path_value:
        return None
    try:
        value = Path(path_value).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def require_known(env_name: str) -> str:
    """校验凭据名在白名单内，返回规范化后的名字。"""
    if env_name not in CREDENTIALS:
        raise UnknownCredentialError(env_name)
    return env_name


def validate_credential_value(value: str) -> str:
    """校验并清洗待写入的密钥值。

    拒绝内部含空白的值：密钥本身不含空格或换行，出现即说明用户误粘了
    整行 ``export FOO=...`` 或多行文本，此时静默保存会产出一个永远认证
    失败的配置。
    """
    cleaned = value.strip()
    if not cleaned:
        raise InvalidCredentialError("密钥不能为空")
    if any(character.isspace() for character in cleaned):
        raise InvalidCredentialError("密钥不能包含空格或换行")
    if len(cleaned) < MIN_CREDENTIAL_LENGTH:
        raise InvalidCredentialError(f"密钥长度不足 {MIN_CREDENTIAL_LENGTH} 个字符")
    return cleaned


def resolve_secret(
    env_name: str,
    *,
    getenv: Callable[[str], str | None] = os.getenv,
) -> str | None:
    """按 env → ``{env}_FILE`` → 凭据卷 的优先级解析密钥，全部缺失时返回 ``None``。

    ``getenv`` 采用 ``os.getenv`` 的 callable 形态，以便直接对接
    ``real_llm_configured(environment=...)`` 既有签名。

    白名单外的名字**不报错**：CLI 的 ``--api-key-env`` 允许指定任意变量名，
    此时前两层照常生效，只是没有对应的卷文件可查。
    """
    direct = (getenv(env_name) or "").strip()
    if direct:
        return direct
    from_secret_file = _read_secret_file(getenv(f"{env_name}_FILE"))
    if from_secret_file:
        return from_secret_file
    file_name = CREDENTIALS.get(env_name)
    if file_name is None:
        return None
    return _read_secret_file(credentials_dir() / file_name)


def credential_status(
    env_name: str,
    *,
    getenv: Callable[[str], str | None] = os.getenv,
) -> CredentialStatus:
    """报告凭据来自哪一层，供设置面板展示。绝不返回密钥值。

    只有**用户显式设置**的环境变量才报 ``env``：本模块注入的那些会被跳过，
    继续往下追到真正的文件来源，否则回填之后一切都会显示成 ``env``。
    """
    require_known(env_name)
    if (getenv(env_name) or "").strip() and env_name not in _INJECTED:
        return CredentialStatus(env_name, True, SOURCE_ENV, None)

    secret_file = getenv(f"{env_name}_FILE")
    if _read_secret_file(secret_file):
        return CredentialStatus(env_name, True, SOURCE_SECRET_FILE, _mtime_of(secret_file))

    volume_path = credentials_dir() / CREDENTIALS[env_name]
    if _read_secret_file(volume_path):
        return CredentialStatus(env_name, True, SOURCE_VOLUME, _mtime_of(volume_path))

    return CredentialStatus(env_name, False, SOURCE_NONE, None)


def _mtime_of(path_value: str | Path | None) -> str | None:
    if not path_value:
        return None
    try:
        stamp = Path(path_value).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=UTC).isoformat()


def _resolve_beneath_injection(env_name: str) -> str | None:
    """按 :func:`resolve_secret` 的口径解析，但把本模块注入的 env 值当作不存在。

    注入的值只是某一层文件在某一时刻的副本；写入/删除之后它可能已经过时，
    拿它参与解析会让旧值自我延续。
    """

    def getenv(name: str) -> str | None:
        if name == env_name:
            return None
        return os.environ.get(name)

    return resolve_secret(env_name, getenv=getenv)


def _sync_environ(env_name: str) -> None:
    """让进程环境里的 ``env_name`` 与三层解析得到的有效值保持一致。

    - 运维显式设置的环境变量（非空且不在 :data:`_INJECTED`）本身就是最高优先级
      的有效值，也不归面板管理：一律不碰；
    - 否则重新解析 secret 文件与凭据卷：有有效值就注入，没有才清掉先前的注入。

    此前写入无条件覆盖 env、删除无条件 pop 注入值：前者顶掉运维的 env（重启后
    又悄悄换回去），后者连 Compose secret 回填的值也一起删——面板仍报已配置，
    读环境的消费方却拿不到密钥，直到重启。
    """
    if (os.environ.get(env_name) or "").strip() and env_name not in _INJECTED:
        return
    effective = _resolve_beneath_injection(env_name)
    if effective:
        os.environ[env_name] = effective
        _INJECTED.add(env_name)
    elif env_name in _INJECTED:
        os.environ.pop(env_name, None)
        _INJECTED.discard(env_name)


def write_credential(env_name: str, value: str) -> None:
    """写入凭据卷，再把进程环境同步到三层解析的有效值。

    卷是最低一层：环境变量或 Compose secret 在生效时，写入照样落盘，但生效的
    仍是更高优先级的那一层——:func:`credential_status` 如实报告来源，设置面板
    据此显示。卷是唯一来源时，单 worker 部署下（见 compose.yml 中 app 服务的
    注释）同步这一步足以让下一次 run 用上新密钥，无需重启进程。
    """
    require_known(env_name)
    cleaned = validate_credential_value(value)
    target = credentials_dir() / CREDENTIALS[env_name]
    temporary = target.with_name(f"{target.name}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 以 0600 建临时文件再 os.replace：读者永远看不到半截内容，密钥也
        # 不会有任何一刻以默认权限落盘。
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, cleaned.encode("utf-8"))
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise CredentialStoreUnavailableError(str(credentials_dir())) from error
    _sync_environ(env_name)


def delete_credential(env_name: str) -> None:
    """删除凭据卷内的文件，再把进程环境同步到剩余来源的有效值。

    环境变量或 Compose secret 提供的凭据不受影响——那两层不由本进程管理：
    运维的环境变量原样保留，secret 文件的值仍留在进程环境里；删除后
    :func:`credential_status` 会如实报告它仍来自更高优先级的来源。只有卷是
    唯一来源时，进程环境里的注入值才随之清掉。
    """
    require_known(env_name)
    target = credentials_dir() / CREDENTIALS[env_name]
    try:
        target.unlink(missing_ok=True)
    except OSError as error:
        raise CredentialStoreUnavailableError(str(credentials_dir())) from error
    _sync_environ(env_name)


def load_credentials_into_environ(
    environ: MutableMapping[str, str] | None = None,
) -> list[str]:
    """把 secret 文件与凭据卷里的密钥回填进环境变量，返回被填充的变量名。

    进程启动时调用一次即可让重启前配好的密钥继续生效。已在环境里的值优先，
    不被覆盖。
    """
    target_environ = os.environ if environ is None else environ
    filled: list[str] = []
    for env_name in CREDENTIALS:
        if (target_environ.get(env_name) or "").strip():
            continue
        value = resolve_secret(env_name, getenv=target_environ.get)
        if value:
            target_environ[env_name] = value
            _INJECTED.add(env_name)
            filled.append(env_name)
    return filled


def forget_injected_credentials() -> None:
    """清空注入记录。仅供测试隔离使用，生产代码不应调用。"""
    _INJECTED.clear()

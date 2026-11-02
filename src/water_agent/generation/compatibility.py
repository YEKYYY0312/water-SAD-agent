"""对话端点的兼容性证据与装配门槛（P7-S10，规格 §3.11）。

换对话模型时兼容性探针是**硬门槛**：铁律②依赖模型真的用 function calling 调 ``cite_clauses``，弱模型会
在引用校验上静默失败。非缺省端点（``base_url`` 或 ``model`` 与 DeepSeek 缺省值不同）必须有一份端点 +
模型精确匹配、探针版本一致、全部检查 ``passed`` 的报告才允许装配；**缺省 DeepSeek 端点免检**（现有质量
证据就是在它上面取得的，rag_p1 real 84.48%），这是唯一例外，不得扩大。

报告模型与"精确匹配 + 全部 passed"判定从 P5 Step 46 的 ``local_model.py`` 抽出，本地模型路径与远程端点
共用；两条路径的检查集不同（本地多一项离线的 ``memory_p0``；远程多 ``streaming_usage`` 并把取消 / 错误
处理改为真实测量），各自声明。报告只是证据，不是"测试跑过了"的断言：读不出、字段不对、版本不对一律当作
没有证据。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from water_agent.generation.endpoints import (
    SOURCE_LOCAL_MODEL,
    ChatEndpoint,
    normalize_base_url,
)

Getenv = Callable[[str], str | None]
CheckStatus = Literal["passed", "failed", "blocked", "skipped"]

#: 运维指定的兼容性报告路径。未设时看面板"运行兼容性检查"写到凭据卷里的那份。
COMPATIBILITY_REPORT_ENV = "WATER_AGENT_LLM_COMPATIBILITY_REPORT"
PANEL_REPORT_FILE = "llm_compatibility_report.json"
#: P7-S15：逐对（端点 + 模型）的兼容性报告目录。每个 (端点, 模型) 一个文件，文件名只是索引（端点 + 模型的摘要），
#: **读出后仍按 ``matches()`` 复核**——文件名不是信任根。原单文件路径（上面的 env / 面板单文件）保持不变。
PAIR_REPORT_DIR = "llm_compatibility_reports"
#: 远程端点探针的版本：检查集或判定变化时递进，旧报告随之失效。
ENDPOINT_PROBE_VERSION = "p7-s10-2026-10-13"
#: 远程 OpenAI 兼容端点必须全部 passed 的检查（``memory_p0`` 是离线套件、与端点无关，不在其中）。
ENDPOINT_COMPATIBILITY_CHECKS = (
    "function_calling",
    "structured_citations",
    "programmatic_calculation",
    "tool_schema_budget",
    "streaming_usage",
    "error_handling",
    "cancellation_timeout",
)
MAX_TOOL_SCHEMA_TOKENS = 4500

VERIFICATION_DEFAULT_EXEMPT = "default_exempt"
VERIFICATION_VERIFIED = "verified"
VERIFICATION_UNVERIFIED = "unverified"


class CompatibilityReport(BaseModel):
    """Machine-readable evidence; this is not an assertion that tests ran."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    status: Literal["compatible", "blocked", "incomplete"]
    model: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    checks: dict[str, CheckStatus]
    schema_tokens: int = Field(ge=0)
    evidence: dict[str, Any] = Field(default_factory=dict)

    def passes(self, required_checks: Iterable[str]) -> bool:
        """全部检查（恰好是这一组）passed、schema 在预算内、证据里的探针版本与报告版本一致。"""

        required = tuple(required_checks)
        return (
            self.status == "compatible"
            and set(self.checks) == set(required)
            and all(self.checks[name] == "passed" for name in required)
            and self.schema_tokens < MAX_TOOL_SCHEMA_TOKENS
            and self.evidence.get("probe_version") == self.version
        )

    def matches(self, *, endpoint: str, model: str) -> bool:
        """报告是否针对这一个端点 + 模型（端点按规范化后的 base_url 比较）。"""

        return (
            normalize_base_url(self.endpoint) == normalize_base_url(endpoint)
            and self.model == model
        )


def read_report[ReportT: CompatibilityReport](
    path: str | Path, model_type: type[ReportT]
) -> ReportT | None:
    """Read evidence without printing its contents or treating bad data as proof."""

    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        return model_type.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return None


def write_report(path: str | Path, report: CompatibilityReport) -> Path:
    """原子写出报告（同目录临时文件 + ``os.replace``，0600）。报告里没有密钥，但它决定能否装配。"""

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(
            descriptor,
            (
                json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8"),
        )
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    return target


def compatibility_report_path(getenv: Getenv = os.getenv) -> Path:
    """远程端点兼容性报告的位置：``WATER_AGENT_LLM_COMPATIBILITY_REPORT`` > 凭据卷里面板写的那份。"""

    raw = (getenv(COMPATIBILITY_REPORT_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    from water_agent.config.credentials import credentials_dir

    return credentials_dir() / PANEL_REPORT_FILE


@dataclass(frozen=True, slots=True)
class EndpointVerification:
    status: str
    reason: str

    @property
    def usable(self) -> bool:
        return self.status in {VERIFICATION_DEFAULT_EXEMPT, VERIFICATION_VERIFIED}


def pair_report_path(base_url: str, model: str) -> Path:
    """某个 (端点, 模型) 的逐对报告位置：凭据卷下 ``llm_compatibility_reports/<sha256(端点|模型)[:16]>.json``。"""

    from water_agent.config.credentials import credentials_dir

    digest = hashlib.sha256(f"{normalize_base_url(base_url)}|{model}".encode()).hexdigest()[:16]
    return credentials_dir() / PAIR_REPORT_DIR / f"{digest}.json"


def _judge(report: CompatibilityReport, endpoint: ChatEndpoint) -> EndpointVerification:
    """一份报告对这个端点 + 模型是否构成证据（精确匹配 + 探针版本 + 全部 passed）。"""

    if not report.matches(endpoint=endpoint.base_url, model=endpoint.model):
        return EndpointVerification(
            VERIFICATION_UNVERIFIED, "compatibility report is for another endpoint or model"
        )
    if report.version != ENDPOINT_PROBE_VERSION:
        return EndpointVerification(
            VERIFICATION_UNVERIFIED, "compatibility report was produced by an outdated probe"
        )
    if not report.passes(ENDPOINT_COMPATIBILITY_CHECKS):
        return EndpointVerification(VERIFICATION_UNVERIFIED, "not every compatibility check passed")
    return EndpointVerification(VERIFICATION_VERIFIED, "compatibility report passed")


def endpoint_verification(
    endpoint: ChatEndpoint, *, getenv: Getenv = os.getenv
) -> EndpointVerification:
    """这个端点有没有可用的兼容性证据。缺省端点免检；本地模型路径沿用 P5 Step 46 自己的门槛。

    证据来源两处，任一处给出可用报告即放行：**逐对报告**（P7-S15，目录里的模型各自一份）与**原单文件**
    （``WATER_AGENT_LLM_COMPATIBILITY_REPORT`` / 面板单文件，全局模型用）。都不可用时优先说明"针对这一对的
    那份报告"为什么不行。
    """

    if endpoint.source == SOURCE_LOCAL_MODEL:
        from water_agent.generation.local_model import local_model_ready

        if local_model_ready(getenv):
            return EndpointVerification(
                VERIFICATION_VERIFIED, "local model compatibility report passed"
            )
        return EndpointVerification(
            VERIFICATION_UNVERIFIED, "local model lacks complete compatibility evidence"
        )
    if endpoint.is_default:
        return EndpointVerification(VERIFICATION_DEFAULT_EXEMPT, "default DeepSeek endpoint")
    pair_report = read_report(pair_report_path(endpoint.base_url, endpoint.model), CompatibilityReport)
    pair_verdict = _judge(pair_report, endpoint) if pair_report is not None else None
    if pair_verdict is not None and pair_verdict.usable:
        return pair_verdict
    single_report = read_report(compatibility_report_path(getenv), CompatibilityReport)
    single_verdict = (
        _judge(single_report, endpoint)
        if single_report is not None
        else EndpointVerification(VERIFICATION_UNVERIFIED, "no compatibility report")
    )
    if single_verdict.usable:
        return single_verdict
    return pair_verdict or single_verdict


class EndpointNotVerifiedError(ValueError):
    """非缺省对话端点没有通过的兼容性报告：装配期拒绝。"""


PROBE_HINT = "运行 `python -m water_agent.harness.endpoint_compat --out <报告路径>` 生成报告"


def require_verified_endpoint(endpoint: ChatEndpoint, *, getenv: Getenv = os.getenv) -> None:
    """非缺省端点没有可用的兼容性证据即抛 :class:`EndpointNotVerifiedError`（不含任何 Key 值）。"""

    verification = endpoint_verification(endpoint, getenv=getenv)
    if verification.usable:
        return
    raise EndpointNotVerifiedError(
        f"对话端点 {endpoint.host} / 模型 {endpoint.model} 未通过兼容性检查（{verification.reason}）；"
        f"{PROBE_HINT}，并用 {COMPATIBILITY_REPORT_ENV} 指向它（或在设置面板运行兼容性检查）"
    )


__all__ = [
    "COMPATIBILITY_REPORT_ENV",
    "ENDPOINT_COMPATIBILITY_CHECKS",
    "ENDPOINT_PROBE_VERSION",
    "MAX_TOOL_SCHEMA_TOKENS",
    "PAIR_REPORT_DIR",
    "PANEL_REPORT_FILE",
    "PROBE_HINT",
    "VERIFICATION_DEFAULT_EXEMPT",
    "VERIFICATION_UNVERIFIED",
    "VERIFICATION_VERIFIED",
    "CheckStatus",
    "CompatibilityReport",
    "EndpointNotVerifiedError",
    "EndpointVerification",
    "compatibility_report_path",
    "endpoint_verification",
    "pair_report_path",
    "read_report",
    "require_verified_endpoint",
    "write_report",
]

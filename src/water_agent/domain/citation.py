"""引用声明模型与确定性引用校验器（Step 5，Step 8 接入版本注册表）。

把引用从"系统代填"改为"模型声明 + 程序化校验"：模型经由 `cite_clauses` 工具声明
引用（写入 state 的 `citations: list[CitationClaim]`），再在任何最终输出前用
`validate_citations` 做集合级确定性校验。校验器是纯函数、离线可跑，遵循三条铁律
中的"引用可追溯 / 禁止编造条款"。

设计要点：
- ``CitationClaim`` 是 state 层统一的引用结构（legacy 转换 / agentic 声明共用）；
- ``validate_citations`` 只比较 source_id 集合（不改评测契约）；
- 失败时 ``errors`` 输出给模型看的可行动文本（did-you-mean 风格），供 Step 6
  回流自纠；
- 版本状态（Step 8）：传入 ``version_registry`` 时为每条引用解析 ``version_status``，
  无法确认现行的进 ``missing_version`` + ``warnings``。**只标记不判废止、不判失败**，
  也不进 ``errors``——版本未知不是模型重试能纠正的事，属于给人看的提示，不是回流信号。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CitationClaim(BaseModel):
    """模型声明的引用：source_id / spec_version / version_status / declared_by / message_id。"""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=3, description="规范条款 source_id，如 GB50015-2019 5.4.2")
    spec_version: str | None = Field(default=None, description="规范版本，如 2019（由 source_id 解析）")
    version_status: str | None = Field(
        default=None,
        description="版本注册表状态：current/superseded/deprecated/unknown；未校验为 None",
    )
    declared_by: str = Field(default="model", description="声明来源：legacy 转换用 system，cite 工具用 model")
    message_id: str = Field(default="", description="声明所在消息 id（agentic 循环使用）")

    @property
    def spec_id(self) -> str:
        """从 source_id 提取规范号主体（空格前的部分，含版本后缀），供版本注册表查询。"""
        return _spec_id_of(self.source_id)


@dataclass(frozen=True)
class CitationCheckResult:
    """校验结果：是否通过 + 各类问题清单 + 给模型的可行动错误文本 + 给人的版本提示。"""

    ok: bool
    invalid: list[str] = field(default_factory=list)          # 不在本次检索结果的引用
    missing_version: list[str] = field(default_factory=list)  # 无法确认现行有效的引用
    errors: list[str] = field(default_factory=list)           # 供模型自纠的可行动文本（仅 invalid）
    warnings: list[str] = field(default_factory=list)         # 版本无法确认的提示（不回流、不判失败）
    version_status: dict[str, str] = field(default_factory=dict)  # cid → current/unknown/...


def _clean(ids: Iterable[str]) -> list[str]:
    """去掉空串与重复，保留出现顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in ids:
        cleaned = (item or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def validate_citations(
    cited_ids: Iterable[str],
    context_ids: Iterable[str],
    *,
    version_registry: Any | None = None,
) -> CitationCheckResult:
    """确定性校验：被引用条款是否都在本次检索结果中。

    - ``cited_ids`` / ``context_ids``：source_id 字符串集合（主键比较）；
    - ``version_registry``：Step 8 版本注册表（需有 ``get_spec_status``）。传入时为每条
      引用解析 ``version_status``；无法确认现行有效的进 ``missing_version`` 与 ``warnings``，
      不判失败、不判废止、不进 ``errors``。
    """
    cited = _clean(cited_ids)
    context_set = set(_clean(context_ids))

    invalid: list[str] = []
    errors: list[str] = []
    candidates = _sorted_suggestions(cited, context_set)
    for cid in cited:
        if cid not in context_set:
            invalid.append(cid)
            suggestions = _suggestions(cid, context_set)
            if suggestions:
                errors.append(
                    f"引用 {cid!r} 不在本次检索结果中；did-you-mean 建议：{suggestions}。"
                )
            else:
                errors.append(
                    f"引用 {cid!r} 不在本次检索结果中；本次可用 source_id：{candidates}。"
                )

    missing_version: list[str] = []
    warnings: list[str] = []
    version_status: dict[str, str] = {}
    if version_registry is not None:
        for cid in cited:
            status = _resolve_version(version_registry, cid)
            version_status[cid] = status
            if status == "unknown":
                missing_version.append(cid)
                warnings.append(
                    f"引用 {cid!r} 的规范版本无法确认当前现行有效（注册表未录入），请核对版本或说明依据。"
                )

    return CitationCheckResult(
        ok=not invalid,
        invalid=invalid,
        missing_version=missing_version,
        errors=errors,
        warnings=warnings,
        version_status=version_status,
    )


def enrich_claims(
    claims: Iterable[CitationClaim],
    check: CitationCheckResult,
) -> list[CitationClaim]:
    """把校验结果里的版本信息写回 CitationClaim（spec_version 解析自 source_id）。"""
    out: list[CitationClaim] = []
    for claim in claims:
        status = check.version_status.get(claim.source_id.strip())
        out.append(
            claim.model_copy(
                update={
                    "spec_version": claim.spec_version or _version_of(claim.source_id),
                    "version_status": status if status is not None else claim.version_status,
                }
            )
        )
    return out


def _sorted_suggestions(cited: list[str], context_set: set[str]) -> list[str]:
    """本次检索可用的候选 source_id（排序后展示），供无效引用的自纠参考。"""
    return sorted(context_set - set(cited))


def _suggestions(cid: str, context_set: set[str], n: int = 3, cutoff: float = 0.6) -> list[str]:
    """用 get_close_matches 找与无效引用相近的候选项（did-you-mean 语义）。"""
    if not context_set:
        return []
    return list(get_close_matches(cid, context_set, n=n, cutoff=cutoff))


def _resolve_version(registry: Any, cid: str) -> str:
    """向版本注册表查询现行状态，归一为字符串；查不到/异常/形态不对都按 unknown（仅标记）。"""
    if not hasattr(registry, "get_spec_status"):
        return "unknown"
    try:
        status = registry.get_spec_status(_spec_id_of(cid))
    except Exception:  # noqa: BLE001 — 注册表异常不抛给调用方，统一按 unknown 标记
        return "unknown"
    if isinstance(status, dict):
        value = status.get("status")
    else:
        value = status
    return str(value) if value else "unknown"


def _spec_id_of(cid: str) -> str:
    """从 source_id 提取规范主体（保留版本后缀）："GB50015-2019 5.4.2" -> "GB50015-2019"。"""
    return cid.split(" ", 1)[0] if " " in cid else cid


def _version_of(cid: str) -> str | None:
    """从 source_id 解析版本号："GB50015-2019 5.4.2" -> "2019"；无版本后缀返回 None。"""
    spec = _spec_id_of(cid)
    if "-" in spec:
        _, _, tail = spec.rpartition("-")
        if tail.isdigit():
            return tail
    return None

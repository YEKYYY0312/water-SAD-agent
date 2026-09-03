"""引用声明模型与确定性引用校验器（Step 5）。

把引用从"系统代填"改为"模型声明 + 程序化校验"：模型经由 `cite_clauses` 工具声明
引用（写入 state 的 `citations: list[CitationClaim]`），再在任何最终输出前用
`validate_citations` 做集合级确定性校验。校验器是纯函数、离线可跑，遵循三条铁律
中的"引用可追溯 / 禁止编造条款"。

设计要点：
- ``CitationClaim`` 是 state 层统一的引用结构（legacy 转换 / agentic 声明共用）；
- ``validate_citations`` 只比较 source_id 集合（本步不改评测契约）；
- 失败时 ``errors`` 输出给模型看的可行动文本（did-you-mean 风格），供 Step 6
  回流自纠；``missing_version`` 仅标记，不判废止（Step 8 才接版本注册表）。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CitationClaim(BaseModel):
    """模型声明的引用：source_id / spec_version / declared_by / message_id。"""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=3, description="规范条款 source_id，如 GB50015-2019 5.4.2")
    spec_version: str | None = Field(default=None, description="规范版本，如 2019；本步不判废止")
    declared_by: str = Field(default="model", description="声明来源：legacy 转换用 system，cite 工具用 model")
    message_id: str = Field(default="", description="声明所在消息 id（agentic 循环使用）")

    @property
    def spec_id(self) -> str:
        """从 source_id 提取规范号主体（空格前的部分），供版本注册表查询。"""
        return self.source_id.split(" ", 1)[0] if " " in self.source_id else self.source_id


@dataclass(frozen=True)
class CitationCheckResult:
    """校验结果：是否通过 + 各类问题清单 + 给模型的可行动错误文本。"""

    ok: bool
    invalid: list[str] = field(default_factory=list)          # 不在本次检索结果的引用
    missing_version: list[str] = field(default_factory=list)  # 无法确认现行有效的引用
    errors: list[str] = field(default_factory=list)           # 供模型自纠的可行动文本


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

    - ``cited_ids`` / ``context_ids``：source_id 字符串集合（本步只做主键比较）；
    - ``version_registry``：Step 8 的版本注册表；本步传入时仅将无法确认现行有效的
      引用标记进 ``missing_version``，不判失败、不判废止。
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
    if version_registry is not None:
        for cid in cited:
            status = _resolve_version(version_registry, cid)
            if _is_unknown(status):
                missing_version.append(cid)
                errors.append(
                    f"引用 {cid!r} 的规范版本无法确认当前现行有效，请核对版本或说明依据。"
                )

    return CitationCheckResult(
        ok=not invalid,
        invalid=invalid,
        missing_version=missing_version,
        errors=errors,
    )


def _sorted_suggestions(cited: list[str], context_set: set[str]) -> list[str]:
    """本次检索可用的候选 source_id（排序后展示），供无效引用的自纠参考。"""
    return sorted(context_set - set(cited))


def _suggestions(cid: str, context_set: set[str], n: int = 3, cutoff: float = 0.6) -> list[str]:
    """用 get_close_matches 找与无效引用相近的候选项（did-you-mean 语义）。"""
    if not context_set:
        return []
    return list(get_close_matches(cid, context_set, n=n, cutoff=cutoff))


def _resolve_version(registry: Any, cid: str) -> Any:
    """向版本注册表查询现行状态；查不到或异常都视为 unknown（仅标记）。

    优先从 source_id 提取完整规范 ID（含版本），如 "GB50015-2019 5.4.2" -> "GB50015-2019"。
    """
    if not hasattr(registry, "get_spec_status"):
        return None
    try:
        # 从 source_id 提取规范 ID（可能含版本后缀）
        spec_id_with_version = _spec_id_of(cid)
        return registry.get_spec_status(spec_id_with_version)
    except Exception:  # noqa: BLE001 — 注册表异常不抛给调用方，统一按 unknown 标记
        return None


def _is_unknown(status: Any) -> bool:
    """判断注册表返回状态是否为 unknown（缺查询/键值 unknown/缺失均视为 unknown）。"""
    if status is None:
        return True
    if isinstance(status, dict):
        return bool(status.get("status", "unknown") in (None, "unknown", "") or not status)
    return bool(status == "unknown")


def _spec_id_of(cid: str) -> str:
    """从 source_id 提取规范主体（保留版本后缀）。

    例如：
        "GB50015-2019 5.4.2" -> "GB50015-2019"
    """
    return cid.split(" ", 1)[0] if " " in cid else cid

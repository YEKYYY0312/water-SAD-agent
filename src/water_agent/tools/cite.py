"""工具：cite_clauses（引用声明，Step 5）。

模型用条款 source_id（如 ``GB50015-2019 5.4.2``）显式声明引用。**语义**：本工具只是
声明载体，真正判分在 ``validate_citations`` 校验器——因此工具对格式做基础防护
（非空、含规范号+条款号），但不做任何"是否在检索结果中"的判断。

可选 ``claims_sink``（list[CitationClaim]）用于把声明写入会话状态；Step 6 的
agentic 循环用它把 ``declared_by="model"`` 的声明落地到 state，再统一消费。

**引用可追溯性校验**（Step 8，2026-09-16 新增）：可选传入 ``available_source_ids``
（检索上下文中的条款号集合）。当提供时，工具将拒绝引用不在该集合中的条款号，
并在错误消息中列出可用的条款号，引导模型仅使用检索到的条文。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from water_agent.domain.citation import CitationClaim
from water_agent.tools.base import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable


class CiteArgs(BaseModel):
    """引用声明输入：一组条款 source_id。"""

    model_config = ConfigDict(extra="forbid")

    article_ids: list[str] = Field(
        min_length=1,
        max_length=20,
        description="要声明的条款 source_id 列表，如 ['GB50015-2019 5.4.2']",
    )

    @field_validator("article_ids")
    @classmethod
    def _clean_and_shape_check(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            item = (raw or "").strip()
            if " " not in item:
                raise ValueError(f"条款 id {item!r} 应为 '规范号 条款号' 形式，如 'GB50015-2019 5.4.2'")
            if item in cleaned:
                continue
            cleaned.append(item)
        return cleaned


def _make_execute(
    claims_sink: list[CitationClaim] | None = None,
    available_source_ids: set[str] | None = None,
    enable_traceability_check: bool = False,
):
    """返回 execute 闭包，把 sink 和可用条款号集合捕获进来。

    Args:
        claims_sink: 可选的声明输出列表
        available_source_ids: 可选的检索上下文条款号集合；提供时进行可追溯性硬校验
        enable_traceability_check: 是否启用可追溯性检查（需要配合 LangGraph 状态使用）
    """
    def execute(args: CiteArgs) -> dict:
        # 可追溯性硬校验：如果提供了 available_source_ids，拒绝不在其中的引用
        if available_source_ids is not None and len(available_source_ids) > 0:
            invalid_ids = [aid for aid in args.article_ids if aid not in available_source_ids]
            if invalid_ids:
                # 列出前 10 个可用条款号作为提示
                available_sample = sorted(available_source_ids)[:10]
                available_preview = ", ".join(available_sample)
                if len(available_source_ids) > 10:
                    available_preview += f" ... (共 {len(available_source_ids)} 个可用条款)"

                error_msg = (
                    f"引用可追溯性校验失败：以下条款号不在检索上下文中：{', '.join(invalid_ids)}。\n\n"
                    f"⚠️ 只能引用检索到的条款，禁止从记忆中补充未检索到的条款号。\n\n"
                    f"可用的条款号：{available_preview}"
                )
                return {
                    "cited": [],
                    "registered": False,
                    "error": error_msg,
                    "invalid_ids": invalid_ids,
                    "available_count": len(available_source_ids),
                }

        # 通过校验或未启用校验，正常注册引用
        claims = [
            CitationClaim(source_id=cid, declared_by="model")
            for cid in args.article_ids
        ]
        if claims_sink is not None:
            claims_sink.extend(claims)
        return {
            "cited": [claim.source_id for claim in claims],
            "registered": True,
            "claims": [claim.model_dump(mode="json") for claim in claims],
        }

    return execute


def build_cite_clauses_tool(
    claims_sink: list[CitationClaim] | None = None,
    available_source_ids: set[str] | None = None,
) -> ToolSpec:
    """构建 cite_clauses 工具；传入 ``claims_sink`` 时把声明写入该列表供上层消费。

    Args:
        claims_sink: 可选的声明输出列表
        available_source_ids: 可选的检索上下文条款号集合；提供时进行可追溯性硬校验
    """
    return ToolSpec(
        name="cite_clauses",
        description=(
            "声明回答将要引用的规范条款，返回被登记的 source_id 列表与声明详情。"
            "只能引用检索上下文中已返回的条款号，不可从记忆补充。"
            "Use when 需要显式登记某条规范条文作为回答引用、或把引用写入会话状态。"
            "Do not use for 检索内容（应调用 retrieve_clauses）。"
        ),
        args_model=CiteArgs,
        execute=_make_execute(claims_sink, available_source_ids),
    )


def build_cite_clauses_tool_with_context_check(
    claims_sink: list[CitationClaim] | None = None,
) -> tuple[ToolSpec, Callable]:
    """构建带可追溯性检查的 cite_clauses 工具，返回 (ToolSpec, context_updater)。

    由于检索上下文在运行时才产生，本函数返回：
    1. ToolSpec：工具规范，execute 会从闭包中读取最新的 available_ids
    2. context_updater：调用方在检索后调用此函数更新可用条款号

    使用示例：
        tool_spec, update_context = build_cite_clauses_tool_with_context_check(claims_sink)
        # 注册 tool_spec 到 registry
        # 在 retrieve_clauses 返回后：
        update_context(retrieved_source_ids)

    Args:
        claims_sink: 可选的声明输出列表

    Returns:
        (ToolSpec, context_updater 函数)
    """
    # 使用闭包共享可变状态
    context_holder: dict[str, set[str]] = {"available_ids": set()}

    def update_context(source_ids: set[str] | list[str]):
        """更新可用的检索上下文条款号（累积模式）"""
        if isinstance(source_ids, list):
            context_holder["available_ids"].update(source_ids)
        else:
            context_holder["available_ids"].update(source_ids)

    def execute_with_check(args: CiteArgs) -> dict:
        """执行引用声明，带可追溯性检查"""
        available_ids = context_holder["available_ids"]

        # 如果有检索上下文，进行校验
        if available_ids:
            invalid_ids = [aid for aid in args.article_ids if aid not in available_ids]
            if invalid_ids:
                available_sample = sorted(available_ids)[:10]
                available_preview = ", ".join(available_sample)
                if len(available_ids) > 10:
                    available_preview += f" ... (共 {len(available_ids)} 个可用条款)"

                error_msg = (
                    f"引用可追溯性校验失败：以下条款号不在检索上下文中：{', '.join(invalid_ids)}。\n\n"
                    f"⚠️ 只能引用检索到的条款，禁止从记忆中补充未检索到的条款号。\n\n"
                    f"可用的条款号：{available_preview}"
                )
                # 返回结构化错误，包含详情供模型纠正
                return {
                    "cited": [],
                    "registered": False,
                    "error": error_msg,
                    "invalid_ids": invalid_ids,
                    "available_count": len(available_ids),
                }

        # 通过校验，正常注册引用
        claims = [
            CitationClaim(source_id=cid, declared_by="model")
            for cid in args.article_ids
        ]
        if claims_sink is not None:
            claims_sink.extend(claims)
        return {
            "cited": [claim.source_id for claim in claims],
            "registered": True,
            "claims": [claim.model_dump(mode="json") for claim in claims],
        }

    tool_spec = ToolSpec(
        name="cite_clauses",
        description=(
            "声明回答将要引用的规范条款，返回被登记的 source_id 列表与声明详情。"
            "只能引用检索上下文中已返回的条款号，不可从记忆补充。"
            "Use when 需要显式登记某条规范条文作为回答引用、或把引用写入会话状态。"
            "Do not use for 检索内容（应调用 retrieve_clauses）。"
        ),
        args_model=CiteArgs,
        execute=execute_with_check,
    )

    return tool_spec, update_context


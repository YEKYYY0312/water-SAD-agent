"""工具：cite_clauses（引用声明，Step 5）。

模型用条款 source_id（如 ``GB50015-2019 5.4.2``）显式声明引用。**语义**：本工具只是
声明载体，真正判分在 ``validate_citations`` 校验器——因此工具对格式做基础防护
（非空、含规范号+条款号），但不做任何"是否在检索结果中"的判断。

可选 ``claims_sink``（list[CitationClaim]）用于把声明写入会话状态；Step 6 的
agentic 循环用它把 ``declared_by="model"`` 的声明落地到 state，再统一消费。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from water_agent.domain.citation import CitationClaim
from water_agent.tools.base import ToolSpec


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


def _make_execute(claims_sink: list[CitationClaim] | None = None):
    def execute(args: CiteArgs) -> dict:
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


def build_cite_clauses_tool(claims_sink: list[CitationClaim] | None = None) -> ToolSpec:
    """构建 cite_clauses 工具；传入 ``claims_sink`` 时把声明写入该列表供上层消费。"""
    return ToolSpec(
        name="cite_clauses",
        description=(
            "声明回答将要引用的规范条款，返回被登记的 source_id 列表与声明详情。"
            "Use when 需要显式登记某条规范条文作为回答引用、或把引用写入会话状态。"
            "Do not use for 检索规范条文内容（应调用 retrieve_clauses），本工具不核对"
            "引用是否真实存在于检索结果——那是校验器的职责。"
        ),
        args_model=CiteArgs,
        execute=_make_execute(claims_sink),
    )
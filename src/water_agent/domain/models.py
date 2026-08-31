"""结构化领域模型。"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


def make_source_id(spec_id: str, article: str) -> str:
    """两级索引的引用标识：`{spec_id} {article}`。"""
    return f"{spec_id} {article}".strip()


def content_hash_of(text: str) -> str:
    """稳定内容哈希：sha256(text) 前 16 位。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def stable_chunk_id(
    spec_id: str,
    chapter: str | None,
    section: str | None,
    article: str | None,
    chunk_type: str,
    content_hash: str,
) -> str:
    """稳定 chunk_id：对稳定字段 + content_hash 做 sha256，取前 16 位。"""
    raw = "|".join(
        [
            spec_id or "",
            chapter or "",
            section or "",
            article or "",
            chunk_type or "",
            content_hash or "",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class RetrievedClause(BaseModel):
    """检索到的规范条款（两级索引：条 + 节/章上下文）。"""

    spec_id: str
    article: str
    chapter: str | None = None
    section: str | None = None
    title: str = ""
    text: str
    score: float | None = None

    @property
    def source_id(self) -> str:
        return make_source_id(self.spec_id, self.article)


class Citation(BaseModel):
    """可追溯引用：规范号 + 条款号。"""

    spec_id: str
    article: str
    title: str = ""
    text_snippet: str = ""

    @property
    def source_id(self) -> str:
        return make_source_id(self.spec_id, self.article)


class GeneratedAnswer(BaseModel):
    """生成器输出：答文 + 引用列表。"""

    answer: str
    citations: list[Citation] = Field(default_factory=list)
    intent: str = "rag"


class TraceEvent(BaseModel):
    """观测事件（本地 JSON Trace 的单元）。"""

    run_id: str
    trace_id: str
    node: str
    event: str
    ts: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class EvalCase(BaseModel):
    """金标准评测用例。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str = "rag"
    query: str
    gold_articles_any: list[str] = Field(default_factory=list)
    gold_articles_all: list[str] = Field(default_factory=list)
    forbidden_articles: list[str] = Field(default_factory=list)
    expected_status: str = "succeeded"
    eval: str = "citation_exists"
    variant: str = "mock"
    top_k: int = 5


class CalcTolerances(BaseModel):
    """数值判分双阈值：相对误差 + 绝对误差。"""

    model_config = ConfigDict(extra="forbid")

    relative: float = 0.0
    absolute: float = 0.0


class CalcCase(BaseModel):
    """水力计算金标准用例（type == "calc"）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str = "calc"
    query: str = ""
    formula: str
    input: dict[str, Any]
    gold: dict[str, float]
    tolerances: CalcTolerances = Field(default_factory=CalcTolerances)
    expected_status: str = "succeeded"
    expected_assumptions: list[str] = Field(default_factory=list)
    variant: str = "programmatic"
    spec_ref: str = ""


class EvalResult(BaseModel):
    """单条评测结果。"""

    case_id: str
    passed: bool
    expected_status: str
    actual_status: str
    expected_articles: list[str] = Field(default_factory=list)
    actual_citations: list[str] = Field(default_factory=list)
    failure_reason: str = ""
    latency_ms: float = 0.0
    trace_id: str = ""
    retry_count: int = 0
    gold_articles_any_hit: list[str] = Field(default_factory=list)
    gold_articles_all_hit: list[str] = Field(default_factory=list)
    forbidden_articles_hit: list[str] = Field(default_factory=list)
    load_error: str = ""
    line_no: int | None = None
    context_articles: list[str] = Field(default_factory=list)


class EvalReport(BaseModel):
    """评测汇总报告。"""

    total: int
    passed: int
    failed: int
    pass_rate: float
    cases: list[EvalResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    input_path: str = ""
    output_path: str = ""


class ChunkType(str, Enum):
    """切块类型。"""

    chapter = "chapter"
    section = "section"
    subsection = "subsection"
    article = "article"
    clause = "clause"
    item = "item"
    table = "table"
    paragraph = "paragraph"
    text_window = "text_window"


PARENT_TYPES: frozenset[str] = frozenset(
    {ChunkType.chapter.value, ChunkType.section.value, ChunkType.subsection.value}
)
CHILD_TYPES: frozenset[str] = frozenset(
    {ChunkType.article.value, ChunkType.clause.value, ChunkType.item.value, ChunkType.table.value}
)


class Chunk(BaseModel):
    """结构化父子级切块单元。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    parent_id: str | None = None
    chunk_type: str
    spec_id: str
    chapter: str | None = None
    section: str | None = None
    article: str | None = None
    title: str = ""
    text: str
    source_path: str | None = None
    page: int | None = None
    content_hash: str

    @property
    def source_id(self) -> str:
        if self.article:
            return make_source_id(self.spec_id, self.article)
        return self.spec_id

    @property
    def is_parent(self) -> bool:
        return self.chunk_type in PARENT_TYPES

    @property
    def is_child(self) -> bool:
        return self.chunk_type in CHILD_TYPES or self.chunk_type in {
            ChunkType.paragraph.value,
            ChunkType.text_window.value,
        }

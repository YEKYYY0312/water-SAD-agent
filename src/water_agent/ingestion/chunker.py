"""结构化父子级切块：章/节 → 条/款/项/表格，递归兜底不跨条。

策略
-----
1. 能识别章/节/条款：结构化父子切块（父块=章/节/小节，子块=条/款/项/表）
2. 条款过长：按段落递归拆分，保留原 article 和 parent_id
3. 无条号但有标题：按标题层级建父子
4. 无结构：按段落递归
5. 段落仍过长：token 窗口 + overlap
6. 表格：单独表格块

约束
-----
- 子块不跨两条；父块不跨无关章节；同一 article 的子块共享逻辑引用
- chunk_id 稳定：sha256(spec_id|chapter|section|article|chunk_type|content_hash)[:16]
- 表格保留结构化内容
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from water_agent.domain.models import Chunk, ChunkType, content_hash_of, stable_chunk_id

# 条号：如 4.4.2 / 5.2.1-3
ARTICLE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,3})\b")
# 节：如 4.4
SECTION_RE = re.compile(r"^\s*(\d+\.\d+)\b")
# 表格标题
TABLE_TITLE_RE = re.compile(r"^\s*表\s*\d+[\.\-]\d+")
# Markdown 表格行
MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*[-:]+\s*\|")


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    cn = len([c for c in text if "\u4e00" <= c <= "\u9fff"])
    en_words = len(text.split())
    return max(cn, en_words)


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text.strip())
    if len(parts) == 1:
        parts = [p.strip() for p in text.split("\n") if p.strip()]
    return [p.strip() for p in parts if p.strip()]


def _split_by_token_window(text: str, window: int, overlap: int) -> list[str]:
    if _approx_tokens(text) <= window:
        return [text]
    chars = list(text)
    out: list[str] = []
    char_window = int(window * 1.2)
    char_overlap = int(overlap * 1.2)
    char_step = max(1, char_window - char_overlap)
    for start in range(0, len(chars), char_step):
        chunk = "".join(chars[start : start + char_window]).strip()
        if chunk:
            out.append(chunk)
        if start + char_window >= len(chars):
            break
    return out


@dataclass(slots=True)
class ChunkConfig:
    """切块配置。"""

    target_tokens: int = 600
    overlap_tokens: int = 80
    article_max_tokens: int = 800
    table_as_single: bool = True


@dataclass(slots=True)
class RawClause:
    spec_id: str
    chapter: str | None
    section: str | None
    article: str | None
    title: str
    text: str
    source_path: str | None = None
    page: int | None = None


def _extract_tables(text: str) -> tuple[str, list[str]]:
    """从文本中抽取 Markdown 表格，返回 (剩余文本, 表格块列表)。"""
    lines = text.split("\n")
    tables: list[str] = []
    remaining: list[str] = []
    buf: list[str] = []
    in_table = False
    for line in lines:
        if MD_TABLE_ROW_RE.match(line) or MD_TABLE_SEP_RE.match(line):
            buf.append(line)
            in_table = True
        else:
            if in_table and buf:
                tables.append("\n".join(buf).strip())
                buf = []
                in_table = False
            remaining.append(line)
    if buf:
        tables.append("\n".join(buf).strip())
    return "\n".join(remaining).strip(), tables


def _make_chunk(
    chunk_type: str,
    spec_id: str,
    chapter: str | None,
    section: str | None,
    article: str | None,
    title: str,
    text: str,
    source_path: str | None,
    page: int | None,
    parent_id: str | None,
) -> Chunk:
    ch = content_hash_of(text)
    cid = stable_chunk_id(spec_id, chapter, section, article, chunk_type, ch)
    return Chunk(
        chunk_id=cid,
        parent_id=parent_id,
        chunk_type=chunk_type,
        spec_id=spec_id,
        chapter=chapter,
        section=section,
        article=article,
        title=title,
        text=text,
        source_path=source_path,
        page=page,
        content_hash=ch,
    )


def _build_title_parent(clause: RawClause) -> Chunk | None:
    """标题型无条号文本的稳定父块。

    该父块用于承接 title 非空但 article 为空的标题层级，不能跨无关标题。
    父块 text 取标题；以正文 content_hash 作为 section 区分符，
    保证"同标题不同正文"不会合并到同一个父块（父块不跨无关内容）。
    """
    if clause.article is not None or not clause.title.strip():
        return None
    title = clause.title.strip()
    chapter = clause.chapter or content_hash_of(title)[:8]
    # section 用标题 + 正文哈希 区分，保证同标题不同正文不合并
    section = clause.section or content_hash_of(f"{title}|{clause.text}")
    parent_text = title
    return _make_chunk(
        ChunkType.section.value,
        clause.spec_id,
        chapter,
        section,
        None,
        title,
        parent_text,
        clause.source_path,
        clause.page,
        None,
    )


def _build_parents(clause: RawClause) -> tuple[Chunk | None, Chunk | None, Chunk | None]:
    """构建章/节/小节父块（幂等，chunk_id 稳定）。"""
    chapter_chunk: Chunk | None = None
    section_chunk: Chunk | None = None
    subsection_chunk: Chunk | None = None
    if clause.chapter:
        chapter_chunk = _make_chunk(
            ChunkType.chapter.value,
            clause.spec_id,
            clause.chapter,
            None,
            None,
            f"第{clause.chapter}章",
            f"第{clause.chapter}章",
            clause.source_path,
            clause.page,
            None,
        )
    if clause.section:
        parent = chapter_chunk.chunk_id if chapter_chunk else None
        section_chunk = _make_chunk(
            ChunkType.section.value,
            clause.spec_id,
            clause.chapter,
            clause.section,
            None,
            clause.section,
            clause.section,
            clause.source_path,
            clause.page,
            parent,
        )
        if clause.section.count(".") >= 2:
            subsection_chunk = section_chunk
            section_chunk = None
    return chapter_chunk, section_chunk, subsection_chunk


class Chunker:
    """结构化父子切块器。"""

    def __init__(self, config: ChunkConfig | None = None) -> None:
        self.config = config or ChunkConfig()

    def chunk_clause(self, clause: RawClause) -> list[Chunk]:
        """对单条 RawClause 做父子切块，返回含父块与子块的列表。"""
        out: list[Chunk] = []
        seen_parent: dict[str, Chunk] = {}

        remaining_text, tables = _extract_tables(clause.text)

        chapter_chunk, section_chunk, subsection_chunk = _build_parents(clause)
        title_parent = _build_title_parent(clause)
        for parent in (chapter_chunk, section_chunk, subsection_chunk, title_parent):
            if parent and parent.chunk_id not in seen_parent:
                out.append(parent)
                seen_parent[parent.chunk_id] = parent

        direct_parent_id: str | None = None
        if title_parent is not None:
            direct_parent_id = title_parent.chunk_id
        elif subsection_chunk:
            direct_parent_id = subsection_chunk.chunk_id
        elif section_chunk:
            direct_parent_id = section_chunk.chunk_id
        elif chapter_chunk:
            direct_parent_id = chapter_chunk.chunk_id

        if not remaining_text.strip() and not tables:
            return out

        for table_text in tables:
            title = clause.title if TABLE_TITLE_RE.match(clause.title) else "表格"
            out.append(
                _make_chunk(
                    ChunkType.table.value,
                    clause.spec_id,
                    clause.chapter,
                    clause.section,
                    clause.article,
                    title,
                    table_text,
                    clause.source_path,
                    clause.page,
                    direct_parent_id,
                )
            )

        if clause.article:
            if not remaining_text.strip():
                return out
            if _approx_tokens(remaining_text) <= self.config.article_max_tokens:
                out.append(
                    _make_chunk(
                        ChunkType.article.value,
                        clause.spec_id,
                        clause.chapter,
                        clause.section,
                        clause.article,
                        clause.title,
                        remaining_text,
                        clause.source_path,
                        clause.page,
                        direct_parent_id,
                    )
                )
            else:
                paragraphs = _split_paragraphs(remaining_text)
                if not paragraphs:
                    paragraphs = [remaining_text]
                for para in paragraphs:
                    if _approx_tokens(para) <= self.config.article_max_tokens:
                        out.append(
                            _make_chunk(
                                ChunkType.article.value,
                                clause.spec_id,
                                clause.chapter,
                                clause.section,
                                clause.article,
                                clause.title,
                                para,
                                clause.source_path,
                                clause.page,
                                direct_parent_id,
                            )
                        )
                    else:
                        windows = _split_by_token_window(
                            para, self.config.target_tokens, self.config.overlap_tokens
                        )
                        for window_text in windows:
                            out.append(
                                _make_chunk(
                                    ChunkType.text_window.value,
                                    clause.spec_id,
                                    clause.chapter,
                                    clause.section,
                                    clause.article,
                                    clause.title,
                                    window_text,
                                    clause.source_path,
                                    clause.page,
                                    direct_parent_id,
                                )
                            )
        else:
            paragraphs = _split_paragraphs(remaining_text)
            if not paragraphs:
                return out
            for para in paragraphs:
                if _approx_tokens(para) <= self.config.target_tokens:
                    out.append(
                        _make_chunk(
                            ChunkType.paragraph.value,
                            clause.spec_id,
                            clause.chapter,
                            clause.section,
                            clause.article,
                            clause.title,
                            para,
                            clause.source_path,
                            clause.page,
                            direct_parent_id,
                        )
                    )
                else:
                    windows = _split_by_token_window(
                        para, self.config.target_tokens, self.config.overlap_tokens
                    )
                    for window_text in windows:
                        out.append(
                            _make_chunk(
                                ChunkType.text_window.value,
                                clause.spec_id,
                                clause.chapter,
                                clause.section,
                                clause.article,
                                clause.title,
                                window_text,
                                clause.source_path,
                                clause.page,
                                direct_parent_id,
                            )
                        )
        return out

    def chunk_clauses(self, clauses: list[RawClause]) -> list[Chunk]:
        all_chunks: list[Chunk] = []
        seen: dict[str, Chunk] = {}
        for clause in clauses:
            for chunk in self.chunk_clause(clause):
                if chunk.chunk_id not in seen:
                    seen[chunk.chunk_id] = chunk
                    all_chunks.append(chunk)
        return all_chunks


def chunk_clauses(clauses: list[RawClause], config: ChunkConfig | None = None) -> list[Chunk]:
    return Chunker(config).chunk_clauses(clauses)

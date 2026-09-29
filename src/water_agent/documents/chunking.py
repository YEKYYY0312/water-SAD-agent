"""上传文本切块（P5-S48 Step 1）。

**不复用 ``ingestion.Chunker``**：那是规范条文的结构化切块器（spec_id / chapter /
article 语义 + 父子块 + 表格识别），自由文本套不上，硬套只会造出一堆
``spec_id=""`` 的伪条文，把上传内容和规范条文混成一个命名空间。

但底层的文本窗口切分**复用**
``ingestion.chunker._split_paragraphs`` / ``_split_by_token_window`` —— 已有的实现，
不写第三份。

页码归属：PDF 提取器写出 ``--- Page {n} ---`` 标记（
:data:`~water_agent.documents.extractors.PAGE_MARKER_TEMPLATE`），这里按标记分段并把
页号带进每个块。标记本身不进块正文（它不是文档内容，进了检索就是噪声）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from water_agent.ingestion.chunker import _split_by_token_window, _split_paragraphs

#: 目标块大小（近似 token）。取 400 而非入库管线的 600：上传块要挤进提示词的
#: ``upload_context`` 段预算，块越大越容易只塞得下一块。
CHUNK_TARGET_TOKENS = 400
#: 相邻块重叠，避免把一句规定切在中间导致两边都检索不到。
CHUNK_OVERLAP_TOKENS = 60

#: 与 extractors.PAGE_MARKER_TEMPLATE 同构。两处必须一起改。
_PAGE_MARKER_RE = re.compile(r"^\s*---\s*Page\s+(\d+)\s*---\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class UploadChunk:
    """一个上传文本块：序号（0 起）、可选页号、正文。"""

    ordinal: int
    page: int | None
    content: str


def _split_pages(text: str) -> list[tuple[int | None, str]]:
    """按页码标记切分，返回 ``[(页号 | None, 该页文本), ...]``。

    没有任何标记时返回单段 ``(None, 全文)`` —— docx/txt 没有页概念，
    强行编一个页号会让引用说"第 1 页"而文档里根本没有分页。
    """

    matches = list(_PAGE_MARKER_RE.finditer(text))
    if not matches:
        stripped = text.strip()
        return [(None, stripped)] if stripped else []
    pages: list[tuple[int | None, str]] = []
    # 首个标记之前的内容（PDF 少见，但 docx 手写标记或拼接文本可能有）不丢。
    preamble = text[: matches[0].start()].strip()
    if preamble:
        pages.append((None, preamble))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            pages.append((int(match.group(1)), body))
    return pages


def chunk_upload_text(
    text: str,
    *,
    target_tokens: int = CHUNK_TARGET_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[UploadChunk]:
    """把提取文本切成有界块。空文本（如扫描件 PDF）返回空列表。

    空列表是**合法结果**，不是错误：调用方据此记录 ``extracted_chars=0``，用户看到
    "这份 PDF 没有文本层"这一如实事实（spec R1）。
    """

    if target_tokens <= 0:
        raise ValueError("target_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be non-negative and smaller than target_tokens")
    chunks: list[UploadChunk] = []
    ordinal = 0
    for page, page_text in _split_pages(text):
        # 先按段落聚到接近目标大小，再对超长段落做窗口切分：段落边界是语义边界，
        # 能整段放进一个块就不要在句子中间切。
        buffer: list[str] = []
        buffer_size = 0

        def flush(page: int | None = page) -> None:
            nonlocal ordinal, buffer, buffer_size
            if not buffer:
                return
            content = "\n".join(buffer).strip()
            buffer = []
            buffer_size = 0
            if content:
                chunks.append(UploadChunk(ordinal=ordinal, page=page, content=content))
                ordinal += 1

        for paragraph in _split_paragraphs(page_text):
            windows = _split_by_token_window(paragraph, target_tokens, overlap_tokens)
            if len(windows) > 1:
                # 超长段落自己成块（可能多块），不与邻居混装。
                flush()
                for window in windows:
                    body = window.strip()
                    if body:
                        chunks.append(UploadChunk(ordinal=ordinal, page=page, content=body))
                        ordinal += 1
                continue
            size = len(paragraph)
            if buffer and buffer_size + size > target_tokens:
                flush()
            buffer.append(paragraph)
            buffer_size += size
        flush()
    return chunks


__all__ = [
    "CHUNK_OVERLAP_TOKENS",
    "CHUNK_TARGET_TOKENS",
    "UploadChunk",
    "chunk_upload_text",
]

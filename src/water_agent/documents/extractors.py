"""上传文件的文本提取（P5-S48 Step 1）。

支持 ``.docx`` / ``.pdf`` / ``.txt`` / ``.md``，以及 ``.dxf``（P5-S50：抽取产物是
确定性审查报告，见 ``drawing_upload``），**不做 OCR、不做图片、不解析表格
类文件**（spec D5）。提取结果是纯文本，后续由护栏包成 ``RETRIEVED_FILE`` 信封，
因此这里只负责"把字取出来"，不负责信任判定。

PDF 引擎两条路径
----------------
默认 ``pypdf``（BSD-3）。``UPLOAD_PDF_ENGINE=pymupdf`` 时改走 pymupdf —— 它的版面
还原更好，但许可是 **AGPL-3.0**，对自托管商业部署有传染性。因此刻意**不做
"装了就用"的自动探测**：AGPL 代码路径必须由部署方显式选择，否则 `pip install` 一个
可选依赖就会无声改变整个产品的许可义务。

扫描件 PDF（没有文本层）返回 ``text=""`` 而**不报错**：那是一个如实的事实（spec R1），
包装成失败会让用户以为文件坏了，而真正要做的是人工转成文字版 PDF。
"""

from __future__ import annotations

import codecs
import io
import os
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from water_agent.documents.chunking import neutralize_page_markers
from water_agent.documents.errors import (
    CODE_UPLOAD_EXTRACTION_FAILED,
    CODE_UPLOAD_MAGIC_MISMATCH,
    CODE_UPLOAD_TYPE_NOT_ALLOWED,
    UploadError,
)

#: 单文件上限 10 MB（spec D1）。路由层必须在**读取过程中**就按这个值截断，
#: 不能先把整个 body 收进内存再判断。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: 扩展名白名单。白名单而非黑名单：新格式要显式加，默认不收。
ALLOWED_SUFFIXES: frozenset[str] = frozenset({".docx", ".pdf", ".txt", ".md", ".dxf", ".dwg"})

#: 扩展名 → 文件头魔数。仅对有稳定魔数的二进制格式校验；.txt/.md 没有魔数。
MAGIC_PREFIXES: dict[str, bytes] = {
    ".docx": b"PK\x03\x04",
    ".pdf": b"%PDF-",
}

#: 文件名里允许原样保留的字符，其余一律替换为 ``_``。
_SAFE_EXTRA_CHARS = frozenset({".", "-", "_"})

#: 页码标记。分块时按它归属 ``page``，因此格式固定，改动要同步 ``chunking.py``。
PAGE_MARKER_TEMPLATE = "\n--- Page {page} ---\n"

_PDF_ENGINE_ENV = "UPLOAD_PDF_ENGINE"
_DEFAULT_PDF_ENGINE = "pypdf"
_PYMUPDF_ENGINE = "pymupdf"


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """提取产物：文本 + 页数 + 实际使用的引擎名（进 trace，便于排查质量差异）。"""

    text: str
    page_count: int | None
    engine: str
    #: 抽取阶段就已判定的可疑（P5-S50：DXF 图元文字命中注入规则）。审查报告只含规则
    #: 结论、不含图元原文，分块护栏因此看不到那些文字——可疑必须从这里带出来。
    suspicious: bool = False
    #: 文本是否带本模块写入的页码标记（只有 PDF 路径置真）。分块只信这类标记：
    #: txt/md/docx 里用户自己写的 ``--- Page n ---`` 是内容，不是分页。
    paginated: bool = False

    @property
    def text_length(self) -> int:
        return len(self.text)


def safe_filename(name: str) -> str:
    """把上传的原始文件名归一为可安全落盘的名字。

    NFKC 归一（全角/兼容字符统一）→ 保留 alnum 与 ``.-_``、其余替换 ``_``。

    **路径穿越是拒绝而不是清洗**：含 ``..``、路径分隔符或绝对路径形态的名字直接抛
    :class:`UploadError`。清洗成 ``__etc_passwd`` 会静默接受一次明确的攻击尝试，
    而调用方永远不知道发生过——`materials/export.py::_validate_export_path` 同口径。
    """

    raw = (name or "").strip()
    if not raw:
        raise UploadError(CODE_UPLOAD_TYPE_NOT_ALLOWED, "文件名为空")
    normalized = unicodedata.normalize("NFKC", raw)
    if "/" in normalized or "\\" in normalized:
        raise UploadError(CODE_UPLOAD_TYPE_NOT_ALLOWED, "文件名不得包含路径分隔符")
    if ".." in normalized:
        raise UploadError(CODE_UPLOAD_TYPE_NOT_ALLOWED, "文件名不得包含 '..' 路径穿越")
    cleaned = "".join(
        char if (char.isalnum() or char in _SAFE_EXTRA_CHARS) else "_" for char in normalized
    )
    # 全部被替换掉（例如名字只有空格与符号）时仍需一个可落盘的名字。
    stripped = cleaned.strip("._-")
    if not stripped:
        raise UploadError(CODE_UPLOAD_TYPE_NOT_ALLOWED, "文件名归一后为空")
    return cleaned[:200]


def suffix_of(filename: str) -> str:
    """取小写扩展名并校验白名单。不在白名单抛 ``UPLOAD_TYPE_NOT_ALLOWED``。"""

    normalized = unicodedata.normalize("NFKC", (filename or "").strip())
    _, dot, tail = normalized.rpartition(".")
    suffix = f".{tail.lower()}" if dot else ""
    if suffix not in ALLOWED_SUFFIXES:
        allowed = "、".join(sorted(ALLOWED_SUFFIXES))
        raise UploadError(
            CODE_UPLOAD_TYPE_NOT_ALLOWED,
            f"不支持的文件类型 {suffix or '（无扩展名）'}；仅支持 {allowed}",
        )
    return suffix


def check_magic(data: bytes, suffix: str) -> None:
    """文件头魔数校验：扩展名声称的格式必须与真实内容一致。

    只看扩展名会让 ``payload.exe`` 改名成 ``payload.docx`` 直接进解析器。有魔数的
    格式必须对得上；``.txt``/``.md`` 无魔数可校验，由白名单与大小上限兜住。
    """

    prefix = MAGIC_PREFIXES.get(suffix)
    if prefix is None:
        return
    if not data.startswith(prefix):
        raise UploadError(
            CODE_UPLOAD_MAGIC_MISMATCH,
            f"文件内容与扩展名 {suffix} 不符（文件头校验未通过）",
        )


def pdf_engine_name(getenv: Callable[[str], str | None] = os.environ.get) -> str:
    """当前生效的 PDF 引擎名。未知取值回落默认，不为一个拼写错误就拒绝服务。"""

    configured = (getenv(_PDF_ENGINE_ENV) or "").strip().lower()
    return _PYMUPDF_ENGINE if configured == _PYMUPDF_ENGINE else _DEFAULT_PDF_ENGINE


def _extract_docx(data: bytes) -> ExtractedDocument:
    try:
        import docx  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover -- 依赖缺失兜底
        raise UploadError(
            CODE_UPLOAD_EXTRACTION_FAILED,
            "解析 .docx 需要 python-docx：安装 water-agent 依赖后重试",
        ) from exc
    from water_agent.documents.omml import count_ole_objects, paragraph_text

    try:
        document = docx.Document(io.BytesIO(data))
        # paragraph.text 会整段跳过 Word 公式（m:oMath）；计算书的数值几乎都在公式里。
        blocks = [paragraph_text(paragraph._p) for paragraph in document.paragraphs]
        ole_objects = count_ole_objects([document.element.body])
        # 表格文本必须一起取：给排水的设计说明把管径/流量表格当正文用，
        # 只取 paragraphs 会丢掉最有检索价值的那部分。
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    blocks.append("\t".join(cells))
    except UploadError:
        raise
    except Exception as exc:  # 第三方解析异常统一转结构化错误
        raise UploadError(
            CODE_UPLOAD_EXTRACTION_FAILED, f"解析 .docx 失败：{type(exc).__name__}"
        ) from exc
    text = "\n".join(block for block in blocks if block.strip())
    if ole_objects:
        # MathType/OLE 公式只有图片，读不到。如实写进正文末尾，模型才不会把"没读到"
        # 说成"文档里没有"。
        text += (
            f"\n【抽取说明】文档另含 {ole_objects} 个 OLE 嵌入对象（MathType 公式或嵌入图），"
            "其内容是图片，未能读取；相关数值请以原文件为准。"
        )
    return ExtractedDocument(text=text, page_count=None, engine="python-docx+omml")


def _extract_pdf_pypdf(data: bytes) -> ExtractedDocument:
    from water_agent.documents.pdf_parser import extract_pdf

    result = extract_pdf(data, "pypdf")
    return ExtractedDocument(
        text=result["text"], page_count=result["page_count"], engine="pypdf", paginated=True
    )


def _extract_pdf_pymupdf(data: bytes) -> ExtractedDocument:
    from water_agent.documents.pdf_parser import extract_pdf

    result = extract_pdf(data, "pymupdf")
    return ExtractedDocument(
        text=result["text"], page_count=result["page_count"], engine="pymupdf", paginated=True
    )


def _extract_plain_text(data: bytes) -> ExtractedDocument:
    # 带 BOM 的按 BOM 解码：Windows 记事本的"Unicode"是 UTF-16，按 UTF-8 解码会满屏乱码
    # 外加 NUL（PostgreSQL 的 text 列存不了 NUL）。
    # errors="replace"：编码不是 UTF-8 的文本文件不该整份拒收，坏字节以 U+FFFD
    # 现形，用户看得见"这里乱码了"，而不是拿到一个 400。
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text, engine = data.decode("utf-16", errors="replace"), "utf-16"
    else:
        text, engine = data.decode("utf-8-sig", errors="replace"), "utf-8"
    return ExtractedDocument(text=text.strip(), page_count=None, engine=engine)


def extract_text(
    data: bytes,
    suffix: str,
    *,
    getenv: Callable[[str], str | None] = os.environ.get,
    display_name: str = "",
) -> ExtractedDocument:
    """按扩展名提取文本。大小与魔数校验已在此处再做一遍（纵深防御）。

    Raises:
        UploadError: 超限 / 类型不允许 / 魔数不符 / 解析失败。
    """

    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(
            "UPLOAD_TOO_LARGE",
            f"文件超过上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    if suffix not in ALLOWED_SUFFIXES:
        allowed = "、".join(sorted(ALLOWED_SUFFIXES))
        raise UploadError(CODE_UPLOAD_TYPE_NOT_ALLOWED, f"不支持的文件类型 {suffix}；仅支持 {allowed}")
    check_magic(data, suffix)
    if suffix == ".dwg":
        # P5-S51：LibreDWG 转 DXF（入库侧受控子进程），再走与 .dxf 完全相同的审查链。
        from water_agent.documents.drawing_upload import review_uploaded_dxf
        from water_agent.documents.dwg_convert import convert_dwg

        conversion = convert_dwg(data)
        review = review_uploaded_dxf(
            conversion.dxf,
            display_name=display_name,
            source_note=(
                f"本图由 DWG 经 LibreDWG {conversion.version} 转换为 DXF 后审查"
                f"（转换器报告 {conversion.warnings} 条警告；代理实体、部分动态块可能未被转换）。"
            ),
        )
        return ExtractedDocument(
            text=review.text,
            page_count=None,
            engine=f"libredwg-{conversion.version}+dxf-review"[:32],
            suspicious=review.suspicious_entities > 0,
        )
    if suffix == ".dxf":
        # P5-S50：图纸的"文本"是程序化审查报告，不是图元文字（见 drawing_upload）。
        from water_agent.documents.drawing_upload import review_uploaded_dxf

        review = review_uploaded_dxf(data, display_name=display_name)
        return ExtractedDocument(
            text=review.text,
            page_count=None,
            engine="dxf-review",
            suspicious=review.suspicious_entities > 0,
        )
    if suffix == ".docx":
        return _extract_docx(data)
    if suffix == ".pdf":
        if pdf_engine_name(getenv) == _PYMUPDF_ENGINE:
            document = _extract_pdf_pymupdf(data)
        else:
            document = _extract_pdf_pypdf(data)
        return _with_vision_review(document, data, getenv=getenv)
    return _extract_plain_text(data)


#: 文本层"基本为空"的判定：每页平均不足这么多字。CAD 导出 PDF 常只剩页眉页码这类
#: 零星文字（实测 0 字），扫描件同理；有正文的设计说明每页几百字以上。
_SPARSE_TEXT_CHARS_PER_PAGE = 40
VISION_ENABLED_ENV = "UPLOAD_VISION_ENABLED"


def _with_vision_review(
    document: ExtractedDocument, data: bytes, *, getenv: Callable[[str], str | None]
) -> ExtractedDocument:
    """文本层基本为空的 PDF（CAD 导出、扫描件）→ 追加视觉审阅（P5-S52）。

    有正文的 PDF 不调视觉模型：文字已经读到了，再花钱看图只会引入误读。视觉失败时
    **上传照样成功**，但在正文里写明"视觉审阅未执行及原因"——文本层为空本来就读不到，
    视觉只是增强；静默失败会让模型以为"这份 PDF 没有任何内容"。
    """

    pages = document.page_count or 1
    if len(document.text.strip()) >= _SPARSE_TEXT_CHARS_PER_PAGE * pages:
        return document
    if (getenv(VISION_ENABLED_ENV) or "true").strip().lower() == "false":
        return document
    from water_agent.vision import VisionUnavailableError, review_pdf_pages

    try:
        review = review_pdf_pages(data)
    except VisionUnavailableError as exc:
        note = (
            f"【视觉审阅未执行】这份 PDF 几乎没有文字层（常见于 CAD 导出或扫描件），"
            f"本应由视觉模型看图，但未能执行：{exc}。当前无法读取图面内容，不得猜测。"
        )
        text = f"{document.text}\n\n{note}".strip()
        return ExtractedDocument(
            text=text,
            page_count=document.page_count,
            engine=document.engine,
            suspicious=document.suspicious,
            paginated=document.paginated,
        )
    # 视觉模型的输出同样是正文：形似页码标记的行不得变成分页信号
    text = f"{document.text}\n\n{neutralize_page_markers(review.text)}".strip()
    return ExtractedDocument(
        text=text,
        page_count=document.page_count,
        engine=f"{document.engine}+vision"[:32],
        suspicious=document.suspicious,
        paginated=document.paginated,
    )


__all__ = [
    "ALLOWED_SUFFIXES",
    "MAGIC_PREFIXES",
    "MAX_UPLOAD_BYTES",
    "PAGE_MARKER_TEMPLATE",
    "ExtractedDocument",
    "check_magic",
    "extract_text",
    "pdf_engine_name",
    "safe_filename",
    "suffix_of",
]

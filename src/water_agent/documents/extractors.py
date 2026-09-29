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

import io
import os
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

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
ALLOWED_SUFFIXES: frozenset[str] = frozenset({".docx", ".pdf", ".txt", ".md", ".dxf"})

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
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover -- 依赖缺失兜底
        raise UploadError(
            CODE_UPLOAD_EXTRACTION_FAILED,
            "解析 .pdf 需要 pypdf：安装 water-agent 依赖后重试",
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages
        parts: list[str] = []
        for index, page in enumerate(pages, start=1):
            # 单页提取失败不该让整份文档失败：一页坏版面在真实图纸说明里很常见，
            # 其余页的文本仍有检索价值。
            try:
                page_text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 -- 单页降级，不放大为整份失败
                page_text = ""
            if not page_text.strip():
                # 空页**不写页码标记**：否则一份全是扫描图的 PDF 会产出一串
                # "--- Page n ---"，``extracted_text_length`` 不为 0，
                # "这份 PDF 没有文本层"这个用户唯一需要知道的事实就消失了（spec R1）。
                # 页号仍由 page_count 与后续页的标记如实反映。
                continue
            parts.append(PAGE_MARKER_TEMPLATE.format(page=index) + page_text)
        page_count = len(pages)
    except UploadError:
        raise
    except Exception as exc:  # 第三方解析异常统一转结构化错误
        raise UploadError(
            CODE_UPLOAD_EXTRACTION_FAILED, f"解析 .pdf 失败：{type(exc).__name__}"
        ) from exc
    return ExtractedDocument(text="".join(parts).strip(), page_count=page_count, engine="pypdf")


def _extract_pdf_pymupdf(data: bytes) -> ExtractedDocument:
    try:
        import pymupdf  # type: ignore[import-untyped,import-not-found,unused-ignore]  # 可选 extra，runner 上未装
    except ImportError as exc:
        raise UploadError(
            CODE_UPLOAD_EXTRACTION_FAILED,
            f"{_PDF_ENGINE_ENV}={_PYMUPDF_ENGINE} 但未安装 pymupdf："
            "安装 water-agent[pdf-enhanced]（注意 AGPL-3.0 许可义务）或改回默认 pypdf",
        ) from exc
    try:
        parts: list[str] = []
        with pymupdf.open(stream=data, filetype="pdf") as document:
            page_count = document.page_count
            for index, page in enumerate(document, start=1):
                page_text = page.get_text()
                # 与 pypdf 路径同语义：空页不写页码标记（见那里的说明）。
                if not page_text.strip():
                    continue
                parts.append(PAGE_MARKER_TEMPLATE.format(page=index) + page_text)
    except UploadError:
        raise
    except Exception as exc:  # 第三方解析异常统一转结构化错误
        raise UploadError(
            CODE_UPLOAD_EXTRACTION_FAILED, f"解析 .pdf 失败：{type(exc).__name__}"
        ) from exc
    return ExtractedDocument(text="".join(parts).strip(), page_count=page_count, engine="pymupdf")


def _extract_plain_text(data: bytes) -> ExtractedDocument:
    # errors="replace"：编码不是 UTF-8 的文本文件不该整份拒收，坏字节以 U+FFFD
    # 现形，用户看得见"这里乱码了"，而不是拿到一个 400。
    return ExtractedDocument(
        text=data.decode("utf-8", errors="replace").strip(), page_count=None, engine="utf-8"
    )


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
            return _extract_pdf_pymupdf(data)
        return _extract_pdf_pypdf(data)
    return _extract_plain_text(data)


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

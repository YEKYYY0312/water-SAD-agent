"""上传文档的解析、切块与持久化（P5-S48）。

本包是上传链路的**唯一实现**：解析（`extractors`）、切块（`chunking`）、落盘位置
（`storage`）、授权持久化（`repository`）、编排（`service`）、召回与提示段渲染
（`retrieval`、Step 2）。`webapp/upload_routes.py` 是薄适配器，不在路由层定义任何
解析或治理策略。

边界（spec D5）：不做 OCR、不做图片上传、不做 Excel/CSV 解析、不做版本控制、
不做用户间文件共享、不做病毒扫描（个人/单位自托管，风险自留）。
"""

from water_agent.documents.chunking import UploadChunk, chunk_upload_text
from water_agent.documents.errors import (
    UPLOAD_ERROR_CODES,
    UPLOAD_ERROR_STATUS,
    UploadError,
)
from water_agent.documents.extractors import (
    ALLOWED_SUFFIXES,
    MAX_UPLOAD_BYTES,
    ExtractedDocument,
    extract_text,
    pdf_engine_name,
    safe_filename,
    suffix_of,
)
from water_agent.documents.repository import UploadRecord, UploadRepository
from water_agent.documents.service import UploadIngestResult, ingest_upload
from water_agent.documents.storage import (
    remove_upload_files,
    resolve_export_dir,
    resolve_upload_dir,
)

__all__ = [
    "ALLOWED_SUFFIXES",
    "MAX_UPLOAD_BYTES",
    "UPLOAD_ERROR_CODES",
    "UPLOAD_ERROR_STATUS",
    "ExtractedDocument",
    "UploadChunk",
    "UploadError",
    "UploadIngestResult",
    "UploadRecord",
    "UploadRepository",
    "chunk_upload_text",
    "extract_text",
    "ingest_upload",
    "pdf_engine_name",
    "remove_upload_files",
    "resolve_export_dir",
    "resolve_upload_dir",
    "safe_filename",
    "suffix_of",
]

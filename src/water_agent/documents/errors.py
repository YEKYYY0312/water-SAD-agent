"""上传链路的稳定错误码（P5-S48 Step 1）。

与治理错误码（``runtime/governance/contracts.py``）分开命名空间：这些码进 HTTP
响应体与 trace，前端按码渲染可行动文案，因此**只增不改**。

刻意**不**设"未知失败"兜底码：解析失败一律是 ``UPLOAD_EXTRACTION_FAILED``，
让调用方拿到可区分的事实，而不是一个什么都可能的通用码。
"""

from __future__ import annotations

#: 超过 :data:`~water_agent.documents.extractors.MAX_UPLOAD_BYTES`
CODE_UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
#: 扩展名不在白名单，或 filename 含路径穿越/分隔符
CODE_UPLOAD_TYPE_NOT_ALLOWED = "UPLOAD_TYPE_NOT_ALLOWED"
#: 扩展名与文件头魔数不符（.docx 不是 ZIP / .pdf 不是 %PDF-）
CODE_UPLOAD_MAGIC_MISMATCH = "UPLOAD_MAGIC_MISMATCH"
#: python-docx / pypdf / pymupdf 解析抛错
CODE_UPLOAD_EXTRACTION_FAILED = "UPLOAD_EXTRACTION_FAILED"
#: 护栏 BLOCK（提取文本过大，或信封被标为可承载指令——后者是防御性分支）
CODE_UPLOAD_INJECTION_BLOCKED = "UPLOAD_INJECTION_BLOCKED"

UPLOAD_ERROR_CODES: frozenset[str] = frozenset(
    {
        CODE_UPLOAD_TOO_LARGE,
        CODE_UPLOAD_TYPE_NOT_ALLOWED,
        CODE_UPLOAD_MAGIC_MISMATCH,
        CODE_UPLOAD_EXTRACTION_FAILED,
        CODE_UPLOAD_INJECTION_BLOCKED,
    }
)

#: 每个码对应的 HTTP 状态。大小超限是 413，其余输入问题是 400。
UPLOAD_ERROR_STATUS: dict[str, int] = {
    CODE_UPLOAD_TOO_LARGE: 413,
    CODE_UPLOAD_TYPE_NOT_ALLOWED: 400,
    CODE_UPLOAD_MAGIC_MISMATCH: 400,
    CODE_UPLOAD_EXTRACTION_FAILED: 400,
    CODE_UPLOAD_INJECTION_BLOCKED: 400,
}


class UploadError(Exception):
    """上传处理失败，携带稳定错误码。

    ``message`` 是给用户看的可行动文案，**不得回显上传文件的正文**：上传内容可能
    含 PII 或注入文本，错误消息会进 HTTP 响应与 trace。
    """

    def __init__(self, code: str, message: str) -> None:
        if code not in UPLOAD_ERROR_CODES:
            raise ValueError(f"未登记的上传错误码：{code!r}")
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

    @property
    def status_code(self) -> int:
        return UPLOAD_ERROR_STATUS[self.code]


__all__ = [
    "CODE_UPLOAD_EXTRACTION_FAILED",
    "CODE_UPLOAD_INJECTION_BLOCKED",
    "CODE_UPLOAD_MAGIC_MISMATCH",
    "CODE_UPLOAD_TOO_LARGE",
    "CODE_UPLOAD_TYPE_NOT_ALLOWED",
    "UPLOAD_ERROR_CODES",
    "UPLOAD_ERROR_STATUS",
    "UploadError",
]

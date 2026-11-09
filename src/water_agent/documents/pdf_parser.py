"""Isolated PDF text extraction with a killable wall-clock deadline."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from water_agent.documents.errors import CODE_UPLOAD_EXTRACTION_FAILED, UploadError

PDF_TIMEOUT_SECONDS = 30
MAX_PDF_PAGES = 1000
MAX_PDF_TEXT_CHARS = 1_000_000
PDF_CPU_SECONDS = 20
PDF_MEMORY_BYTES = 1024 * 1024 * 1024


def extract_pdf(data: bytes, engine: str) -> dict:
    """Run only the text parser in a credential-free subprocess, never a model."""
    if engine not in {"pypdf", "pymupdf"}:
        raise ValueError("unsupported PDF engine")
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    if os.environ.get("TMPDIR"):
        environment["TMPDIR"] = os.environ["TMPDIR"]
    # Diagnostics can contain document text and expand far beyond input size.
    # They are never consumed, so discard them instead of spooling shared disk.
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "water_agent.documents.pdf_parser", engine],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, timeout=PDF_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UploadError(
            CODE_UPLOAD_EXTRACTION_FAILED,
            "PDF 解析超时，已终止解析进程；请拆分文档或提供文字版后重试",
        ) from exc
    except OSError as exc:
        raise UploadError(CODE_UPLOAD_EXTRACTION_FAILED, "PDF 隔离解析进程不可用") from exc
    if completed.returncode != 0:
        raise UploadError(
            CODE_UPLOAD_EXTRACTION_FAILED,
            "PDF 无法安全解析或超过资源限制；请检查、拆分文档后重试",
        )
    try:
        result = json.loads(completed.stdout)
        if (not isinstance(result, dict)
                or not isinstance(result.get("text"), str)
                or type(result.get("page_count")) is not int
                or not 0 <= result["page_count"] <= MAX_PDF_PAGES
                or len(result["text"]) > MAX_PDF_TEXT_CHARS):
            raise ValueError("invalid parser result")
    except (ValueError, UnicodeError) as exc:
        raise UploadError(CODE_UPLOAD_EXTRACTION_FAILED, "PDF 解析结果不符合安全约束") from exc
    return result


def _resource_limits() -> None:
    if os.name != "posix":
        return
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (PDF_CPU_SECONDS, PDF_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # macOS does not reliably enforce RLIMIT_AS; production Linux does.
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (PDF_MEMORY_BYTES, PDF_MEMORY_BYTES))


def _parse(data: bytes, engine: str) -> dict:
    import io

    from water_agent.documents.chunking import neutralize_page_markers

    if engine == "pypdf":
        from pypdf import PdfReader

        document = PdfReader(io.BytesIO(data))
        pages = document.pages
        count = len(pages)
        if count > MAX_PDF_PAGES:
            raise ValueError("page limit")
        def page_text(page: object) -> str:
            try:
                return page.extract_text() or ""  # type: ignore[attr-defined,no-any-return]
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001 -- Preserve the established per-page degradation.
                return ""

        texts = (page_text(page) for page in pages)
    else:
        import pymupdf  # type: ignore[import-not-found]  # Optional pdf-enhanced extra.

        document = pymupdf.open(stream=data, filetype="pdf")
        count = document.page_count
        if count > MAX_PDF_PAGES:
            document.close()
            raise ValueError("page limit")
        texts = (page.get_text() for page in document)
    parts: list[str] = []
    characters = 0
    try:
        for index, text in enumerate(texts, start=1):
            if not text.strip():
                continue
            part = f"\n--- Page {index} ---\n" + neutralize_page_markers(text)
            characters += len(part)
            if characters > MAX_PDF_TEXT_CHARS:
                raise ValueError("text limit")
            parts.append(part)
    finally:
        if engine == "pymupdf":
            document.close()
    return {"text": "".join(parts).strip(), "page_count": count}


def main() -> int:
    _resource_limits()
    engine = sys.argv[1] if len(sys.argv) == 2 else ""
    if engine not in {"pypdf", "pymupdf"}:
        return 2
    # Parent checks the upload limit too; bound worker stdin independently.
    data = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        return 2
    try:
        result = _parse(data, engine)
    except Exception:  # noqa: BLE001 -- Parser errors leave through a fixed, content-free exit.
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

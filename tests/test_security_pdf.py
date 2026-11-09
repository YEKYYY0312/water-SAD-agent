"""PDF parsing must terminate, even for a truncated cross-reference table."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_truncated_xref_is_rejected_without_hanging() -> None:
    # Upstream pypdf #4170 regression input, generated locally, no real document.
    script = r'''
from water_agent.documents.extractors import extract_text
from water_agent.documents.errors import UploadError
body = b"%PDF-1.0\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
tail = b"\nstartxref\n%d\n%%%%EOF" % len(body)
data = body + b"xref\n0 3\n0000000000 65535 f \n" + tail
try:
    extract_text(data, ".pdf", getenv=lambda _: "false")
except UploadError:
    print("rejected", flush=True)
else:
    raise AssertionError("truncated PDF must not be accepted")
'''
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": os.environ.get("TMPDIR", str(ROOT.parent)),
    }
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, cwd=ROOT,
        capture_output=True, text=True, timeout=4, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "rejected"


def test_pdf_diagnostics_are_discarded_without_disk_spooling(monkeypatch) -> None:
    from water_agent.documents import pdf_parser

    def run(*args, **kwargs):
        assert kwargs["stderr"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(args[0], 0, b'{"text":"", "page_count":0}')

    monkeypatch.setattr(pdf_parser.subprocess, "run", run)
    assert pdf_parser.extract_pdf(b"dummy", "pypdf")["page_count"] == 0



def test_one_broken_pdf_page_does_not_discard_readable_pages(monkeypatch) -> None:
    import pypdf

    from water_agent.documents.pdf_parser import _parse

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            if self.text is None:
                raise ValueError("damaged page")
            return self.text

    class Reader:
        def __init__(self, stream):
            self.pages = [Page("first page"), Page(None), Page("third page")]

    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    result = _parse(b"dummy", "pypdf")
    assert result["page_count"] == 3
    assert "first page" in result["text"] and "third page" in result["text"]
    assert "--- Page 2 ---" not in result["text"]


def test_parser_timeout_kills_the_worker_and_does_not_echo_content(monkeypatch, tmp_path) -> None:
    import pytest

    from water_agent.documents import pdf_parser
    from water_agent.documents.errors import UploadError

    started = tmp_path / "started"
    finished = tmp_path / "finished"
    real_run = subprocess.run
    worker = (
        "import time,pathlib; "
        f"pathlib.Path({str(started)!r}).touch(); "
        "time.sleep(2); "
        f"pathlib.Path({str(finished)!r}).touch()"
    )

    def run(*args, **kwargs):
        assert kwargs["env"].keys() <= {
            "PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING", "TMPDIR"
        }
        return real_run([sys.executable, "-c", worker], **kwargs)

    monkeypatch.setattr(pdf_parser.subprocess, "run", run)
    monkeypatch.setattr(pdf_parser, "PDF_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("SECRET_FOR_TEST", "synthetic-private-text")
    with pytest.raises(UploadError, match="超时") as caught:
        pdf_parser.extract_pdf(b"synthetic-private-text", "pypdf")
    assert "synthetic-private-text" not in str(caught.value)
    assert started.exists() and not finished.exists()


def test_pdf_page_limit_is_checked_before_text_extraction(monkeypatch) -> None:
    import pypdf
    import pytest

    from water_agent.documents import pdf_parser

    class Page:
        def extract_text(self):
            raise AssertionError("must check limit first")

    class Reader:
        def __init__(self, stream):
            self.pages = [Page(), Page()]

    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    monkeypatch.setattr(pdf_parser, "MAX_PDF_PAGES", 1)
    with pytest.raises(ValueError, match="page limit"):
        pdf_parser._parse(b"dummy", "pypdf")


def test_pdf_text_limit_rejects_instead_of_truncating(monkeypatch) -> None:
    import pypdf
    import pytest

    from water_agent.documents import pdf_parser

    class Page:
        def extract_text(self):
            return "large paragraph"

    class Reader:
        def __init__(self, stream):
            self.pages = [Page()]

    monkeypatch.setattr(pypdf, "PdfReader", Reader)
    monkeypatch.setattr(pdf_parser, "MAX_PDF_TEXT_CHARS", 4)
    with pytest.raises(ValueError, match="text limit"):
        pdf_parser._parse(b"dummy", "pypdf")

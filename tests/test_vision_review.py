"""P5-S52：CAD 导出 PDF 的视觉审阅（离线：假 transport，绝不发真实请求）。

守住：
- 只有文本层基本为空的 PDF 才看图；有正文的 PDF 不花这笔钱；
- 视觉结果整段标注"图像转写，可能误读，不是程序化校验"，数值不得直接用于计算；
- 视觉失败时上传照样成功，但正文写明"视觉审阅未执行及原因"，不静默；
- 走 ProviderGateway（第 7 个远程出口），usage 如实对账；页数有上限并声明未审阅页；
- 发送的是渲染后的页面图像，请求体里没有密钥以外的凭据、没有用户文件名。
"""

from __future__ import annotations

import base64
import io

import pytest

from water_agent import vision
from water_agent.documents.extractors import extract_text
from water_agent.runtime.governance.contracts import CallCategory, ProviderKind
from water_agent.runtime.governance.inventory import PROVIDER_CALL_SITES, call_site


def _pdf(pages: list[str]) -> bytes:
    """最小 PDF：每页一行文字（空串 = 无文本层）。"""

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=842, height=595)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
            )
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 500 Td ({text}) Tj ET".encode("latin-1"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class _FakeTransport:
    def __init__(self, reply: str = "1) 图名：管道总平面布置图 2) 构筑物：絮凝沉淀池") -> None:
        self.reply = reply
        self.payloads: list[dict] = []

    def __call__(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return {
            "choices": [{"message": {"content": self.reply}}],
            "usage": {"prompt_tokens": 2654, "completion_tokens": 321, "prompt_tokens_details": {"cached_tokens": 0}},
        }


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeTransport:
    transport = _FakeTransport()
    original = vision.QwenVisionReviewer

    def build(api_key: str = "", **kwargs):
        return original("test-key", transport=transport, **kwargs)

    monkeypatch.setattr(vision, "QwenVisionReviewer", build)
    monkeypatch.setenv("UPLOAD_VISION_ENABLED", "true")
    return transport


def test_textless_pdf_is_reviewed_and_labelled_as_image_transcription(fake: _FakeTransport) -> None:
    extracted = extract_text(_pdf(["", ""]), ".pdf")
    assert "【视觉审阅结果】" in extracted.text
    assert "可能误读" in extracted.text and "不得直接用于计算" in extracted.text
    assert "絮凝沉淀池" in extracted.text
    assert "--- 第 1 页 ---" in extracted.text and "--- 第 2 页 ---" in extracted.text
    assert extracted.engine.endswith("+vision")
    assert len(fake.payloads) == 2


def test_pdf_with_real_text_is_not_sent_to_the_vision_model(fake: _FakeTransport) -> None:
    body = "Design note: water supply by variable-speed pumps, drainage by gravity. " * 3
    extracted = extract_text(_pdf([body]), ".pdf")
    assert "【视觉审阅结果】" not in extracted.text
    assert fake.payloads == [], "有文字层的 PDF 不得花钱看图"


def test_request_carries_only_the_rendered_page_and_the_fixed_prompt(fake: _FakeTransport) -> None:
    extract_text(_pdf([""]), ".pdf")
    payload = fake.payloads[0]
    assert payload["model"] == vision.DEFAULT_VISION_MODEL
    assert payload["temperature"] == 0 and payload["max_tokens"] == vision.MAX_OUTPUT_TOKENS
    content = payload["messages"][0]["content"]
    image = content[0]["image_url"]["url"]
    assert image.startswith("data:image/png;base64,")
    png = base64.b64decode(image.split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert content[1]["text"] == vision.VISION_PROMPT
    assert "不要推测" in vision.VISION_PROMPT


def test_page_limit_is_declared(fake: _FakeTransport) -> None:
    extracted = extract_text(_pdf([""] * (vision.MAX_PAGES + 2)), ".pdf")
    assert len(fake.payloads) == vision.MAX_PAGES
    assert f"共审阅 {vision.MAX_PAGES}/{vision.MAX_PAGES + 2} 页" in extracted.text
    assert "未审阅" in extracted.text


def test_vision_failure_keeps_the_upload_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_VISION_ENABLED", "true")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    extracted = extract_text(_pdf([""]), ".pdf")
    assert "【视觉审阅未执行】" in extracted.text
    assert "DASHSCOPE_API_KEY" in extracted.text
    assert "不得猜测" in extracted.text


def test_vision_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, fake: _FakeTransport) -> None:
    monkeypatch.setenv("UPLOAD_VISION_ENABLED", "false")
    extracted = extract_text(_pdf([""]), ".pdf")
    assert extracted.text == "" and fake.payloads == []


def test_calls_go_through_the_gateway_and_usage_is_reconciled(monkeypatch: pytest.MonkeyPatch) -> None:
    from water_agent.runtime.governance.gateway import ProviderGateway

    seen: list = []
    real_call = ProviderGateway.call

    def spy(self, ctx, call, executor):
        result = real_call(self, ctx, call, executor)
        seen.append((call, result.usage_record))
        return result

    monkeypatch.setattr(ProviderGateway, "call", spy)
    reviewer = vision.QwenVisionReviewer("test-key", transport=_FakeTransport())
    review = vision.review_pdf_pages(_pdf([""]), reviewer=reviewer)
    call, record = seen[0]
    assert call.provider is ProviderKind.BAILIAN_VISION
    assert call.category is CallCategory.VISION_REVIEW
    assert record.usage_known and record.input_tokens == 2654 and record.output_tokens == 321
    assert review.input_tokens == 2654 and review.output_tokens == 321


def test_missing_key_is_refused_before_any_request() -> None:
    with pytest.raises(vision.VisionUnavailableError):
        vision.QwenVisionReviewer("  ")


def test_vision_call_site_is_registered_and_governed() -> None:
    site = call_site("bailian_vision_pdf_review")
    assert site.egress_module == "water_agent/vision.py"
    assert site.governed and not site.cache_eligible
    assert len(PROVIDER_CALL_SITES) == 7

"""CAD 导出 PDF 的视觉审阅（P5-S52，百炼 Qwen-VL）。

CAD 导出的 PDF 几乎都是矢量线条、文字转成了曲线，文本层为空（S48 抽取得到 0 字）。
这里把每页渲染成图片交给视觉模型，得到一份**图面描述**，作为这份上传的抽取文本进入
既有的分块 / 召回 / 显式附件链路。

纪律（规格 D3–D6）
------------------
- **第 7 个远程出口**：经 ``ProviderGateway``（限流、预算预留、有界重试、熔断、usage
  对账），``inventory.PROVIDER_CALL_SITES`` 登记、静态扫描覆盖。不自建重试。
- **视觉读出的内容是转写，不是事实**：输出整段标注"视觉识别结果，可能误读，不是程序化
  校验"，数值不得直接进入 calc 工具（铁律 1），条款仍须 ``cite_clauses``（铁律 2）。
  提示词要求"看不清就写看不清、不推测"，并要求给出位置。
- **有界**：页数上限、每页像素上限、输出 token 上限；超出页数如实声明未审阅的页。
- 失败（无 Key、403、超时）→ 上传照样成功但注明"视觉审阅未执行及原因"，**不静默**：
  文本层为空的 PDF 本来就读不到，视觉只是增强，不是上传的必要条件。
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib import request

from water_agent.config.credentials import resolve_secret
from water_agent.runtime.governance.binding import default_gateway, local_request_context
from water_agent.runtime.governance.contracts import (
    CallCategory,
    Capability,
    ProviderCall,
    ProviderKind,
    RequestContext,
)
from water_agent.runtime.governance.gateway import ProviderGateway, ProviderUsage

#: 实测（2026-09-29，同一张 A3 管道总平面图）：qwen3-vl-plus 39.7 s，qwen3-vl-flash
#: 18.6 s，图像均 ~2,500 token。plus 读对了"絮凝沉淀池"，flash 读成"紫菜沉淀池"、把
#: "I 期工程"读成"男工棚"——审图场景误读代价高，默认用 plus。可用环境变量改。
DEFAULT_VISION_MODEL = "qwen3-vl-plus"
VISION_MODEL_ENV = "WATER_AGENT_VISION_MODEL"
DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
#: 每次上传最多审阅几页。CAD 导出一般 1–3 张图；更多页如实声明未审阅。
MAX_PAGES = 4
#: 渲染长边像素。2000 px 在 A3 1:500 总平面图上能读清 2.5 mm 字高的标注（实测）。
RENDER_LONG_EDGE_PX = 2000
MAX_OUTPUT_TOKENS = 2000
#: 图像 token 的预留估算（实测 2000 px 长边 ≈ 2,522 token）。只用于预算预留，真实用量
#: 以返回的 usage 对账。
_ESTIMATED_IMAGE_TOKENS = 2600

VISION_PROMPT = (
    "你在审阅一张给排水 CAD 图纸的页面图像。只描述图上**能看见**的内容；看不清的写“看不清”，"
    "不要推测、不要补全、不要换算。按以下小节输出，每项尽量注明在图面的大致位置（左上/中部/右下…）：\n"
    "1) 图名、图号、比例、设计阶段（来自标题栏）；\n"
    "2) 构筑物与建筑物（照录图上文字）；\n"
    "3) 管道：管径/规格标注、标高标注、管材、走向概述；\n"
    "4) 设备与阀门（照录型号）；\n"
    "5) 图例与说明文字（原文照录）；\n"
    "6) 图面可见的疑点：标注缺失、前后矛盾、看不清的关键数字（只列图面可见的，不做规范判断）。"
)

VISION_HEADER = (
    "【视觉审阅结果】以下由视觉模型从 PDF 页面图像识别得到（{model}，共审阅 {reviewed}/{total} 页）。"
    "这是**图像转写，可能误读**，不是图纸原文、也不是程序化校验：其中的数值不得直接用于计算，"
    "需要时请用户核对原图；规范判断仍须检索并引用条款。"
)


class VisionUnavailableError(RuntimeError):
    """视觉审阅无法执行（无 Key / 权限 / 网络 / 渲染失败）。调用方降级为"未执行"说明。"""


@dataclass(frozen=True, slots=True)
class VisionReview:
    text: str
    model: str
    pages_reviewed: int
    pages_total: int
    input_tokens: int | None
    output_tokens: int | None


def _png_from_bitmap(bitmap: Any) -> bytes:
    """pypdfium2 位图 → 灰度 PNG，只用标准库（zlib + struct）。

    不走 ``bitmap.to_pil()``：那需要 Pillow，为了一次编码引入一个带 C 扩展的依赖不值得。
    """

    import struct
    import zlib

    width, height, stride = int(bitmap.width), int(bitmap.height), int(bitmap.stride)
    channels = int(getattr(bitmap, "n_channels", 1) or 1)
    raw = bytes(bitmap.buffer)
    rows = bytearray()
    for y in range(height):
        line = raw[y * stride : y * stride + width * channels]
        rows.append(0)  # PNG filter type 0
        rows.extend(line if channels == 1 else line[::channels])
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grayscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


def render_pdf_pages(data: bytes, *, max_pages: int = MAX_PAGES) -> tuple[list[bytes], int]:
    """把 PDF 前 ``max_pages`` 页渲染成灰度 PNG，返回 (图片列表, 总页数)。"""

    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover -- 依赖缺失兜底
        raise VisionUnavailableError("缺少 pypdfium2，无法渲染 PDF 页面") from exc
    try:
        pdf = pdfium.PdfDocument(data)
        total = len(pdf)
        images: list[bytes] = []
        for index in range(min(total, max_pages)):
            page = pdf[index]
            width, height = page.get_size()
            scale = RENDER_LONG_EDGE_PX / max(width, height, 1.0)
            bitmap = page.render(scale=scale, grayscale=True)
            images.append(_png_from_bitmap(bitmap))
        pdf.close()
    except Exception as exc:
        raise VisionUnavailableError(f"PDF 页面渲染失败（{type(exc).__name__}）") from exc
    return images, total


class QwenVisionReviewer:
    """百炼 OpenAI 兼容端点上的 Qwen-VL 调用（``_call`` 是唯一注入缝）。"""

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        gateway: ProviderGateway | None = None,
        request_context: RequestContext | None = None,
    ) -> None:
        if not api_key.strip():
            raise VisionUnavailableError("未配置 DASHSCOPE_API_KEY，视觉审阅不可用")
        self.api_key = api_key
        self.model = model or os.environ.get(VISION_MODEL_ENV, "").strip() or DEFAULT_VISION_MODEL
        self.endpoint = endpoint
        self._transport = transport
        self._gateway = gateway
        self._request_context = request_context

    def _resolve_gateway(self) -> ProviderGateway:
        return self._gateway if self._gateway is not None else default_gateway()

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发一次请求。**不含重试**：重试/退避/熔断由 ``ProviderGateway`` 承担。"""

        if self._transport is not None:
            return self._transport(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            self.endpoint,
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        timeout = self._resolve_gateway().timeout_for(ProviderKind.BAILIAN_VISION)
        with request.urlopen(req, timeout=timeout) as raw:
            return json.loads(raw.read().decode("utf-8"))  # type: ignore[no-any-return]

    def describe_page(self, png: bytes) -> tuple[str, ProviderUsage]:
        image = base64.b64encode(png).decode("ascii")
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                }
            ],
        }
        call = ProviderCall(
            provider=ProviderKind.BAILIAN_VISION,
            category=CallCategory.VISION_REVIEW,
            model=self.model,
            temperature=0.0,
            estimated_input_tokens=_ESTIMATED_IMAGE_TOKENS + len(VISION_PROMPT),
            max_output_tokens=MAX_OUTPUT_TOKENS,
            required_capability=Capability.LLM_INFERENCE,
        )

        def executor() -> tuple[dict[str, Any], ProviderUsage]:
            response = self._call(payload)
            usage = response.get("usage") or {}
            if isinstance(usage, dict) and "prompt_tokens" in usage:
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                return response, ProviderUsage.of(
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                    cached_input_tokens=int(cached),
                )
            return response, ProviderUsage.unknown()

        context = self._request_context or local_request_context(thread_id="vision-review")
        captured: list[ProviderUsage] = []

        def counted() -> tuple[dict[str, Any], ProviderUsage]:
            response, usage = executor()
            captured.append(usage)
            return response, usage

        response = self._resolve_gateway().call(context, call, counted).value
        try:
            text = str(response["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionUnavailableError("视觉模型返回格式异常") from exc
        return text, captured[-1] if captured else ProviderUsage.unknown()


def review_pdf_pages(
    data: bytes,
    *,
    reviewer: QwenVisionReviewer | None = None,
) -> VisionReview:
    """渲染并逐页审阅。

    Raises:
        VisionUnavailableError: 无 Key、渲染失败、或模型调用失败（调用方降级）。
    """

    from water_agent.runtime.governance.contracts import GovernanceError

    images, total = render_pdf_pages(data)
    if not images:
        raise VisionUnavailableError("PDF 没有可渲染的页面")
    if reviewer is None:
        # 与 embedding / rerank 同一解析顺序（env → _FILE → 凭据卷），不直读 os.environ。
        reviewer = QwenVisionReviewer(resolve_secret("DASHSCOPE_API_KEY") or "")
    sections: list[str] = []
    in_tokens = out_tokens = 0
    known = True
    for index, png in enumerate(images, start=1):
        try:
            text, usage = reviewer.describe_page(png)
        except GovernanceError as exc:
            raise VisionUnavailableError(f"视觉模型调用失败：{exc}") from exc
        sections.append(f"--- 第 {index} 页 ---\n{text}")
        if usage.known:
            in_tokens += usage.input_tokens
            out_tokens += usage.output_tokens
        else:
            known = False
    header = VISION_HEADER.format(model=reviewer.model, reviewed=len(images), total=total)
    if total > len(images):
        header += f"第 {len(images) + 1}–{total} 页未审阅（每次上限 {MAX_PAGES} 页）。"
    return VisionReview(
        text=header + "\n\n" + "\n\n".join(sections),
        model=reviewer.model,
        pages_reviewed=len(images),
        pages_total=total,
        input_tokens=in_tokens if known else None,
        output_tokens=out_tokens if known else None,
    )


__all__ = [
    "DEFAULT_VISION_MODEL",
    "MAX_PAGES",
    "VISION_PROMPT",
    "QwenVisionReviewer",
    "VisionReview",
    "VisionUnavailableError",
    "render_pdf_pages",
    "review_pdf_pages",
]

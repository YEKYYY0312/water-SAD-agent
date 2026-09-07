"""图纸中间格式：解析器输出、规则引擎与语义映射的共同输入。

契约见 `docs/design/13-p3-drawing-review.md` §3。要点：

- **不沿用** `04-file-reading-and-mcp.md` 设想的通用 `read_file()` 返回形状：
  `chunks`/`tables` 对图纸是死字段，填了会让读者以为分块/表格抽取已经生效
  （P2.5 §4 禁止保留看似生效的死字段）。P3 只交付 `entities`。
- 坐标一律 `float`（解析器负责把 numpy 标量强制转换，见 §5.3）。
- `text` 是**已过内容护栏**的文本；`suspicious` 只带规则 ID，绝不带命中文本
  （与 `GuardrailPipeline._summarize` 同一纪律：摘要不泄漏原文）。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 稳定错误码：前置条件缺失与真实失败必须可区分
# ---------------------------------------------------------------------------

#: 登记号在 DRAWING_ROOT 内无对应文件。
DRAWING_NOT_FOUND = "DRAWING_NOT_FOUND"
#: 路径三层防御任一层拒绝（登记号非法 / 解析后越界 / 不在读白名单）。
DRAWING_PATH_DENIED = "DRAWING_PATH_DENIED"
#: ezdxf 无法解析（坏文件、空文件、非 DXF 内容）。
DRAWING_UNREADABLE = "DRAWING_UNREADABLE"
#: 超过 `MAX_DRAWING_BYTES`。
DRAWING_TOO_LARGE = "DRAWING_TOO_LARGE"
#: `.dwg` 或其他非 `.dxf` 扩展名 —— P3 读不了，需人工预转。
DRAWING_UNSUPPORTED_FORMAT = "DRAWING_UNSUPPORTED_FORMAT"
#: ezdxf 未安装。**前置条件缺失**，不是伪装成功，也不是文件坏了。
DRAWING_DEPENDENCY_MISSING = "DRAWING_DEPENDENCY_MISSING"


class DrawingEntityKind(str, Enum):
    """图元类型。

    只登记 P3 语义映射用得上的类型，其余归 ``OTHER`` —— 与其为每个 DXF 类型建一个
    枚举值（DXF 有上百种），不如让 ``OTHER`` 携带原始 ``dxftype`` 字符串，
    等 Step 21 确认真的需要时再提升为独立枚举值。
    """

    LINE = "line"
    POLYLINE = "polyline"
    CIRCLE = "circle"
    ARC = "arc"
    TEXT = "text"
    MTEXT = "mtext"
    INSERT = "insert"
    OTHER = "other"


class DrawingEntity(BaseModel):
    """单个图元。``handle`` + ``points`` 承担「能定位缺陷对象」的验收要求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: DXF handle（图纸内唯一，重新打开仍稳定）—— 缺陷定位的主键。
    handle: str
    kind: DrawingEntityKind
    #: 原始 DXF 类型名（如 ``LWPOLYLINE``）。``kind == OTHER`` 时靠它辨识。
    dxftype: str
    layer: str
    #: 顶点序列（模型空间坐标，二维）。LINE 2 个点，CIRCLE 1 个（圆心），TEXT 1 个（插入点）。
    points: tuple[tuple[float, float], ...] = ()
    radius: float | None = None
    #: 标注文本，**已过内容护栏**（MTEXT 已用 `plain_mtext` 归一）。非文本图元为空串。
    text: str = ""
    #: 来源标签（恒 ``retrieved_file``，见规格 §4.2）。
    source_tag: str = "retrieved_file"
    #: 内容护栏命中的规则 ID。**只带 ID，不带命中文本。**
    suspicious: tuple[str, ...] = ()


class DrawingExtraction(BaseModel):
    """一次图纸抽取的完整结果。``ok=False`` 时 ``entities`` 为空、``errors`` 非空。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    drawing_id: str
    #: 文件内容 sha256 前 16 位。**不含路径原文** —— `ContentEnvelope.safe_dict()`
    #: 会把 `reference_id` 原样落 trace，绝对路径进 trace 就是二次泄漏面。
    file_hash: str = ""
    dxf_version: str = ""
    #: ``$INSUNITS`` 原值（0 = unitless，缺失 = None）。
    insunits: int | None = None
    #: 单位是否已知。``$INSUNITS`` 缺失或为 0 时为 ``False`` ——
    #: 此时**不得**假定毫米或米（规格 §5.1，同 `UsageRecord.usage_known` 纪律）。
    unit_known: bool = False
    layers: tuple[str, ...] = ()
    entities: tuple[DrawingEntity, ...] = ()
    #: 因 ``max_entities`` 或返回字节上限而截断。
    truncated: bool = False
    #: 结构化错误码 + 说明（错误码取本模块常量）。
    errors: tuple[str, ...] = ()
    #: 需人工核对的假设（如单位未声明）。
    assumptions: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def error_codes(self) -> tuple[str, ...]:
        """从 ``errors`` 提取错误码（每条以 ``<CODE>: <detail>`` 开头）。"""
        return tuple(item.split(":", 1)[0].strip() for item in self.errors)

    def has_error(self, code: str) -> bool:
        return code in self.error_codes

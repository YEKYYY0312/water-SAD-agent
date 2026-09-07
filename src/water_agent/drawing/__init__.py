"""图纸读取与审查（P3）：DXF 解析 → 图元中间格式。

权威规格见 `docs/design/13-p3-drawing-review.md`。

**解析基线是 DXF，不是 DWG**：DWG→DXF 转换需要跑外部进程，即
`Capability.EXECUTE_SHELL` —— 该能力在 `Principal` 构造期即被拒绝，
故读 DWG **不可能做成模型可调用的工具**。`.dwg` 恒返回结构化
`DRAWING_UNSUPPORTED_FORMAT`，只能由人工预先转换。
"""

from water_agent.drawing.drawing_root import (
    DRAWING_SUFFIX,
    ENV_NAME,
    DrawingRootResult,
    resolve_drawing_root,
)
from water_agent.drawing.dxf_reader import (
    MAX_DRAWING_BYTES,
    read_drawing,
    resolve_drawing_path,
)
from water_agent.drawing.models import (
    DRAWING_DEPENDENCY_MISSING,
    DRAWING_NOT_FOUND,
    DRAWING_PATH_DENIED,
    DRAWING_TOO_LARGE,
    DRAWING_UNREADABLE,
    DRAWING_UNSUPPORTED_FORMAT,
    DrawingEntity,
    DrawingEntityKind,
    DrawingExtraction,
)

__all__ = [
    "DRAWING_DEPENDENCY_MISSING",
    "DRAWING_NOT_FOUND",
    "DRAWING_PATH_DENIED",
    "DRAWING_SUFFIX",
    "DRAWING_TOO_LARGE",
    "DRAWING_UNREADABLE",
    "DRAWING_UNSUPPORTED_FORMAT",
    "ENV_NAME",
    "MAX_DRAWING_BYTES",
    "DrawingEntity",
    "DrawingEntityKind",
    "DrawingExtraction",
    "DrawingRootResult",
    "read_drawing",
    "resolve_drawing_path",
    "resolve_drawing_root",
]

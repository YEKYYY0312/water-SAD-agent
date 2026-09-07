"""DRAWING_ROOT 图纸接入契约：路径解析与结构化状态。

图纸目录由环境变量 ``DRAWING_ROOT`` 指定（或通过 CLI ``--drawing-root`` 显式传入），
图纸文件固定位于 ``$DRAWING_ROOT/*.dxf``（**不递归子目录** —— 见下）。

原则（与 :mod:`water_agent.ingestion.data_root` 同源）
------------------------------------------------------
- 不硬编码任何用户目录；
- 不自动回退到项目 fixture 或临时目录；
- 所有失败都以结构化 :class:`DrawingRootResult` 返回，便于上层 CLI 输出可读 JSON。

P3 特有的两条纪律
-------------------
- **不递归子目录**：图纸登记号（``drawing_id``）与文件名一一对应且不含分隔符，
  这样模型永远无法通过登记号表达"某个子目录下的文件"。递归会重新引入路径歧义
  （两个子目录下的同名文件谁赢？），而歧义在路径判定里就是安全缺口。
- **只认 ``.dxf``**：``.dwg`` 不在此列出，因为 P3 读不了它
  （转换需 ``EXECUTE_SHELL``，该能力构造期即被拒绝）。发现 ``.dwg`` 时在
  ``message`` 里提示需人工预转，但**不**把它算作可用图纸。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DrawingRootStatus = Literal["NOT_CONFIGURED", "NOT_FOUND", "NO_DRAWING_FILES", "OK"]

ENV_NAME = "DRAWING_ROOT"

#: P3 唯一支持的图纸扩展名（小写比较）。
DRAWING_SUFFIX = ".dxf"

#: 读得到但**不支持**的扩展名 → 在 message 里提示人工预转，不计入可用图纸。
UNSUPPORTED_SUFFIXES = (".dwg",)


@dataclass(slots=True)
class DrawingRootResult:
    """DRAWING_ROOT 解析结果（结构化，可序列化为 JSON）。"""

    status: DrawingRootStatus
    drawing_root: str | None = None
    message: str = ""
    drawings: list[Path] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "drawing_root": self.drawing_root,
            "message": self.message,
            "drawings": [str(p) for p in self.drawings],
        }


def _unsupported_hint(root: Path) -> str:
    """扫描不支持的图纸格式，返回可读提示（无则空串）。"""
    found = sorted(
        p.name
        for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in UNSUPPORTED_SUFFIXES
    )
    if not found:
        return ""
    shown = "、".join(found[:3]) + ("…" if len(found) > 3 else "")
    return (
        f"；另发现 {len(found)} 个不支持的图纸（{shown}）——"
        f"P3 只读 {DRAWING_SUFFIX}，DWG 需人工转换后再放入"
    )


def resolve_drawing_root(explicit: str | Path | None = None) -> DrawingRootResult:
    """解析 DRAWING_ROOT：显式路径优先，否则读环境变量。

    - 未配置（env 缺失或为空）→ ``NOT_CONFIGURED``
    - 路径不存在或不是目录 → ``NOT_FOUND``
    - 目录下没有 ``*.dxf`` → ``NO_DRAWING_FILES``
    - 正常 → ``OK`` 并返回发现的图纸列表（按路径排序）

    ``NOT_CONFIGURED`` 是**前置条件缺失**而非失败：上层应据此不注册图纸工具
    （能力不可用就不暴露），而不是注册一个必然失败的工具。
    """
    if explicit is None:
        raw = os.environ.get(ENV_NAME, "")
        if not raw or not raw.strip():
            return DrawingRootResult(
                status="NOT_CONFIGURED",
                drawing_root=None,
                message=(
                    f"{ENV_NAME} 未配置（请通过环境变量 {ENV_NAME}=/path/to/drawings "
                    "或 --drawing-root 指定）"
                ),
            )
        raw = raw.strip()
    else:
        raw = str(explicit).strip()
        if not raw:
            return DrawingRootResult(
                status="NOT_CONFIGURED", drawing_root=None, message="--drawing-root 为空"
            )

    root = Path(raw).expanduser()
    if not root.is_dir():
        return DrawingRootResult(
            status="NOT_FOUND", drawing_root=raw, message=f"DRAWING_ROOT 路径不存在: {root}"
        )

    try:
        files = sorted(
            p for p in root.iterdir() if p.is_file() and p.suffix.lower() == DRAWING_SUFFIX
        )
        hint = _unsupported_hint(root)
    except OSError as exc:  # 权限不足等：报为结构化失败，不抛栈
        return DrawingRootResult(
            status="NOT_FOUND", drawing_root=raw, message=f"DRAWING_ROOT 无法枚举: {exc}"
        )

    if not files:
        return DrawingRootResult(
            status="NO_DRAWING_FILES",
            drawing_root=raw,
            message=f"目录下没有 {DRAWING_SUFFIX} 图纸: {root}{hint}",
        )

    return DrawingRootResult(
        status="OK",
        drawing_root=raw,
        message=f"发现 {len(files)} 个 {DRAWING_SUFFIX} 图纸{hint}",
        drawings=files,
    )

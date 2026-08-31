"""DATA_ROOT 数据接入契约：路径解析与结构化状态。

真实规范条款目录由环境变量 ``DATA_ROOT`` 指定（或通过 CLI ``--data-root`` 显式传入），
条款文件固定位于 ``$DATA_ROOT/clauses/*.jsonl``。

原则
-----
- 不硬编码任何用户目录；
- 不自动回退到 ``knowledge/`` 或项目 fixture；
- 所有失败都以结构化 :class:`DataRootResult` 返回，便于上层 CLI 输出可读 JSON。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DataRootStatus = Literal["NOT_CONFIGURED", "NOT_FOUND", "NO_CLAUSE_FILES", "OK"]

ENV_NAME = "DATA_ROOT"


@dataclass(slots=True)
class DataRootResult:
    """DATA_ROOT 解析结果（结构化，可序列化为 JSON）。"""

    status: DataRootStatus
    data_root: str | None = None
    message: str = ""
    clause_files: list[Path] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "data_root": self.data_root,
            "message": self.message,
            "clause_files": [str(p) for p in self.clause_files],
        }


def resolve_data_root(explicit: str | Path | None = None) -> DataRootResult:
    """解析 DATA_ROOT：显式路径优先，否则读环境变量。

    - 未配置（env 缺失或为空）→ ``NOT_CONFIGURED``
    - 路径不存在 → ``NOT_FOUND``
    - ``clauses/`` 不存在或没有 ``*.jsonl`` → ``NO_CLAUSE_FILES``
    - 正常 → ``OK`` 并返回发现的文件列表（按路径排序）
    """
    if explicit is None:
        raw = os.environ.get(ENV_NAME, "")
        if not raw or not raw.strip():
            return DataRootResult(
                status="NOT_CONFIGURED",
                data_root=None,
                message=f"{ENV_NAME} 未配置（请通过环境变量 {ENV_NAME}=/path/to/data-cleaning 或 --data-root 指定）",
            )
        raw = raw.strip()
    else:
        raw = str(explicit).strip()
        if not raw:
            return DataRootResult(status="NOT_CONFIGURED", data_root=None, message="--data-root 为空")

    root = Path(raw).expanduser()
    if not root.is_dir():
        return DataRootResult(status="NOT_FOUND", data_root=raw, message=f"DATA_ROOT 路径不存在: {root}")

    clauses_dir = root / "clauses"
    if not clauses_dir.is_dir():
        return DataRootResult(
            status="NO_CLAUSE_FILES",
            data_root=raw,
            message=f"clauses/ 目录不存在: {clauses_dir}",
        )

    files = sorted(p for p in clauses_dir.glob("*.jsonl") if p.is_file())
    if not files:
        return DataRootResult(
            status="NO_CLAUSE_FILES",
            data_root=raw,
            message=f"clauses/ 下没有 .jsonl 文件: {clauses_dir}",
        )

    return DataRootResult(
        status="OK",
        data_root=raw,
        message=f"发现 {len(files)} 个 JSONL 条款文件",
        clause_files=files,
    )

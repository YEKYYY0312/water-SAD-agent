"""语料与索引版本摘要（P2.5 Step 13）。

缓存键必须绑定「语料 manifest / 索引版本」（规格 §5）：条款更新后旧键必须失效。
本模块把 `data/curated/manifest.json` 与索引维度收敛为一个稳定字符串。

**为什么不直接用 manifest 的 `created_at`**：重建语料但内容未变时时间戳会变，
导致缓存被无谓清空。这里对**内容相关字段**取摘要（纳入的规范、记录数、
源文件清单），时间戳只在无法读取内容字段时兜底。

**fail closed**：manifest 缺失或不可解析时返回 :data:`CORPUS_VERSION_UNKNOWN`，
调用方须据此判定不可缓存 —— 不知道语料版本就不该复用旧答案。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

#: 语料版本不可判定时的标识。**不得**当作合法版本参与缓存键。
CORPUS_VERSION_UNKNOWN = "corpus_unknown"

_DEFAULT_MANIFEST = Path("data/curated/manifest.json")


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def corpus_version(manifest_path: Path | None = None) -> str:
    """语料版本摘要。

    取 manifest 的内容相关字段（`included_specs`、`records_included`、
    `source_files` 的 basename、`included_by_label`）做摘要。
    缺失/不可解析 → :data:`CORPUS_VERSION_UNKNOWN`。
    """
    path = manifest_path if manifest_path is not None else _DEFAULT_MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CORPUS_VERSION_UNKNOWN
    if not isinstance(data, dict):
        return CORPUS_VERSION_UNKNOWN

    included = data.get("included_specs")
    records = data.get("records_included")
    if included is None and records is None:
        # 内容字段都读不到 → 退回时间戳（可能过度失效，但不会错误复用）
        created = data.get("created_at")
        if isinstance(created, str) and created:
            return f"created_at:{_digest({'created_at': created})}"
        return CORPUS_VERSION_UNKNOWN

    sources = data.get("source_files")
    basenames = (
        sorted(Path(str(p)).name for p in sources) if isinstance(sources, list) else []
    )
    return _digest(
        {
            "included_specs": sorted(included) if isinstance(included, list) else included,
            "records_included": records,
            "source_files": basenames,
            "included_by_label": data.get("included_by_label"),
        }
    )


def index_version(*, embedding_model: str, dimensions: int) -> str:
    """索引版本：嵌入模型 + 维度。换模型或换维度必须让缓存失效。"""
    return f"{embedding_model}__{dimensions}"


def combined_corpus_version(
    *,
    embedding_model: str,
    dimensions: int,
    manifest_path: Path | None = None,
) -> str:
    """缓存键使用的合并语料版本：``<corpus_digest>|<index_version>``。

    语料不可判定时前缀为 :data:`CORPUS_VERSION_UNKNOWN`，调用方应判定不可缓存。
    """
    return f"{corpus_version(manifest_path)}|{index_version(embedding_model=embedding_model, dimensions=dimensions)}"


def corpus_version_known(version: str) -> bool:
    """该合并版本串是否可信（不含 unknown 前缀）。"""
    return not version.startswith(CORPUS_VERSION_UNKNOWN)


__all__ = [
    "CORPUS_VERSION_UNKNOWN",
    "combined_corpus_version",
    "corpus_version",
    "corpus_version_known",
    "index_version",
]

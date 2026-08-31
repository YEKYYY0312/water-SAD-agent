"""会话存储：会话 ID / 单一 sqlite 会话库 / 会话元数据。

设计（Step 3）：
- 会话存储统一为单库 ``data/sessions/sessions.sqlite``，用 LangGraph checkpointer 的
  ``thread_id`` 做会话隔离，进程内存态与磁盘持久化共用同一套 API。
- 进程内测试用 ``MemorySaver``；需要跨进程/重启恢复时用 ``SqliteSaver``（Step 3 已装）。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

DEFAULT_SESSION_DIR = Path(__file__).resolve().parents[2] / "data" / "sessions"


def new_session_id() -> str:
    """生成唯一会话 ID（uuid4 hex）。"""
    return uuid4().hex


def session_db_path(session_dir: str | Path = DEFAULT_SESSION_DIR) -> Path:
    """会话库路径：单库 ``<session_dir>/sessions.sqlite``。"""
    return Path(session_dir) / "sessions.sqlite"


def save_session_meta(
    session_id: str,
    *,
    session_dir: str | Path = DEFAULT_SESSION_DIR,
    query: str = "",
    status: str = "running",
    **extra,
) -> Path:
    """原子写入会话元数据（temp + rename），返回元数据文件路径。

    元数据只放会话事实（id / 首条 query / status / 时间），与消息正文分离；
    消息正文由 checkpointer 存 sqlite，不重复落 JSON。
    """
    root = Path(session_dir)
    meta_dir = root / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_path = meta_dir / f"{session_id}.json"
    payload = {
        "session_id": session_id,
        "query": query,
        "status": status,
        "created_at": datetime.now(UTC).isoformat(),
    }
    payload.update(extra)
    temporary = meta_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(meta_path)
    return meta_path
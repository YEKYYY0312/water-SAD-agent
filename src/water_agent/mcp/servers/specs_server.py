#!/usr/bin/env python3
"""规范库检索 MCP Server（P4 Step 24）。

把本地语料（``data/curated/clauses/*.jsonl`` + ``data/fixtures/rag_fixture.jsonl``
回退）包装为 3 个只读 MCP 工具：

- ``retrieve_clauses``：关键词检索（bigram 重叠，复用 InMemoryRetriever 口径）
- ``get_article``：按 ``spec_id + article`` 精确取条
- ``get_spec_info``：返回可用规范清单与版本（读 ``data/versions.json``，缺失不报错）

只读、无网络、无写入。语料在进程启动时一次性加载。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SERVER_INFO: dict[str, str] = {
    "name": "water-agent-specs",
    "version": "p4-step24-2026-09-09",
}

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CLAUSES_DIRS: list[Path] = [
    REPO_ROOT / "data" / "curated" / "clauses",
    REPO_ROOT / "data" / "fixtures",
]
VERSIONS_PATH = REPO_ROOT / "data" / "versions.json"

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "retrieve_clauses",
        "description": (
            "按关键词在本地规范库中检索条款，返回 top_k 条的 "
            "spec_id/article/title/text。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "检索关键词，如 设计秒流量",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                    "description": "返回条数 1-20",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_article",
        "description": "按规范号与条款号精确取条，返回单条 spec_id/article/title/text。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "规范号，如 GB50015-2019",
                },
                "article": {
                    "type": "string",
                    "minLength": 1,
                    "description": "条款号，如 3.2.1",
                },
            },
            "required": ["spec_id", "article"],
        },
    },
    {
        "name": "get_spec_info",
        "description": "返回本地可用规范清单与版本状态（只读，不触发网络）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def _load_clauses() -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    override = os.environ.get("WATER_AGENT_CLAUSES_PATH", "").strip()
    dirs: list[Path] = [Path(override)] if override else DEFAULT_CLAUSES_DIRS
    for directory in dirs:
        if not directory.exists():
            continue
        if directory.is_dir():
            files = sorted(directory.glob("*.jsonl"))
            if not files:
                continue
        else:
            files = [directory]
        for filepath in files:
            if not filepath.is_file():
                continue
            try:
                for line in filepath.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    if "spec_id" in obj and "article" in obj:
                        clauses.append(obj)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[specs_server] 跳过 {filepath}: {exc}", file=sys.stderr)
        if clauses:
            break
    return clauses


_CLAUSES: list[dict[str, Any]] = _load_clauses()
_CLAUSE_INDEX: dict[tuple[str, str], dict[str, Any]] = {
    (str(c.get("spec_id", "")), str(c.get("article", ""))): c for c in _CLAUSES
}


def _score(query: str, clause: dict[str, Any]) -> int:
    q = query.lower().strip()
    if not q:
        return 0
    text = f"{clause.get('title', '')} {clause.get('text', '')} {clause.get('article', '')}".lower()
    if len(q) < 2:
        return 5 if q in text else 0
    q_grams = {q[i : i + 2] for i in range(len(q) - 1)}
    t_grams = {text[i : i + 2] for i in range(len(text) - 1)}
    overlap = len(q_grams & t_grams)
    return overlap + (5 if q in text else 0)


def _tool_retrieve_clauses(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "")
    top_k = int(args.get("top_k") or 5)
    if not query.strip():
        return {
            "content": [{"type": "text", "text": "query 不能为空"}],
            "isError": True,
        }
    top_k = max(1, min(20, top_k))
    scored = [(_score(query, c), c) for c in _CLAUSES]
    scored = [(s, c) for s, c in scored if s > 0]
    scored.sort(key=lambda t: t[0], reverse=True)
    hits: list[dict[str, Any]] = []
    for _, clause in scored[:top_k]:
        hits.append(
            {
                "source_id": f"{clause.get('spec_id', '')} {clause.get('article', '')}".strip(),
                "spec_id": clause.get("spec_id", ""),
                "article": clause.get("article", ""),
                "title": clause.get("title", ""),
                "text": (clause.get("text", "") or "")[:2000],
            }
        )
    payload: dict[str, Any] = {"ok": True, "count": len(hits), "hits": hits}
    text = json.dumps(payload, ensure_ascii=False)
    if len(text.encode("utf-8")) > 10 * 1024:
        truncated_hits: list[dict[str, Any]] = list(hits)
        while truncated_hits and len(
            json.dumps(
                {**payload, "hits": truncated_hits, "truncated": True},
                ensure_ascii=False,
            ).encode("utf-8")
        ) > 10 * 1024:
            truncated_hits.pop()
        payload = {**payload, "hits": truncated_hits, "count": len(truncated_hits), "truncated": True}
        text = json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_get_article(args: dict[str, Any]) -> dict[str, Any]:
    spec_id = str(args.get("spec_id") or "").strip()
    article = str(args.get("article") or "").strip()
    if not spec_id or not article:
        return {
            "content": [{"type": "text", "text": "spec_id 与 article 均必填"}],
            "isError": True,
        }
    clause = _CLAUSE_INDEX.get((spec_id, article))
    if clause is None:
        return {
            "content": [{"type": "text", "text": f"未找到 {spec_id} {article}"}],
            "isError": True,
        }
    payload: dict[str, Any] = {
        "source_id": f"{spec_id} {article}",
        "spec_id": spec_id,
        "article": article,
        "title": clause.get("title", ""),
        "text": clause.get("text", ""),
        "chapter": clause.get("chapter"),
        "section": clause.get("section"),
    }
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_get_spec_info(_args: dict[str, Any]) -> dict[str, Any]:
    specs = sorted({str(c.get("spec_id", "")) for c in _CLAUSES if c.get("spec_id")})
    versions: Any = None
    try:
        if VERSIONS_PATH.exists():
            versions = json.loads(VERSIONS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 版本文件损坏时静默降级为空，不影响检索
        versions = None
    payload: dict[str, Any] = {"specs": specs, "total_clauses": len(_CLAUSES)}
    if isinstance(versions, dict):
        payload["versions"] = versions
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": False,
    }


_DISPATCH: dict[str, Any] = {
    "retrieve_clauses": _tool_retrieve_clauses,
    "get_article": _tool_get_article,
    "get_spec_info": _tool_get_spec_info,
}


def _send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    print(f"[specs_server] 已加载 {len(_CLAUSES)} 条条款", file=sys.stderr)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": ver,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            tools = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["inputSchema"],
                }
                for t in TOOL_DEFS
            ]
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            fn = _DISPATCH.get(str(name))
            if fn is None:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32602,
                            "message": f"未知工具 {name!r}"[:500],
                        },
                    }
                )
                continue
            try:
                result = fn(dict(args) if isinstance(args, dict) else {})
            except Exception as exc:  # noqa: BLE001 — 检索参数异常兜底为 isError，不崩协议
                result = {
                    "content": [{"type": "text", "text": f"工具执行异常：{exc}"}],
                    "isError": True,
                }
            _send({"jsonrpc": "2.0", "id": req_id, "result": result})
        elif req_id is not None:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"不支持的方法 {method!r}"},
                }
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Minimal read-only Memory MCP fixture for Step 46 adapter tests."""

from __future__ import annotations

import json
import sys

MEMORY_ID = "00000000-0000-0000-0000-000000000046"


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for raw in sys.stdin:
        if not raw.strip():
            continue
        message = json.loads(raw)
        method = message.get("method")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "protocolVersion": message["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-memory", "version": "0.0.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "tools": [
                            {
                                "name": "memory_search",
                                "description": "只读返回已授权记忆的 ID 排序提示",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "query": {"type": "string", "minLength": 1},
                                        "tenant_id": {"type": "string"},
                                        "user_id": {"type": "string"},
                                        "project_id": {"type": "string"},
                                        "limit": {"type": "integer", "minimum": 1, "default": 8},
                                    },
                                    "required": ["query", "tenant_id", "user_id"],
                                },
                            }
                        ]
                    },
                }
            )
        elif method == "tools/call":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": "ID-only hint"}],
                        "structuredContent": {
                            "hits": [{"memory_id": MEMORY_ID, "score": 0.75}]
                        },
                        "isError": False,
                    },
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

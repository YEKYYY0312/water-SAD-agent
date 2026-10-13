"""假的 OpenAI 兼容对话端点（P7-S10 兼容性探针与门槛的离线测试用，进程内线程，明文回环）。

只实现探针与装配用得到的那部分 ``POST <base>/chat/completions``：

- ``Authorization: Bearer <key>`` 必须等于构造时给的 Key，否则 401（并记下收到的 Key，供"密钥只发给
  所属服务商"的断言用）；
- 带 ``tools`` 的请求回一个对第一个工具的调用（``calc_velocity`` / ``cite_clauses`` 的参数是探针提示里写的值）；
- ``stream: true`` 回 SSE；``stream_options.include_usage`` 时末块带 usage；长提示（取消检查）逐块慢慢发，
  客户端提前断开时记一次 ``disconnects``；
- 模型名不在 ``models`` 里回 404 ``model_not_found``。

``modes`` 用来模拟不合格的端点：``no_tools``（不会 function calling，只回文本）、``no_stream_usage``（流式末块
不带 usage）、``accept_unknown_model``（不认识的模型名也悄悄回成功）。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

TOOL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "calc_velocity": {"flow_lps": 20, "diameter_mm": 150, "scenario": "water_supply"},
    "cite_clauses": {"article_ids": ["GB50015-2019 4.7.1"]},
    "retrieve_clauses": {"query": "给水管道设计秒流量", "top_k": 5},
}
LONG_STREAM_CHUNKS = 400


class FakeOpenAIServer:
    def __init__(
        self,
        *,
        api_key: str = "sk-fake-endpoint-key-0001",
        models: Iterable[str] = ("fake-model",),
        modes: Iterable[str] = (),
    ) -> None:
        self.api_key = api_key
        self.models = set(models)
        self.modes = set(modes)
        self.received_keys: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.disconnects = 0
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                key = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                with server._lock:
                    server.received_keys.append(key)
                    server.requests.append(
                        {
                            "path": self.path,
                            "model": body.get("model"),
                            "stream": bool(body.get("stream")),
                            "tools": [
                                t.get("function", {}).get("name") for t in body.get("tools") or []
                            ],
                        }
                    )
                if not self.path.endswith("/chat/completions"):
                    self._json(404, {"error": {"message": "not found"}})
                    return
                if key != server.api_key:
                    self._json(
                        401, {"error": {"message": "invalid api key", "code": "invalid_api_key"}}
                    )
                    return
                model = str(body.get("model") or "")
                if model not in server.models and "accept_unknown_model" not in server.modes:
                    self._json(
                        404,
                        {"error": {"message": "model does not exist", "code": "model_not_found"}},
                    )
                    return
                if body.get("stream"):
                    self._stream(body, model)
                else:
                    self._json(200, server._completion(body, model))

            def _stream(self, body: dict[str, Any], model: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                prompt = json.dumps(body.get("messages") or [], ensure_ascii=False)
                pieces = ["要点"] * LONG_STREAM_CHUNKS if "二十个要点" in prompt else ["2"]
                include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
                try:
                    for index, piece in enumerate(pieces):
                        self._event(
                            server._chunk(
                                model,
                                {"content": piece}
                                if index
                                else {"role": "assistant", "content": piece},
                            )
                        )
                        if len(pieces) > 1:
                            time.sleep(0.01)
                    self._event(server._chunk(model, {}, finish_reason="stop"))
                    if include_usage and "no_stream_usage" not in server.modes:
                        self._event(
                            {
                                "id": "chatcmpl-fake",
                                "object": "chat.completion.chunk",
                                "created": 0,
                                "model": model,
                                "choices": [],
                                "usage": {
                                    "prompt_tokens": 12,
                                    "completion_tokens": len(pieces),
                                    "total_tokens": 12 + len(pieces),
                                },
                            }
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    with server._lock:
                        server.disconnects += 1

            def _event(self, payload: dict[str, Any]) -> None:
                self.wfile.write(
                    b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"
                )
                self.wfile.flush()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def start(self) -> Self:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @staticmethod
    def _chunk(
        model: str, delta: dict[str, Any], *, finish_reason: str | None = None
    ) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def _completion(self, body: dict[str, Any], model: str) -> dict[str, Any]:
        tools = body.get("tools") or []
        message: dict[str, Any]
        if tools and "no_tools" not in self.modes:
            name = tools[0].get("function", {}).get("name", "")
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_fake_1",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(TOOL_ARGUMENTS.get(name, {})),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "2"}
            finish = "stop"
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        }


def write_passing_report(path: Any, *, endpoint: str, model: str, **overrides: Any) -> Any:
    """写一份针对 ``endpoint`` + ``model`` 的"全部通过"报告（门槛测试用；``overrides`` 用来造反例）。"""

    from water_agent.generation.compatibility import (
        ENDPOINT_COMPATIBILITY_CHECKS,
        ENDPOINT_PROBE_VERSION,
        CompatibilityReport,
        write_report,
    )

    fields: dict[str, Any] = {
        "version": ENDPOINT_PROBE_VERSION,
        "status": "compatible",
        "model": model,
        "endpoint": endpoint,
        "checks": {name: "passed" for name in ENDPOINT_COMPATIBILITY_CHECKS},
        "schema_tokens": 1000,
        "evidence": {"probe_version": ENDPOINT_PROBE_VERSION},
    }
    fields.update(overrides)
    return write_report(path, CompatibilityReport(**fields))

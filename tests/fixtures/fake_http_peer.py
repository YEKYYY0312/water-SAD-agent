"""进程内假 HTTP 对端（P7-S11）：``threading.Thread`` + ``http.server``，**不起子进程**（S8 决策记录 D13：
评测 / 快照 harness 里起子进程会新增"子进程调用点"并被治理扫描盯上）。

记录收到的**请求数与路径**——"零字节发往某地址"靠它断言。模式（``mode``）：

- ``ok``            200 + ``{"ok": true}``（``application/json``）
- ``redirect``      302 → ``Location``（指向另一个回环端口，用来证明不跟随）
- ``oversize``      200 + 超过上限的响应体
- ``status_NNN``    返回该状态码，响应体里带"上游正文 / 网址 / 密钥"样的字符串（用来证明错误文本不回显）
- ``slow``          先睡 ``delay`` 秒再回
- ``wrong_ctype``   200 + ``text/html``
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

UPSTREAM_SECRET_BODY = "upstream-body-SECRET-TOKEN https://upstream.internal/leak?key=sk-live-abcdef0123456789"


class FakeHttpPeer:
    def __init__(self, mode: str = "ok", *, redirect_to: str = "", oversize_bytes: int = 2_000_000, delay: float = 0.0) -> None:
        self.mode = mode
        self.redirect_to = redirect_to
        self.oversize_bytes = oversize_bytes
        self.delay = delay
        self.requests: list[str] = []
        self.auth_headers: list[str | None] = []
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # 静默
                return

            def do_GET(self) -> None:
                peer.requests.append(self.path)
                peer.auth_headers.append(self.headers.get("Authorization"))
                mode = peer.mode
                if mode == "slow":
                    time.sleep(peer.delay)
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", peer.redirect_to)
                    self.end_headers()
                    return
                if mode.startswith("status_"):
                    code = int(mode.split("_", 1)[1])
                    body = UPSTREAM_SECRET_BODY.encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if mode == "oversize":
                    body = b"x" * peer.oversize_bytes
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except OSError:
                        pass
                    return
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html" if mode == "wrong_ctype" else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

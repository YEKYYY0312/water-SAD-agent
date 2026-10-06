"""MCP stdio 客户端（P4 Step 23）：子进程生命周期 + JSON-RPC 请求/响应。

纯标准库实现（`subprocess` + `json` + `queue` + `selectors`），不依赖 ``mcp``
SDK —— 环境实测无网络无法安装（决策记录 D1）。只覆盖协议面（规格 §2.2）：

- 生命周期：``connect()``（initialize 握手 + initialized 通知）→ ``list_tools()``
  （可翻页，返回 :class:`RemoteTool`）→ ``call_tool()`` → ``close()``；
- server 主动请求回 ``MethodNotFound``；通知忽略但可观测记录；
- 单次调用 deadline（``call_timeout_seconds`` 或显式 ``deadline_seconds``）、
  响应字节上限（``max_response_bytes``）、EOF/崩溃转为结构化错误；
- **错误语义分层**：远端业务失败（``isError=true``）返回结构化 dict
  （``ok=False``）；连接/协议/超时/超限抛 :class:`MCPError`
  （崩溃类由调用方 —— ``ToolRegistry.invoke``/``open_bridge`` 兜底）。

**进程边界**：``command``/``args``/``env`` 全部来自人工配置（装配期），模型的
任何输入都不能影响它们（规格 §5）。list 形式 spawn，无 shell、无字符串插值。
"""
from __future__ import annotations

import io
import queue
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field

from water_agent.mcp.config import MCPServerConfig, child_env
from water_agent.mcp.errors import (
    MCP_CONNECT_FAILED,
    MCP_NOT_INITIALIZED,
    MCP_PROTOCOL_ERROR,
    MCP_RESPONSE_TOO_LARGE,
    MCP_TIMEOUT,
    MCP_TOOL_REMOTE_ERROR,
    MCPError,
)
from water_agent.mcp.protocol import (
    CLIENT_INFO,
    JSONRPC_METHOD_NOT_FOUND,
    MCP_PROTOCOL_VERSION,
    RpcMessage,
    decode_message,
    encode_message,
    make_error_response,
    make_notification,
    make_request,
    parse_tool_call_result,
)

#: tools/list 分页上限。超出视为清单异常（MCP_PROTOCOL_ERROR），
#: 防止 server 用无限 nextCursor 拖死客户端。
MAX_LIST_PAGES = 20

#: stderr 采集上限（字节）。只保留尾部，防 server 刷屏打爆内存。
MAX_STDERR_TAIL_BYTES = 16 * 1024

_STDOUT_CHUNK = 4096


class RemoteTool(BaseModel):
    """server 清单里的一个工具（``tools/list`` 的受检条目）。

    描述与 schema 内嵌描述都是**不可信文本**，只在这里做 shape 校验；
    内容安检在桥接层（``schema.ensure_description_safe``）完成 —— 本模型
    不该被当成"已安检"的证据。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


@dataclass(slots=True)
class StdioProcess:
    """子进程句柄 + 读取线程的共享状态。"""

    proc: subprocess.Popen[bytes]
    inbox: queue.Queue[dict[str, Any] | None]
    #: 读取线程遇到的不可恢复错误（协议/超限）：随 EOF 哨兵一并上报。
    reader_error: MCPError | None = None
    #: 读取线程已结束（EOF / 崩溃 / 协议错误）。之后这条连接不可再用（P7-S5 健康检查）。
    reader_done: bool = False
    stderr_lines: list[bytes] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _stderr_tail(handle: StdioProcess, limit: int = 300) -> str:
    with handle.lock:
        tail = b"".join(handle.stderr_lines).decode("utf-8", "replace")
    return tail[-limit:] if tail else ""


class MCPStdioClient:
    """一个 stdio MCP server 的连接。

    用法（``with`` 或显式 ``connect``/``close``）：

    .. code-block:: python

        client = MCPStdioClient(server_config)
        try:
            client.connect()
            tools = client.list_tools()
            result = client.call_tool("query", {...})
        finally:
            client.close()
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.server_info: dict[str, Any] | None = None
        self._proc: StdioProcess | None = None
        self._connected = False
        self._next_id = 0
        self._write_lock = threading.Lock()
        #: 请求锁（P7-S5）：同一连接上的 JSON-RPC 请求串行。"发请求 → 等匹配 id 的响应"整段
        #: 在锁内——此前各请求共用一个 inbox，读到别人的响应时当成通知存起来继续等，真正的
        #: 请求方只能超时（server 乱序应答时必然发生）。
        self._request_lock = threading.Lock()
        self._notifications: list[dict[str, Any]] = []
        #: 本连接自己建的临时工作目录（配置没给 ``cwd`` 时）；连接关闭时删除。
        self._scratch_dir: str | None = None

    # ------------------------------------------------------------------ 连接

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def alive(self) -> bool:
        """连接仍可用：已握手、子进程未退出、读取线程未结束（P7-S5 连接池的健康检查）。"""
        handle = self._proc
        return (
            self._connected
            and handle is not None
            and handle.proc.poll() is None
            and not handle.reader_done
        )

    @property
    def notifications(self) -> tuple[RpcMessage, ...]:
        """收到的 server 通知（可观测；本网关不响应它们）。"""
        return tuple(RpcMessage(m) for m in self._notifications)

    def _working_directory(self) -> str:
        """子进程工作目录（P7-S3，F15/F16）。

        配置给了 ``cwd`` 就用它；没给就为本连接新建一个空临时目录，**不继承 agent 自己的工作
        目录**——模型能借 MCP 工具参数里的相对路径读文件，而护栏刻意不管相对路径。这不是沙箱：
        server 进程照样能读进程用户能读的一切，隔离靠容器；这里只关掉"相对路径解析到 agent
        仓库"这一条缝。
        """
        if self.config.cwd is not None:
            return self.config.cwd
        if self._scratch_dir is None:
            self._scratch_dir = tempfile.mkdtemp(prefix="wa-mcp-")
        return self._scratch_dir

    def _remove_scratch_dir(self) -> None:
        scratch, self._scratch_dir = self._scratch_dir, None
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    def _spawn(self) -> StdioProcess:
        command = [self.config.command, *self.config.args]
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env(self.config),
                cwd=self._working_directory(),
                text=False,
            )
        except (OSError, MCPError) as exc:
            self._remove_scratch_dir()
            if isinstance(exc, MCPError):
                raise
            raise MCPError(
                MCP_CONNECT_FAILED,
                f"无法启动 MCP server {self.config.server_id!r}"
                f"（{self.config.command}）：{exc}",
            ) from exc

        handle = StdioProcess(proc=proc, inbox=queue.Queue())
        threading.Thread(
            target=self._read_loop,
            args=(handle,),
            name=f"mcp-stdout-{self.config.server_id}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._drain_stderr,
            args=(handle,),
            name=f"mcp-stderr-{self.config.server_id}",
            daemon=True,
        ).start()
        return handle

    def _drain_stderr(self, handle: StdioProcess) -> None:
        """持续读 stderr，只保留尾部（防刷屏）。EOF 时静默退出。"""
        stream = cast(io.BufferedReader, handle.proc.stderr)
        try:
            # read1：单次底层读，返回**当前可用**数据，不等到满块或 EOF
            # （阻塞式 read(n) 会等满 n 字节，把本线程挂死在短输出上）。
            for chunk in iter(lambda: stream.read1(_STDOUT_CHUNK), b""):
                with handle.lock:
                    handle.stderr_lines.append(chunk)
                    while sum(len(line) for line in handle.stderr_lines) > MAX_STDERR_TAIL_BYTES:
                        handle.stderr_lines.pop(0)
        except (OSError, ValueError):
            pass

    def _read_loop(self, handle: StdioProcess) -> None:
        """读取 stdout 行 → 解码 → 入队。

        EOF / 不可恢复错误时置 ``reader_error``（如有）并入队 ``None`` 哨兵。
        **绝不向线程外抛异常**：线程崩溃的痕迹只在队列里。
        """
        stream = cast(io.BufferedReader, handle.proc.stdout)
        sel = selectors.DefaultSelector()
        sel.register(stream, selectors.EVENT_READ)
        buffer = bytearray()
        limit = self.config.max_response_bytes

        def _abort(error: MCPError | None) -> None:
            handle.reader_error = error
            handle.inbox.put(None)

        try:
            while True:
                events = sel.select(timeout=0.2)
                if not events:
                    if handle.proc.poll() is not None and not buffer:
                        break
                    continue
                chunk = stream.read1(_STDOUT_CHUNK)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    if len(line) > limit:
                        _abort(
                            MCPError(
                                MCP_RESPONSE_TOO_LARGE,
                                f"MCP server {self.config.server_id!r} 单行响应 "
                                f"{len(line)} 字节超过上限 {limit}",
                            )
                        )
                        return
                    try:
                        handle.inbox.put(decode_message(bytes(line)))
                    except MCPError as exc:
                        _abort(exc)
                        return
                if len(buffer) > limit:
                    _abort(
                        MCPError(
                            MCP_RESPONSE_TOO_LARGE,
                            f"MCP server {self.config.server_id!r} 响应超过上限 {limit} 字节",
                        )
                    )
                    return
        except (OSError, ValueError):
            pass
        finally:
            try:
                sel.unregister(stream)
            except (OSError, ValueError, KeyError):
                pass
            sel.close()
            handle.reader_done = True
            handle.inbox.put(None)

    def connect(self) -> dict[str, Any]:
        """启动子进程并完成 initialize 握手（幂等：已连接则直接返回）。"""
        if self._connected:
            assert self.server_info is not None
            return self.server_info

        handle = self._spawn()
        self._proc = handle
        try:
            response = self._request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
                deadline=self.config.startup_timeout_seconds,
            )
        except MCPError as exc:
            self._terminate()
            if exc.code in (MCP_RESPONSE_TOO_LARGE, MCP_PROTOCOL_ERROR):
                # 协议级失败本身已精确可诊断，保持原码 —— 包成"连接失败"
                # 会把"响应超限"与"server 挂了"混为一谈，排障时误导。
                raise
            raise MCPError(
                MCP_CONNECT_FAILED,
                f"MCP server {self.config.server_id!r} initialize 失败：{exc.message}",
            ) from exc

        if not isinstance(response, dict) or not isinstance(response.get("serverInfo"), dict):
            self._terminate()
            raise MCPError(
                MCP_CONNECT_FAILED,
                f"MCP server {self.config.server_id!r} initialize 响应缺少 serverInfo",
            )
        # 握手后回 initialized 通知；通知失败不算连接失败（server 仍可应答请求）
        try:
            self._send(make_notification("notifications/initialized"))
        except MCPError:
            pass
        self._connected = True
        assert isinstance(response["serverInfo"], dict)
        self.server_info = response["serverInfo"]
        return response

    # ------------------------------------------------------------------ 请求

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.proc.stdin is None:
            raise MCPError(MCP_NOT_INITIALIZED, "MCP server 未连接，无法发送消息")
        data = encode_message(message)
        try:
            with self._write_lock:
                proc.proc.stdin.write(data.encode("utf-8"))
                proc.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPError(
                MCP_CONNECT_FAILED,
                f"MCP server {self.config.server_id!r} 写入失败（进程可能已退出）：{exc}",
            ) from exc

    def _request(self, method: str, params: dict[str, Any] | None, *, deadline: float) -> Any:
        """发送请求并等待**匹配 id** 的响应；忽略通知、回绝 server 请求。

        同一连接上的请求串行（``_request_lock``）；等锁的时间计入 ``deadline``——前一个调用
        占着连接时，后一个调用按自己的 deadline 超时并说明 server 正忙，不无限排队。
        """
        deadline_at = time.monotonic() + deadline
        if not self._request_lock.acquire(timeout=max(deadline, 0.0)):
            raise MCPError(
                MCP_TIMEOUT,
                f"MCP server {self.config.server_id!r} 正忙：等待前一个调用超过 {deadline:.1f}s",
            )
        try:
            return self._request_locked(method, params, deadline=deadline, deadline_at=deadline_at)
        finally:
            self._request_lock.release()

    def _request_locked(
        self, method: str, params: dict[str, Any] | None, *, deadline: float, deadline_at: float
    ) -> Any:
        proc = self._proc
        if proc is None:
            raise MCPError(MCP_NOT_INITIALIZED, f"MCP server {self.config.server_id!r} 未连接")
        if proc.reader_done and proc.inbox.empty():
            # 读取线程已结束、EOF 哨兵也已被取走：不再空等到超时
            raise MCPError(
                MCP_CONNECT_FAILED,
                f"MCP server {self.config.server_id!r} 连接已关闭（进程退出或协议错误）",
            )
        self._next_id += 1
        request_id = self._next_id
        self._send(make_request(request_id, method, params))

        while True:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise MCPError(
                    MCP_TIMEOUT,
                    f"MCP server {self.config.server_id!r} {method!r} 调用超过 {deadline:.1f}s",
                )
            message = RpcMessage(self._wait_for_message(remaining))
            if message.message_id != request_id:
                if message.is_request:
                    # 本网关不支持任何 server→client 请求（sampling/roots 等），
                    # 回 MethodNotFound，不让 server 侧挂起。
                    self._send(make_error_response(message.message_id, JSONRPC_METHOD_NOT_FOUND, "unsupported server-to-client request"))
                else:
                    self._notifications.append(message.payload)
                continue
            if message.error is not None:
                raise MCPError(MCP_TOOL_REMOTE_ERROR, message.error.get("message", "unknown error"))
            return message.result

    def _wait_for_message(self, timeout_seconds: float) -> dict[str, Any]:
        """从 inbox 取一条原始消息（dict）；EOF 哨兵/超时转结构化错误。"""
        proc = self._proc
        assert proc is not None
        try:
            payload = proc.inbox.get(timeout=timeout_seconds)
        except queue.Empty:
            raise MCPError(
                MCP_TIMEOUT,
                f"MCP server {self.config.server_id!r} 无响应",
            ) from None
        if payload is None:
            if proc.reader_error is not None:
                raise proc.reader_error
            tail = _stderr_tail(proc)
            detail = f"；stderr 尾部：{tail}" if tail else ""
            raise MCPError(
                MCP_CONNECT_FAILED,
                f"MCP server {self.config.server_id!r} 连接提前关闭（EOF/崩溃）{detail}",
            )
        return payload

    def list_tools(self) -> list[RemoteTool]:
        """拉取完整工具清单（自动翻页），返回 :class:`RemoteTool` 列表。

        清单项 shape 在这里校验（name 必须是 string）；描述/schema 的**内容**
        安检在桥接层 —— 本方法不做内容判定。
        """
        tools: list[RemoteTool] = []
        cursor: str | None = None
        for _page in range(MAX_LIST_PAGES):
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result = self._request(
                "tools/list", params, deadline=self.config.call_timeout_seconds
            )
            if not isinstance(result, dict):
                raise MCPError(
                    MCP_PROTOCOL_ERROR,
                    f"tools/list 结果必须是 object，得到 {type(result).__name__}",
                )
            page = result.get("tools")
            if not isinstance(page, list):
                raise MCPError(MCP_PROTOCOL_ERROR, "tools/list 结果缺少 tools 数组")
            for item in page:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise MCPError(
                        MCP_PROTOCOL_ERROR, "tools/list 存在非法工具项（缺 string name）"
                    )
                raw_description = item.get("description")
                raw_schema = item.get("inputSchema")
                tools.append(
                    RemoteTool(
                        name=item["name"],
                        description=raw_description if isinstance(raw_description, str) else "",
                        input_schema=raw_schema if isinstance(raw_schema, dict) else {"type": "object", "properties": {}},
                    )
                )
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return tools
            if not isinstance(next_cursor, str):
                raise MCPError(MCP_PROTOCOL_ERROR, "nextCursor 必须是 string")
            cursor = next_cursor
        raise MCPError(
            MCP_PROTOCOL_ERROR, f"tools/list 分页超过 {MAX_LIST_PAGES} 页，视为异常清单"
        )

    def call_tool(
        self, name: str, arguments: dict[str, Any], *, deadline_seconds: float | None = None
    ) -> dict[str, Any]:
        """调用远程工具。

        - 远端业务失败（``isError=true``）→ 结构化 dict ``{"ok": False,
          "code": MCP_TOOL_REMOTE_ERROR, ...}``（业务失败不是异常）；
        - 连接/协议/超时/超限 → 抛 :class:`MCPError`（崩溃类，由调用方兜底）。
        """
        if not self._connected:
            raise MCPError(
                MCP_NOT_INITIALIZED,
                f"MCP server {self.config.server_id!r} 未完成握手，不能调用工具",
            )
        result = self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            deadline=deadline_seconds or self.config.call_timeout_seconds,
        )
        parsed = parse_tool_call_result(result)
        if parsed.is_error:
            detail = "; ".join(parsed.texts)[:500] if parsed.texts else "server 未给出原因"
            return {
                "ok": False,
                "server_id": self.config.server_id,
                "tool": name,
                "code": MCP_TOOL_REMOTE_ERROR,
                "message": f"远程工具 {name!r} 执行失败：{detail}",
            }
        return {
            "ok": True,
            "server_id": self.config.server_id,
            "tool": name,
            "structured_content": parsed.structured_content,
            "content_text": "\n".join(parsed.texts),
            "unsupported_blocks": list(parsed.unsupported),
        }

    # ------------------------------------------------------------------ 关闭

    def _terminate(self) -> None:
        handle = self._proc
        self._proc = None
        self._connected = False
        if handle is None:
            self._remove_scratch_dir()
            return
        proc = handle.proc
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._remove_scratch_dir()

    def close(self) -> None:
        """终止子进程并释放资源。幂等。"""
        self._terminate()

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = [
    "MAX_LIST_PAGES",
    "MAX_STDERR_TAIL_BYTES",
    "MCPStdioClient",
    "RemoteTool",
    "StdioProcess",
]

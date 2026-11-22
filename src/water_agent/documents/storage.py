"""上传与导出文件的落盘位置（P5-S48 Step 1/4）。

**为什么不是 ``data/`` 下面**：镜像把 ``/app/data`` 打成只读（``Dockerfile`` 里
``chmod -R a-w``，实测 ``dr-xr-xr-x``），任何写进那里的路径在容器中必然
``OSError``。可写位置是 compose 卷 ``app_uploads:/var/lib/water-agent/uploads`` 与
``app_exports:/var/lib/water-agent/exports``（spec D1 的口径）。执行 prompt 里写的
``data/projects/{project_id}/uploads/`` 是错的，按 spec 与容器现实修正。

解析顺序与 ``observability/json_trace.py::resolve_trace_dir()`` 一致：
显式参数 → 环境变量 → 卷路径（存在且可写）→ 仓库内开发兜底。

上传和临时导出沿用 **0644 文件 / 0755 目录**。持久化导出输入由服务进程自己创建，
单独放在 **0700 目录 / 0600 文件**，不与跨用户传输的上传暂存权限混用。
"""

from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

UPLOAD_DIR_ENV = "WATER_AGENT_UPLOAD_DIR"
EXPORT_DIR_ENV = "WATER_AGENT_EXPORT_DIR"

#: compose 卷挂载点（Dockerfile 里已建目录并 chown 给 wateragent）。
VOLUME_UPLOAD_DIR = Path("/var/lib/water-agent/uploads")
VOLUME_EXPORT_DIR = Path("/var/lib/water-agent/exports")

REPO_ROOT = Path(__file__).resolve().parents[3]
#: 本机开发兜底。仓库里 ``data/`` 可写（只有镜像内才只读），且 ``.dockerignore``
#: 不重新包含这两个子目录，所以它们永远不会被打进镜像。
DEV_UPLOAD_DIR = REPO_ROOT / "data" / "uploads"
DEV_EXPORT_DIR = REPO_ROOT / "data" / "exports"

FILE_MODE = 0o644
DIR_MODE = 0o755


def _is_writable_dir(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


def _resolve(
    explicit: str | os.PathLike[str] | None,
    *,
    env_name: str,
    volume_dir: Path,
    dev_dir: Path,
    getenv: Callable[[str], str | None],
) -> Path:
    if explicit is not None and str(explicit) != "":
        return Path(explicit)
    configured = (getenv(env_name) or "").strip()
    if configured:
        return Path(configured)
    # 只在卷真的挂上且可写时才用它。没挂卷的本机运行不该把文件写到
    # /var/lib（大概率 permission denied，且用户找不到东西在哪）。
    if _is_writable_dir(volume_dir):
        return volume_dir
    return dev_dir


def resolve_upload_dir(
    explicit: str | os.PathLike[str] | None = None,
    *,
    getenv: Callable[[str], str | None] = os.environ.get,
) -> Path:
    """上传根目录：显式参数 → ``WATER_AGENT_UPLOAD_DIR`` → 卷 → ``data/uploads``。"""

    return _resolve(
        explicit,
        env_name=UPLOAD_DIR_ENV,
        volume_dir=VOLUME_UPLOAD_DIR,
        dev_dir=DEV_UPLOAD_DIR,
        getenv=getenv,
    )


def resolve_export_dir(
    explicit: str | os.PathLike[str] | None = None,
    *,
    getenv: Callable[[str], str | None] = os.environ.get,
) -> Path:
    """导出根目录：显式参数 → ``WATER_AGENT_EXPORT_DIR`` → 卷 → ``data/exports``。"""

    return _resolve(
        explicit,
        env_name=EXPORT_DIR_ENV,
        volume_dir=VOLUME_EXPORT_DIR,
        dev_dir=DEV_EXPORT_DIR,
        getenv=getenv,
    )


class ExportSnapshotStore:
    """Private, bounded per-run results. No credentials, messages or prompts."""

    max_bytes = 4 * 1024 * 1024

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    @staticmethod
    def _uuid(value: Any) -> str:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError("invalid identifier")
        return value

    @staticmethod
    def _number(value: Any) -> bool:
        try:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            return False

    @staticmethod
    def _strings(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(v, str) for v in value)

    @classmethod
    def _numbers(cls, value: Any) -> bool:
        return isinstance(value, dict) and all(isinstance(k, str) and cls._number(v) for k, v in value.items())

    @classmethod
    def _validate_material(cls, table: Any) -> None:
        if table is None:
            return
        if not isinstance(table, dict) or set(table) != {"rows", "total_length_m", "assumptions"}:
            raise ValueError("invalid material fields")
        if not cls._number(table["total_length_m"]) or table["total_length_m"] < 0 or not cls._strings(table["assumptions"]) or not isinstance(table["rows"], list):
            raise ValueError("invalid material table")
        allowed = {"material", "spec", "unit", "quantity", "raw_quantity", "segments", "detail"}
        for row in table["rows"]:
            if (
                not isinstance(row, dict) or set(row) - allowed or not {"material", "quantity"} <= set(row)
                or not isinstance(row["material"], str) or not row["material"]
                or not cls._number(row["quantity"]) or row["quantity"] < 0
                or not cls._number(row.get("raw_quantity", row["quantity"]))
                or any(not isinstance(row.get(k, ""), str) for k in ("spec", "unit", "detail"))
                or not cls._strings(row.get("segments", []))
            ):
                raise ValueError("invalid material row")

    @classmethod
    def _sanitize(cls, value: Any, depth: int = 0) -> Any:
        if depth > 32:
            raise ValueError("export content nesting limit")
        if isinstance(value, str):
            from water_agent.runtime.governance.pipeline import default_guardrail_pipeline
            return default_guardrail_pipeline().sanitize_for_persistence(value)[0]
        if isinstance(value, list):
            return [cls._sanitize(v, depth + 1) for v in value]
        if isinstance(value, dict):
            if any(not isinstance(k, str) or cls._sanitize(k, depth + 1) != k for k in value):
                raise ValueError("sensitive or invalid dictionary key")
            return {k: cls._sanitize(v, depth + 1) for k, v in value.items()}
        return value

    @classmethod
    def sanitize_snapshot(cls, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {k: v if k == "run_id" else cls._sanitize(v) for k, v in snapshot.items()}

    @classmethod
    def _private_record(cls, data: dict[str, Any]) -> dict[str, Any]:
        safe = {
            **data, "snapshot": cls.sanitize_snapshot(data["snapshot"]),
            "finalize": {**data["finalize"], "payload": cls._sanitize(data["finalize"]["payload"])},
        }
        return cls._validate(safe)

    @classmethod
    def _validate_calculations(cls, calculations: Any) -> None:
        if not isinstance(calculations, list):
            raise TypeError("invalid calculations")
        for calc in calculations:
            if (
                not isinstance(calc, dict)
                or set(calc) != {"tool_name", "programmatic", "values", "units", "assumptions"}
                or calc["programmatic"] is not True or not isinstance(calc["tool_name"], str)
                or not cls._numbers(calc["values"]) or not cls._strings(calc["assumptions"])
                or not isinstance(calc["units"], dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in calc["units"].items())
            ):
                raise ValueError("invalid programmatic calculation")

    @classmethod
    def _validate(cls, data: Any, run_id: str | None = None) -> dict[str, Any]:
        if not isinstance(data, dict) or set(data) != {"version", "owner_user_id", "snapshot", "finalize"}:
            raise ValueError("invalid snapshot")
        if type(data["version"]) is int and data["version"] == 2:
            from water_agent.multi_agent.project_publication import validate_snapshot_v2
            return validate_snapshot_v2(data, run_id)
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("unsupported snapshot version")
        cls._uuid(data["owner_user_id"])
        s, event = data["snapshot"], data["finalize"]
        keys = {"run_id", "status", "answer", "citations", "calculations", "calc_inputs", "material_table"}
        if not isinstance(s, dict) or set(s) != keys:
            raise ValueError("invalid snapshot fields")
        identity = cls._uuid(s["run_id"])
        if run_id is not None and identity != run_id:
            raise ValueError("snapshot identity mismatch")
        if s["status"] != "succeeded" or not isinstance(s["answer"], str):
            raise ValueError("snapshot is not a successful result")
        if not isinstance(s["citations"], list) or not isinstance(s["calculations"], list) or not isinstance(s["calc_inputs"], dict):
            raise TypeError("invalid export inputs")
        if any(not isinstance(c, dict) or set(c) != {"source_id"} or not isinstance(c["source_id"], str) or not c["source_id"] for c in s["citations"]):
            raise ValueError("invalid citation")
        if not cls._numbers(s["calc_inputs"]):
            raise ValueError("invalid calculation inputs")
        cls._validate_calculations(s["calculations"])
        cls._validate_material(s["material_table"])
        event_keys = {"event_id", "type", "run_id", "thread_id", "time", "payload_version", "payload"}
        if not isinstance(event, dict) or set(event) != event_keys:
            raise ValueError("invalid final event")
        if event["run_id"] != identity or event["type"] != "finalize" or event["payload_version"] != "v1":
            raise ValueError("final event identity mismatch")
        cls._uuid(event["thread_id"])
        cursor = event["event_id"]
        if (
            not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit()
            or len(cursor) > 12 or int(cursor) <= 0 or str(int(cursor)) != cursor
            or not isinstance(event["time"], str)
        ):
            raise ValueError("invalid event metadata")
        if len(event["time"]) > 64 or datetime.fromisoformat(event["time"]).tzinfo is None:
            raise ValueError("invalid event time")
        payload = event["payload"]
        if not isinstance(payload, dict) or payload.get("status") != "succeeded":
            raise ValueError("invalid final payload")
        allowed_payload = {
            "status", "answer", "citations", "calculations", "warnings", "errors",
            "retrieval_mode", "retrieval_mode_reason", "model", "export_formats",
        }
        expected_formats = (["docx"] if s["answer"].strip() else []) + (["xlsx"] if s["material_table"] is not None else [])
        if set(payload) - allowed_payload or payload.get("export_formats") != expected_formats:
            raise ValueError("invalid persisted export capabilities")
        if any(not cls._strings(payload[k]) for k in ("warnings", "errors") if k in payload):
            raise ValueError("invalid final messages")
        if any(payload[k] is not None and not isinstance(payload[k], str) for k in ("model", "retrieval_mode", "retrieval_mode_reason") if k in payload):
            raise ValueError("invalid final metadata")
        cls._validate_calculations(payload.get("calculations", []))
        if any(payload.get(k, []) != s[k] for k in ("citations", "calculations")) or payload.get("answer") != s["answer"]:
            raise ValueError("export and final payload disagree")
        return data

    def _open_root(self, *, create: bool) -> int:
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError("unsafe snapshot directory")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise ValueError("unsafe snapshot directory permissions")
        return fd

    def write(self, record: dict[str, Any]) -> None:
        data = self._private_record(self._validate(record))
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError("snapshot exceeds size limit")
        directory = self._open_root(create=True)
        temporary = "." + uuid4().hex + ".pending"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, data["snapshot"]["run_id"] + ".json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def read(self, run_id: str) -> dict[str, Any] | None:
        try:
            self._uuid(run_id)
            directory = self._open_root(create=False)
            try:
                fd = os.open(run_id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            finally:
                os.close(directory)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                    or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_size > self.max_bytes
                ):
                    return None
                raw = stream.read(self.max_bytes + 1)
                after = os.fstat(stream.fileno())
                if len(raw) != before.st_size or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns or after.st_nlink != 1:
                    return None
            def invalid_constant(_value: str) -> None:
                raise ValueError("nonfinite JSON")
            decoded = json.loads(raw, parse_constant=invalid_constant)
            # Re-encoding refuses values such as an overflowing exponent.
            json.dumps(decoded, allow_nan=False)
            return self._private_record(self._validate(decoded, run_id))
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            return None


def upload_directory(root: Path, upload_id: str) -> Path:
    """一次上传独占一个目录（``{root}/{upload_id}/``）。

    ``upload_id`` 必须是调用方生成的 uuid4 十六进制串，绝不来自用户输入——
    这样目录名天然无法承载路径穿越，落盘路径不依赖文件名清洗的正确性。
    """

    if not upload_id or not all(char in "0123456789abcdef-" for char in upload_id.lower()):
        raise ValueError(f"upload_id 必须是十六进制 uuid 形态，收到：{upload_id!r}")
    return root / upload_id


def write_upload_files(
    root: Path,
    upload_id: str,
    *,
    filename: str,
    payload: bytes,
    extracted_text: str,
) -> tuple[Path, Path]:
    """落盘原始文件与提取文本，返回 ``(原始文件路径, extracted.txt 路径)``。

    ``extracted_text`` 必须是**已过护栏**的文本（命中 PII/凭据时是脱敏版本）：
    未脱敏内容不得落盘，这与 ``GuardrailPipeline.sanitize_for_persistence`` 同纪律。
    """

    directory = upload_directory(root, upload_id)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, DIR_MODE)

    original = directory / filename
    original.write_bytes(payload)
    os.chmod(original, FILE_MODE)

    extracted = directory / "extracted.txt"
    extracted.write_text(extracted_text, encoding="utf-8")
    os.chmod(extracted, FILE_MODE)
    return original, extracted


def remove_upload_files(root: Path, upload_id: str) -> bool:
    """删除一次上传的整个目录。返回目录此前是否存在。

    目录不在不是错误（库行与盘上文件可能因为一次失败的写入而不同步，删除要幂等）。
    """

    import shutil

    directory = upload_directory(root, upload_id)
    if not directory.exists():
        return False
    shutil.rmtree(directory)
    return True


__all__ = [
    "DEV_EXPORT_DIR",
    "DEV_UPLOAD_DIR",
    "DIR_MODE",
    "EXPORT_DIR_ENV",
    "FILE_MODE",
    "UPLOAD_DIR_ENV",
    "VOLUME_EXPORT_DIR",
    "VOLUME_UPLOAD_DIR",
    "ExportSnapshotStore",
    "remove_upload_files",
    "resolve_export_dir",
    "resolve_upload_dir",
    "upload_directory",
    "write_upload_files",
]

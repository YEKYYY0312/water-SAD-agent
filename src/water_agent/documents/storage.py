"""上传与导出文件的落盘位置（P5-S48 Step 1/4）。

**为什么不是 ``data/`` 下面**：镜像把 ``/app/data`` 打成只读（``Dockerfile`` 里
``chmod -R a-w``，实测 ``dr-xr-xr-x``），任何写进那里的路径在容器中必然
``OSError``。可写位置是 compose 卷 ``app_uploads:/var/lib/water-agent/uploads`` 与
``app_exports:/var/lib/water-agent/exports``（spec D1 的口径）。执行 prompt 里写的
``data/projects/{project_id}/uploads/`` 是错的，按 spec 与容器现实修正。

解析顺序与 ``observability/json_trace.py::resolve_trace_dir()`` 一致：
显式参数 → 环境变量 → 卷路径（存在且可写）→ 仓库内开发兜底。

权限一律 **0644 文件 / 0755 目录**：Step 45 实测 0600 会让容器 uid 10001 直接
``PermissionError``。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

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
    "remove_upload_files",
    "resolve_export_dir",
    "resolve_upload_dir",
    "upload_directory",
    "write_upload_files",
]

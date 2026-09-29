"""DWG → DXF 转换（P5-S51，LibreDWG ``dwg2dxf``）。

这是**入库流水线里由程序发起的受控子进程**，不是模型工具：模型无法触发它、无法控制
任何参数，``Capability.EXECUTE_SHELL`` 照旧对所有主体一律拒绝（规格 D2）。

DWG 解析器面对的是不可信二进制，所以最坏情况必须被限制在"这一次上传失败"（规格 D3）：
- argv 固定、``shell=False``、可执行文件用绝对路径解析一次，不在每次调用时走 PATH；
- 输入/输出只在本次转换独占的临时目录里，文件名固定（``in.dwg`` / ``out.dxf``），
  用户文件名从不进入命令行；
- 超时 + ``RLIMIT_AS`` / ``RLIMIT_CPU`` / ``RLIMIT_FSIZE``：挂死、吃光内存、写爆磁盘都
  止于这个子进程；
- 环境变量白名单；stdout/stderr 只截尾部做诊断，**不回显给浏览器**。

LibreDWG 是 GPLv3，这里以独立进程调用、不链接进本进程（规格 D8）。

P6-S1 追加反方向 ``dxf2dwg``（参数化出图的**实验性** DWG 输出）：输入是本程序自己渲染的
DXF，不是用户文件，但约束完全照搬——固定 argv、临时目录固定文件名、超时与 rlimit、
环境变量白名单。LibreDWG 写 DWG 仍是实验功能，写出的文件只经 LibreDWG 自己回读核对，
未在 AutoCAD / 天正里验证过。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from water_agent.documents.errors import CODE_UPLOAD_EXTRACTION_FAILED, UploadError

#: 显式指定转换器路径（镜像里是 /opt/libredwg/bin/dwg2dxf）；未设置时在 PATH 里找一次。
CONVERTER_ENV = "WATER_AGENT_DWG2DXF"
#: DXF → DWG 写出器（P6-S1）。未设置时先找 dwg2dxf 同目录下的 dxf2dwg，再找 PATH。
WRITER_ENV = "WATER_AGENT_DXF2DWG"
CONVERT_TIMEOUT_SECONDS = 60
#: 真实样图 500 KB 的 DWG 转出 3.4 MB DXF；200 MiB 足够任何 10 MB 以内的上传。
_MAX_OUTPUT_BYTES = 200 * 1024 * 1024
_MAX_ADDRESS_SPACE = 1024 * 1024 * 1024
_DIAGNOSTIC_TAIL = 2048
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH")
#: DWG 版本标识 AC1012(R13) … AC1032(R2018+)；更老的 AC1009 以前 LibreDWG 不支持。
_DWG_MAGIC = b"AC10"


#: 组码 5（handle）值为 0 的记录。DXF 规定 handle 0 非法，ezdxf 直接拒绝打开整张图
#: （"Invalid handle 0"）。LibreDWG 0.14 读取它自己 dxf2dwg 写出的 DWG 时会给一个
#: ENDBLK 写出 handle 0（S51 实测；4 张真实 DWG 均未出现）。只删这一对组码，让 ezdxf
#: 重新分配 handle——不改任何几何或文字，修复次数计入转换警告、如实披露。
_ZERO_HANDLE_RE = re.compile(rb"(?<=\n)[ ]*5\r?\n0\r?\n")


def _repair_zero_handles(dxf: bytes) -> tuple[bytes, int]:
    return _ZERO_HANDLE_RE.subn(b"", dxf)


@dataclass(frozen=True, slots=True)
class DwgConversion:
    dxf: bytes
    converter: str
    version: str
    warnings: int


def converter_path() -> str | None:
    """转换器的绝对路径；不可用时返回 None（能力说明与白名单据此推导）。"""

    explicit = os.environ.get(CONVERTER_ENV, "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    found = shutil.which("dwg2dxf")
    return str(Path(found).resolve()) if found else None


def dwg_converter_available() -> bool:
    return converter_path() is not None


def _limit_resources() -> None:  # pragma: no cover -- 在子进程里执行
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (CONVERT_TIMEOUT_SECONDS, CONVERT_TIMEOUT_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_MAX_OUTPUT_BYTES, _MAX_OUTPUT_BYTES))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_MAX_ADDRESS_SPACE, _MAX_ADDRESS_SPACE))
    except (ValueError, OSError):
        # macOS 不支持收紧 RLIMIT_AS；Linux 镜像里生效。超时与 CPU 上限两处兜底。
        pass


def _tail(data: bytes) -> str:
    return data[-_DIAGNOSTIC_TAIL:].decode("utf-8", errors="replace")


def check_dwg_magic(data: bytes) -> None:
    if not data.startswith(_DWG_MAGIC) or len(data) < 128:
        raise UploadError(
            "UPLOAD_MAGIC_MISMATCH",
            "文件内容不是 DWG（文件头应为 AC10xx 版本标识）",
        )


def convert_dwg(data: bytes) -> DwgConversion:
    """把 DWG 字节转换为 DXF 字节。

    Raises:
        UploadError: 文件头不是 DWG（``UPLOAD_MAGIC_MISMATCH``）；转换器缺失、超时、
            失败或没有产出（``UPLOAD_EXTRACTION_FAILED``）。
    """

    check_dwg_magic(data)
    executable = converter_path()
    if executable is None:
        raise UploadError(
            CODE_UPLOAD_EXTRACTION_FAILED,
            "服务器未安装 DWG 转换器（LibreDWG），暂时无法读取 DWG；请在 CAD 中另存为 DXF 后上传",
        )
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="wa-dwg-") as tmp:
        work = Path(tmp)
        source, target = work / "in.dwg", work / "out.dxf"
        source.write_bytes(data)
        argv = [executable, "-y", "-o", str(target), str(source)]
        try:
            completed = subprocess.run(
                argv,
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=CONVERT_TIMEOUT_SECONDS,
                check=False,
                preexec_fn=_limit_resources if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise UploadError(
                CODE_UPLOAD_EXTRACTION_FAILED,
                f"DWG 转换超时（>{CONVERT_TIMEOUT_SECONDS} s），图纸可能过于复杂或已损坏",
            ) from exc
        except OSError as exc:
            raise UploadError(
                CODE_UPLOAD_EXTRACTION_FAILED, f"DWG 转换器无法启动（{type(exc).__name__}）"
            ) from exc
        stderr = _tail(completed.stderr) + _tail(completed.stdout)
        if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            first_error = next((line for line in stderr.splitlines() if "ERROR" in line), "")
            raise UploadError(
                CODE_UPLOAD_EXTRACTION_FAILED,
                "DWG 转换失败（退出码 "
                f"{completed.returncode}）{'：' + first_error[:160] if first_error else ''}；"
                "请确认文件能在 CAD 中正常打开，或另存为 DXF 后上传",
            )
        dxf, repaired = _repair_zero_handles(target.read_bytes())
    return DwgConversion(
        dxf=dxf,
        converter=executable,
        version=_version_of(executable, env),
        warnings=repaired
        + sum(1 for line in stderr.splitlines() if "ERROR" in line or "Warning" in line),
    )


class DwgWriteError(RuntimeError):
    """DXF → DWG 写出失败。消息只含退出码与转换器的首条 ERROR 行，不含路径。"""


@dataclass(frozen=True, slots=True)
class DxfToDwg:
    dwg: bytes
    converter: str
    #: 转换器输出里 ERROR / Warning 行的条数（LibreDWG 对 MATERIAL 等对象会大量告警）。
    warnings: int


def _executable(path: str | Path) -> str | None:
    candidate = Path(path)
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def dxf2dwg_path() -> str | None:
    """写出器的绝对路径；不可用时返回 None。"""

    explicit = os.environ.get(WRITER_ENV, "").strip()
    if explicit:
        return _executable(explicit)
    reader = converter_path()
    if reader is not None:
        sibling = _executable(Path(reader).with_name("dxf2dwg"))
        if sibling is not None:
            return sibling
    found = shutil.which("dxf2dwg")
    return str(Path(found).resolve()) if found else None


def dwg_roundtrip_available() -> bool:
    """写出与回读两个方向都在，DWG 输出才可能启用（回读核对不过就不交付）。"""

    return dxf2dwg_path() is not None and converter_path() is not None


def convert_dxf_to_dwg(dxf: bytes) -> DxfToDwg:
    """把本程序渲染的 DXF 写成 DWG（LibreDWG 默认写 R2000 / AC1015）。

    Raises:
        DwgWriteError: 写出器缺失、超时、失败或没有产出。
    """

    executable = dxf2dwg_path()
    if executable is None:
        raise DwgWriteError("服务器未安装 LibreDWG 的 dxf2dwg，DWG 输出不可用")
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="wa-dxf2dwg-") as tmp:
        work = Path(tmp)
        source, target = work / "in.dxf", work / "out.dwg"
        source.write_bytes(dxf)
        argv = [executable, "-y", "-o", str(target), str(source)]
        try:
            completed = subprocess.run(
                argv,
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=CONVERT_TIMEOUT_SECONDS,
                check=False,
                preexec_fn=_limit_resources if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise DwgWriteError(f"DWG 写出超时（>{CONVERT_TIMEOUT_SECONDS} s）") from exc
        except OSError as exc:
            raise DwgWriteError(f"DWG 写出器无法启动（{type(exc).__name__}）") from exc
        diagnostics = _tail(completed.stderr) + _tail(completed.stdout)
        if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            first_error = next((line for line in diagnostics.splitlines() if "ERROR" in line), "")
            raise DwgWriteError(
                f"DWG 写出失败（退出码 {completed.returncode}）"
                + (f"：{first_error[:160]}" if first_error else "")
            )
        dwg = target.read_bytes()
    return DxfToDwg(
        dwg=dwg,
        converter=executable,
        warnings=sum(1 for line in diagnostics.splitlines() if "ERROR" in line or "Warning" in line),
    )


def _version_of(executable: str, env: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            [executable, "--version"], env=env, capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    words = completed.stdout.decode("utf-8", errors="replace").split()
    return words[-1] if words else "unknown"


__all__ = [
    "CONVERTER_ENV",
    "WRITER_ENV",
    "DwgConversion",
    "DwgWriteError",
    "DxfToDwg",
    "check_dwg_magic",
    "convert_dwg",
    "convert_dxf_to_dwg",
    "converter_path",
    "dwg_converter_available",
    "dwg_roundtrip_available",
    "dxf2dwg_path",
]

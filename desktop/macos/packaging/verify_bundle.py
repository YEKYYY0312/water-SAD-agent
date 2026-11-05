"""Fail closed on private files, external native links and resource tampering."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

MANIFEST = "build-manifest.json"
PRIVATE_PARTS = {
    ".git",
    ".venv",
    "_venv",
    "secrets",
    "credentials",
    "sessions",
    "traces",
    "__pycache__",
}
MACH_MAGIC = {
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
}


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def private_path(path: Path) -> bool:
    if path.as_posix() in {
        "Contents/Resources/python/lib/python3.12/site-packages/certifi/cacert.pem",
        "python/lib/python3.12/site-packages/certifi/cacert.pem",
    }:
        return False
    return (
        any(part in PRIVATE_PARTS or part.startswith(".env") for part in path.parts)
        or path.name in {"credentials.json", "direct_url.json", "pyvenv.cfg"}
        or path.suffix in {".key", ".pem", ".p12", ".pfx", ".pyc"}
    )


def check_dependency(dependency: str) -> None:
    if not dependency.startswith(
        ("@loader_path/", "@rpath/", "@executable_path/", "/usr/lib/", "/System/Library/")
    ):
        raise ValueError(f"external Mach-O dependency: {dependency}")


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as source:
        return source.read(4) in MACH_MAGIC


def parse_dependencies(output: str) -> list[str]:
    dependencies = []
    load = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("cmd "):
            load = stripped.split()[1] in {
                "LC_LOAD_DYLIB",
                "LC_LOAD_WEAK_DYLIB",
                "LC_REEXPORT_DYLIB",
                "LC_LOAD_UPWARD_DYLIB",
            }
        elif load and stripped.startswith("name "):
            dependencies.append(stripped[5:].split(" (offset")[0])
            load = False
    return dependencies


def native_dependencies(path: Path) -> list[str]:
    output = subprocess.check_output(["/usr/bin/otool", "-l", str(path)], text=True)
    return parse_dependencies(output)


def executable_directory(root: Path, binary: Path) -> Path:
    relative = binary.relative_to(root).parts
    if relative[:3] == ("Contents", "Resources", "python"):
        return root / "Contents/Resources/python/bin"
    if relative[:1] == ("python",):
        return root / "python/bin"
    return root / "Contents/MacOS"


def resolve_rpath(root: Path, binary: Path, rpath: str) -> Path | None:
    """Validate every search directory, including unused fallback entries."""
    if rpath == "/usr/lib/swift":
        return None  # Apple system runtime / dyld shared cache, never bundled.
    if rpath == "@loader_path":
        directory = binary.parent
    elif rpath.startswith("@loader_path/"):
        directory = binary.parent / rpath.removeprefix("@loader_path/")
    elif rpath == "@executable_path":
        directory = executable_directory(root, binary)
    elif rpath.startswith("@executable_path/"):
        directory = executable_directory(root, binary) / rpath.removeprefix("@executable_path/")
    else:
        raise ValueError(f"external rpath: {rpath}")
    if not directory.is_dir() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"missing or escaping bundled rpath: {rpath}")
    return directory


def resolve_loader_dependency(root: Path, binary: Path, dependency: str, rpaths: list[str]) -> None:
    """Check all search paths before accepting an internal dependency candidate."""
    directories = [resolve_rpath(root, binary, rpath) for rpath in rpaths]
    if dependency.startswith(("/usr/lib/", "/System/Library/")):
        return
    if dependency.startswith("@loader_path/"):
        candidates = [binary.parent / dependency.removeprefix("@loader_path/")]
    elif dependency.startswith("@rpath/"):
        suffix = dependency.removeprefix("@rpath/")
        candidates = [directory / suffix for directory in directories if directory is not None]
    elif dependency.startswith("@executable_path/"):
        candidates = [
            executable_directory(root, binary) / dependency.removeprefix("@executable_path/")
        ]
    else:
        raise ValueError(f"external native dependency: {dependency}")
    if not any(c.is_file() and c.resolve().is_relative_to(root.resolve()) for c in candidates):
        raise ValueError(f"unresolved bundled native dependency: {dependency}")


def scan_tree(root: Path, *, native: bool = False) -> list[str]:
    problems = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if private_path(relative):
            problems.append(f"private path: {relative}")
        if path.is_symlink():
            target = path.readlink()
            if (
                target.is_absolute()
                or not path.resolve().is_relative_to(root.resolve())
                or not path.exists()
            ):
                problems.append(f"unsafe symlink: {relative}")
        if path.is_file() and not path.is_symlink():
            if native and is_macho(path):
                load_commands = subprocess.check_output(
                    ["/usr/bin/otool", "-l", str(path)], text=True
                )
                rpaths = re.findall(r"\bpath (.+?) \(offset", load_commands)
                for dependency in parse_dependencies(load_commands):
                    try:
                        check_dependency(dependency)
                        resolve_loader_dependency(root, path, dependency, rpaths)
                    except ValueError as error:
                        problems.append(f"{relative}: {error}")
                for rpath in rpaths:
                    try:
                        resolve_rpath(root, path, rpath)
                    except ValueError as error:
                        problems.append(f"{relative}: {error}")
                architecture = subprocess.run(
                    ["/usr/bin/lipo", str(path), "-verify_arch", "arm64"],
                    capture_output=True,
                    check=False,
                )
                if architecture.returncode:
                    problems.append(f"{relative}: arm64 architecture absent")
            if path.name == "direct_url.json":
                problems.append(f"developer install metadata: {relative}")
            # Do not classify documentation example tokens as real credentials. These
            # high-entropy private-key / provider key formats are never release data.
            if path.stat().st_size < 8_000_000 and path.suffix in {
                ".json",
                ".py",
                ".txt",
                ".yaml",
                ".yml",
                ".js",
                ".ini",
                ".toml",
                ".md",
                ".pem",
                ".crt",
                ".cer",
            }:
                content = path.read_bytes()
                if relative.as_posix() in {
                    "Contents/Resources/backend/src/water_agent/runtime/governance/scenarios.py",
                    "backend/src/water_agent/runtime/governance/scenarios.py",
                }:
                    # Exact public deterministic redaction canary, not a broad
                    # exemption of the file or provider-key format.
                    content = re.sub(
                        rb"(?<![A-Za-z0-9_-])sk-abcdef0123456789abcdef0123456789(?![A-Za-z0-9_-])",
                        b"PUBLIC_TEST_CANARY",
                        content,
                    )
                if re.search(
                    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|sk-(?:proj-)?[A-Za-z0-9_-]{32,}",
                    content,
                ):
                    problems.append(f"credential material: {relative}")
    return problems


def entries(root: Path) -> dict[str, dict[str, str | int]]:
    result: dict[str, dict[str, str | int]] = {}
    for path in sorted(root.rglob("*")):
        if path == root / MANIFEST:
            continue
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = {"symlink": str(path.readlink())}
        elif path.is_file():
            result[name] = {"sha256": digest(path), "size": path.stat().st_size}
    return result


def write_manifest(root: Path, metadata: dict) -> None:
    (root / MANIFEST).write_text(
        json.dumps({"metadata": metadata, "files": entries(root)}, ensure_ascii=False, indent=2)
        + "\n"
    )


def verify_manifest(root: Path) -> list[str]:
    path = root / MANIFEST
    if not path.is_file():
        return ["missing build manifest"]
    expected = json.loads(path.read_text())["files"]
    actual = entries(root)
    return [
        f"manifest mismatch: {name}"
        for name in sorted(expected.keys() | actual.keys())
        if expected.get(name) != actual.get(name)
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    resources = args.app / "Contents/Resources"
    required = [
        "python/bin/python3.12",
        "postgres/bin/postgres",
        "postgres/bin/initdb",
        "postgres/bin/pg_ctl",
        "postgres/bin/createdb",
        "libredwg/bin/dwg2dxf",
        "libredwg/bin/dxf2dwg",
        "postgres/bin/pg_dump",
        "postgres/bin/pg_restore",
        "backend/src/water_agent",
        "backend/alembic.ini",
        "backend/web/app/dist/index.html",
        "runtime/launcher.py",
        "runtime/service.py",
        "runtime/paths.py",
        "runtime/backup.py",
        "runtime/process_identity.py",
    ]
    problems = [f"missing resource: {item}" for item in required if not (resources / item).exists()]
    problems += scan_tree(args.app, native=True)
    problems += verify_manifest(resources)
    print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False, indent=2))
    raise SystemExit(bool(problems))


if __name__ == "__main__":
    main()

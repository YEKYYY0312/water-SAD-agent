"""Build pinned native dependencies only inside the local, ignored build cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / "work/macos-standalone"
PG_VERSION = "18.2"
DWG_VERSION = "0.14"
DWG_SHA256 = "cb6ee0b078c6d9e0f09d66f1feac33ba6342df88ae544e9f9335fab475218351"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(arguments: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(arguments), flush=True)
    subprocess.run(
        arguments, cwd=cwd, check=True, env={**os.environ, "MACOSX_DEPLOYMENT_TARGET": "14.0"}
    )


def fetch(url: str, destination: Path, expected: str | None = None) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".partial")
        run(
            [
                "curl",
                "-fL",
                "--retry",
                "3",
                "--connect-timeout",
                "30",
                "--max-time",
                "600",
                url,
                "-o",
                str(temporary),
            ]
        )
        temporary.replace(destination)
    if expected and digest(destination) != expected:
        raise ValueError(f"checksum mismatch: {destination.name}")
    return destination


def extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")
    return destination


def controlled(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to((WORK / "cache").resolve()):
        raise ValueError("native dependency must originate in controlled cache")
    return resolved


def build_source(name: str, version: str, archive: Path, flags: list[str]) -> Path:
    prefix = controlled(WORK / "cache" / f"{name}-{version}-install")
    if (prefix / ".complete").exists():
        return prefix
    build = WORK / "cache" / f"{name}-{version}-source"
    if not build.exists():
        staging = WORK / "cache" / f"{name}-{version}-extract"
        extract(archive, staging)
        next(staging.iterdir()).rename(build)
        staging.rmdir()
    prefix.mkdir(exist_ok=True)
    run(["./configure", f"--prefix={prefix}", *flags], cwd=build)
    run(["make", f"-j{min(os.cpu_count() or 2, 8)}"], cwd=build)
    run(["make", "install"], cwd=build)
    (prefix / ".complete").write_text(digest(archive) + "\n")
    return prefix


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["postgres", "libredwg", "all"], default="all")
    arguments = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This build targets native Apple Silicon macOS only")
    cache = WORK / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    record_path = cache / "dependencies.json"
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    if arguments.only in {"postgres", "all"}:
        url = f"https://ftp.postgresql.org/pub/source/v{PG_VERSION}/postgresql-{PG_VERSION}.tar.bz2"
        checksum = (
            fetch(url + ".sha256", cache / f"postgresql-{PG_VERSION}.tar.bz2.sha256")
            .read_text()
            .split()[0]
        )
        archive = fetch(url, cache / f"postgresql-{PG_VERSION}.tar.bz2", checksum)
        prefix = build_source(
            "postgresql",
            PG_VERSION,
            archive,
            [
                "--without-icu",
                "--without-readline",
                "--without-zlib",
                "--without-lz4",
                "--without-zstd",
                "--disable-nls",
            ],
        )
        record = json.loads(record_path.read_text()) if record_path.exists() else {}
        record["postgres"] = {
            "version": PG_VERSION,
            "source_url": url,
            "sha256": checksum,
            "prefix": str(prefix),
            "archive": str(archive),
            "configure": [
                "--without-icu",
                "--without-readline",
                "--without-zlib",
                "--without-lz4",
                "--without-zstd",
                "--disable-nls",
            ],
        }
        record_path.write_text(json.dumps(record, indent=2) + "\n")
    if arguments.only in {"libredwg", "all"}:
        url = f"https://ftp.gnu.org/gnu/libredwg/libredwg-{DWG_VERSION}.tar.gz"
        archive = fetch(url, cache / f"libredwg-{DWG_VERSION}.tar.gz", DWG_SHA256)
        flags = [
            "--disable-bindings",
            "--disable-werror",
            "--disable-docs",
            "--disable-shared",
            "--enable-static",
        ]
        prefix = build_source("libredwg", DWG_VERSION, archive, flags)
        record = json.loads(record_path.read_text()) if record_path.exists() else {}
        record["libredwg"] = {
            "version": DWG_VERSION,
            "source_url": url,
            "sha256": DWG_SHA256,
            "prefix": str(prefix),
            "archive": str(archive),
            "configure": flags,
        }
        record_path.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()

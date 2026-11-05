"""Assemble an arm64 local-use .app without host runtime or private user data."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from desktop.macos.packaging.build_dependencies import (
    ROOT,
    WORK,
    controlled,
    digest,
    extract,
    fetch,
    run,
)
from desktop.macos.packaging.verify_bundle import (
    is_macho,
    native_dependencies,
    private_path,
    scan_tree,
    write_manifest,
)


def validate_publish_path(path: Path) -> None:
    if (
        path.is_absolute()
        or ".." in path.parts
        or private_path(path)
        or any(part in {"uploads", "exports", "backups", "node_modules"} for part in path.parts)
    ):
        raise ValueError(f"private or unsafe publish path: {path}")


def copy_selected(source: Path, destination: Path, paths: list[Path]) -> None:
    for relative in paths:
        validate_publish_path(relative)
        src = source / relative
        if src.is_symlink() or not src.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"developer symlink in selected resources: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


def verify_index(index: Path) -> dict:
    metadata: dict = json.loads((index / "artifact.json").read_text())
    if metadata.get("model") != "text-embedding-3-large" or metadata.get("dimensions") != 3072:
        raise ValueError("index must be explicit text-embedding-3-large / 3072 release")
    listed = {"artifact.json"}
    for item in metadata["files"]:
        name = Path(item["name"])
        if name.name != str(name) or name.is_absolute() or name.name in listed:
            raise ValueError("unsafe index manifest")
        path = index / name
        if (
            path.is_symlink()
            or digest(path) != item["sha256"]
            or path.stat().st_size != item["size"]
        ):
            raise ValueError(f"index checksum mismatch: {name}")
        listed.add(str(name))
    if {p.name for p in index.iterdir()} != listed:
        raise ValueError("index contains unlisted resources")
    return metadata


def tracked_backend(destination: Path) -> None:
    output = subprocess.check_output(
        [
            "git",
            "ls-files",
            "-z",
            "src",
            "alembic",
            "alembic.ini",
            "data/fixtures",
            "data/curated",
            "data/versions.json",
        ],
        cwd=ROOT,
    )
    paths = [Path(name.decode()) for name in output.split(b"\0") if name]
    copy_selected(ROOT, destination, paths)


def prepare_python(destination: Path) -> dict:
    source_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20260825/cpython-3.12.14%2B20260825-aarch64-apple-darwin-install_only_stripped.tar.gz"
    source_sha256 = "8b0f1fa71eab7ca644e482c631807a1116fa848491051cd1c8d9429491de63a6"
    archive = fetch(source_url, WORK / "cache/cpython-3.12.14-20260825-arm64.tar.gz", source_sha256)
    extracted = WORK / "cache/cpython-3.12.14-20260825-arm64"
    if not extracted.exists():
        extract(archive, extracted)
    source = extracted / "python"
    version = subprocess.check_output(
        [
            str(source / "bin/python3.12"),
            "-I",
            "-B",
            "-c",
            "import platform; print(platform.python_version())",
        ],
        text=True,
    ).strip()
    if version != "3.12.14":
        raise ValueError("unexpected standalone Python version")
    shutil.copytree(source, destination, symlinks=True)
    requirements = WORK / "requirements-runtime.txt"
    run(
        [
            "uv",
            "export",
            "--quiet",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        ],
        cwd=ROOT,
    )
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(destination / "bin/python3.12"),
            "--break-system-packages",
            "--no-deps",
            "--require-hashes",
            "-r",
            str(requirements),
        ]
    )
    # No developer shebangs or stale precompiled absolute source paths ship.
    for path in list((destination / "bin").iterdir()):
        if path.name not in {"python", "python3", "python3.12"}:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    for path in destination.rglob("__pycache__"):
        shutil.rmtree(path)
    for path in destination.rglob("*.pyc"):
        path.unlink()
    for path in (destination / "lib/python3.12/site-packages").glob("pip*"):
        if path.is_dir():
            shutil.rmtree(path)
    for path in destination.rglob("direct_url.json"):
        raise ValueError(f"editable/developer install metadata: {path}")
    packages = subprocess.check_output(
        [
            str(destination / "bin/python3.12"),
            "-I",
            "-B",
            "-c",
            'import importlib.metadata,json;print(json.dumps(sorted((d.metadata["Name"],d.version) for d in importlib.metadata.distributions())))',
        ],
        text=True,
    )
    for path in destination.rglob("__pycache__"):
        shutil.rmtree(path)
    return {
        "version": version,
        "distribution": "python-build-standalone via uv",
        "build": "20260825",
        "source_url": source_url,
        "distribution_sha256": source_sha256,
        "uv_lock_sha256": digest(ROOT / "uv.lock"),
        "requirements_sha256": digest(requirements),
        "packages": json.loads(packages),
    }


def relocate_native(root: Path, original_prefixes: list[str]) -> None:
    for path in root.rglob("*"):
        if not is_macho(path):
            continue
        changes = []
        for dependency in native_dependencies(path):
            for prefix in original_prefixes:
                if dependency.startswith(prefix + "/"):
                    suffix = Path(dependency).relative_to(prefix)
                    bundled = root / suffix
                    if not bundled.exists():
                        raise ValueError(f"unbundled dependency: {dependency}")
                    target = "@loader_path/" + os.path.relpath(bundled, path.parent)
                    changes += ["-change", dependency, target]
        if path.suffix == ".dylib" or ".dylib." in path.name:
            changes += ["-id", "@loader_path/" + path.name]
        if changes:
            run(["/usr/bin/install_name_tool", *changes, str(path)])


def install_native(resources: Path, record: dict) -> dict:
    sources = resources / "licenses"
    sources.mkdir()
    metadata = {}
    for name, item in record.items():
        prefix = controlled(Path(item["prefix"]))
        archive = controlled(Path(item["archive"]))
        if digest(archive) != item["sha256"]:
            raise ValueError(f"native source checksum mismatch: {name}")
        destination = resources / name
        if name == "postgres":
            shutil.copytree(
                prefix,
                destination,
                symlinks=True,
                ignore=shutil.ignore_patterns("include", ".complete", "*.a", "pkgconfig"),
            )
        else:
            (destination / "bin").mkdir(parents=True)
            for executable in ["dwg2dxf", "dxf2dwg"]:
                shutil.copy2(prefix / "bin" / executable, destination / "bin" / executable)
        shutil.copy2(archive, sources / archive.name)
        actual_source = (
            WORK
            / "cache"
            / f"{'postgresql' if name == 'postgres' else name}-{item['version']}-source"
        )
        for license_name in ["COPYING", "COPYRIGHT", "LICENSE"]:
            if (actual_source / license_name).is_file():
                shutil.copy2(actual_source / license_name, sources / f"{name}-{license_name}")
        relocate_native(destination, [str(prefix)])
        metadata[name] = {
            key: value for key, value in item.items() if key not in {"prefix", "archive"}
        }
    (sources / "BUILD-INSTRUCTIONS.md").write_text(
        "Native sources included alongside this file. Extract each archive, run ./configure with the configure flags in build-manifest.json and --prefix=$PWD/install, then make -j8 && make install. Build on native arm64 macOS with Apple command line tools. PostgreSQL ICU/readline/compression/TLS are disabled; it serves only a private local Unix socket. LibreDWG is a separate GPLv3 executable built with static libredwg. Python is the uv-managed python-build-standalone 20260825 CPython 3.12.14 release. Installed wheel distributions include their original license metadata under python/lib/python3.12/site-packages. No Apple notarization is claimed.\n"
    )
    return metadata


def create_icon(resources: Path) -> None:
    """Draw a native SF Symbol icon without a network image dependency."""
    iconset = WORK / "WaterAgent.iconset"
    iconset.mkdir(exist_ok=True)
    script = WORK / "render-icon.swift"
    script.write_text(r"""import AppKit
import Foundation
let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let frame = NSRect(x: 0, y: 0, width: pixels, height: pixels)
        let background = NSBezierPath(roundedRect: frame.insetBy(dx: CGFloat(pixels)*0.04, dy: CGFloat(pixels)*0.04), xRadius: CGFloat(pixels)*0.20, yRadius: CGFloat(pixels)*0.20)
        NSColor(calibratedRed: 0.04, green: 0.27, blue: 0.39, alpha: 1).setFill()
        background.fill()
        let config = NSImage.SymbolConfiguration(pointSize: CGFloat(pixels)*0.58, weight: .semibold)
            .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
        if let symbol = NSImage(systemSymbolName: "drop.fill", accessibilityDescription: nil)?.withSymbolConfiguration(config) {
            symbol.draw(in: frame.insetBy(dx: CGFloat(pixels)*0.23, dy: CGFloat(pixels)*0.15))
        }
        image.unlockFocus()
        let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let suffix = scale == 2 ? "@2x" : ""
        let path = output.appendingPathComponent("icon_\(size)x\(size)\(suffix).png")
        try bitmap.representation(using: .png, properties: [:])!.write(to: path)
    }
}
""")
    run(["swift", str(script), str(iconset)])
    run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(resources / "WaterAgent.icns")])


def sign(app: Path) -> None:
    for path in app.rglob("*"):
        if is_macho(path):
            run(["/usr/bin/codesign", "--force", "--sign", "-", str(path)])
    run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument(
        "--skip-frontend", action="store_true", help="Reuse an already tested local dist"
    )
    parser.add_argument("--no-dmg", action="store_true")
    args = parser.parse_args()
    index_metadata = verify_index(args.index)
    record = json.loads((WORK / "cache/dependencies.json").read_text())
    if set(record) != {"postgres", "libredwg"}:
        raise ValueError("build both pinned native dependencies first")
    if not args.skip_frontend:
        for command in [
            ["npm", "ci", "--ignore-scripts"],
            ["npm", "test"],
            ["npm", "run", "build"],
        ]:
            run(command, cwd=ROOT / "web/app")
    run(["swift", "build", "--package-path", str(ROOT / "desktop/macos/native"), "-c", "release"])
    output = WORK / "output"
    output.mkdir(exist_ok=True)
    app = output / "给排水 Agent.app"
    if app.exists():
        for directory in [app, *app.rglob("*")]:
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o755)
        shutil.rmtree(app)
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    executable = app / "Contents/MacOS/WaterAgentDesktop"
    executable.parent.mkdir()
    shutil.copy2(ROOT / "desktop/macos/native/.build/release/WaterAgentDesktop", executable)
    cached_python = WORK / "cache/python-runtime"
    cached_metadata = WORK / "cache/python-runtime.json"
    if cached_metadata.exists():
        python_metadata = json.loads(cached_metadata.read_text())
        if python_metadata["uv_lock_sha256"] != digest(ROOT / "uv.lock"):
            raise ValueError("cached Python lock fingerprint changed; rebuild cache")
        shutil.copytree(cached_python, resources / "python", symlinks=True)
    else:
        python_metadata = prepare_python(resources / "python")
    native_metadata = install_native(resources, record)
    dwg_report = WORK / "dwg-smoke/report.json"
    if dwg_report.exists():
        native_metadata["libredwg"]["synthetic_production_roundtrip"] = json.loads(
            dwg_report.read_text()
        )
    tracked_backend(resources / "backend")
    shutil.copytree(ROOT / "web/app/dist", resources / "backend/web/app/dist")
    shutil.copytree(
        ROOT / "desktop/macos/runtime",
        resources / "runtime",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    target_index = resources / "backend/data/indexes" / args.index.name
    shutil.copytree(args.index, target_index)
    verify_index(target_index)
    for path in (resources / "backend/data").rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    create_icon(resources)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleExecutable": "WaterAgentDesktop",
                "CFBundleIconFile": "WaterAgent.icns",
                "CFBundleIdentifier": "local.wateragent.desktop",
                "CFBundleName": "给排水 Agent",
                "CFBundleDisplayName": "给排水 Agent",
                "CFBundleVersion": "1",
                "CFBundleShortVersionString": "0.1.0",
                "CFBundlePackageType": "APPL",
                "LSMinimumSystemVersion": "14.0",
                "NSHighResolutionCapable": True,
                "NSLocalNetworkUsageDescription": "连接本应用专用的本机给排水 Agent 服务。",
            }
        )
    )
    # Swift embeds a build-toolchain fallback rpath even when macOS provides
    # the complete Swift ABI. Never let the shipped app search that host path.
    commands = subprocess.check_output(["/usr/bin/otool", "-l", str(executable)], text=True)
    import re

    for rpath in re.findall(r"\bpath (.+?) \(offset", commands):
        if rpath.startswith("/Library/Developer/"):
            run(["/usr/bin/install_name_tool", "-delete_rpath", rpath, str(executable)])
    for path in resources.rglob("__pycache__"):
        shutil.rmtree(path)
    problems = scan_tree(app, native=True)
    if problems:
        raise ValueError("\n".join(problems))
    # Nested signatures change native file bytes. Hash them only after signing.
    sign(app)
    metadata = {
        "scanner_waivers": [
            {
                "path": "backend/src/water_agent/runtime/governance/scenarios.py",
                "reason": "One exact deterministic public governance redaction canary; no other key format or file is exempt.",
            }
        ],
        "architecture": "arm64",
        "build_host_macos": os.uname().release,
        "source_snapshot": "Git commit identifies base; per-resource hashes identify the copied worktree snapshot.",
        "minimum_target_unverified": "14.0",
        "signing": "ad-hoc local use; not notarized",
        "git_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
        "native_executable_source_sha256": digest(
            ROOT / "desktop/macos/native/.build/release/WaterAgentDesktop"
        ),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "python": python_metadata,
        "native": native_metadata,
        "index": index_metadata,
    }
    write_manifest(resources, metadata)
    run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    run([sys.executable, str(Path(__file__).with_name("verify_bundle.py")), str(app)])
    print(f"APP_READY {app}", flush=True)
    if not args.no_dmg:
        dmg = output / "WaterAgent-arm64.dmg"
        staging = WORK / "dmg-staging"
        if staging.exists():
            for directory in [staging, *staging.rglob("*")]:
                if directory.is_dir() and not directory.is_symlink():
                    directory.chmod(0o755)
            shutil.rmtree(staging)
        staging.mkdir()
        shutil.copytree(app, staging / app.name, symlinks=True)
        (staging / "Applications").symlink_to("/Applications")
        run(
            [
                "/usr/bin/hdiutil",
                "create",
                "-ov",
                "-format",
                "UDZO",
                "-volname",
                "给排水 Agent",
                "-srcfolder",
                str(staging),
                str(dmg),
            ]
        )
        (output / "SHA256SUMS").write_text(f"{digest(dmg)}  {dmg.name}\n")
    print(str(app))


if __name__ == "__main__":
    main()

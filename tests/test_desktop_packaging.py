"""Publish selectors and relocation checks fail closed before app assembly."""

from pathlib import Path

import pytest

from desktop.macos.packaging.build_app import copy_selected, validate_publish_path, verify_index
from desktop.macos.packaging.verify_bundle import (
    check_dependency,
    scan_tree,
    verify_manifest,
    write_manifest,
)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "src/.env.production",
        "secrets/a",
        "_venv/bin/python",
        ".git/config",
        "data/sessions/a",
        "data/traces/a",
        "uploads/a",
        "a/private.key",
    ],
)
def test_publish_rejects_private_paths(name):
    with pytest.raises(ValueError):
        validate_publish_path(Path(name))


def test_copy_rejects_developer_symlink(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").symlink_to("/etc/passwd")
    with pytest.raises(ValueError):
        copy_selected(source, tmp_path / "out", [Path("module.py")])


def test_relocation_rejects_homebrew_and_absolute_dependencies():
    for dependency in [
        "/opt/homebrew/lib/libssl.dylib",
        "/Users/owner/lib.dylib",
        "/usr/local/lib/test.dylib",
    ]:
        with pytest.raises(ValueError):
            check_dependency(dependency)
    for dependency in [
        "@loader_path/../lib/libpq.dylib",
        "@rpath/libpython3.12.dylib",
        "/usr/lib/libSystem.B.dylib",
        "/System/Library/Frameworks/AppKit.framework/Versions/C/AppKit",
    ]:
        check_dependency(dependency)


def test_bundle_scan_rejects_absolute_symlink_and_credentials(tmp_path):
    (tmp_path / "a").symlink_to("/usr/bin/python3")
    assert any("symlink" in item for item in scan_tree(tmp_path))
    (tmp_path / "a").unlink()
    (tmp_path / "credentials.json").write_text("{}")
    assert any("private" in item for item in scan_tree(tmp_path))


def test_manifest_detects_changed_and_unlisted_resources(tmp_path):
    (tmp_path / "source.py").write_text("x=1\n")
    write_manifest(tmp_path, {"architecture": "arm64"})
    assert verify_manifest(tmp_path) == []
    (tmp_path / "source.py").write_text("x=2\n")
    assert verify_manifest(tmp_path)
    (tmp_path / "source.py").write_text("x=1\n")
    (tmp_path / "unlisted").write_text("x")
    assert verify_manifest(tmp_path)


def test_index_rejects_hash_mismatch(tmp_path):
    (tmp_path / "artifact.json").write_text('{"files": {"clauses.db": "' + "0" * 64 + '"}}')
    (tmp_path / "clauses.db").write_bytes(b"wrong")
    with pytest.raises(ValueError):
        verify_index(tmp_path)


def test_load_commands_exclude_dylib_id_and_fat_headers():
    from desktop.macos.packaging.verify_bundle import parse_dependencies

    output = """/tmp/libtest.dylib (architecture arm64):
Load command 0
      cmd LC_ID_DYLIB
  cmdsize 64
     name /DLC/libtest.dylib (offset 24)
Load command 1
      cmd LC_LOAD_DYLIB
  cmdsize 64
     name @loader_path/libdependency.dylib (offset 24)
"""
    assert parse_dependencies(output) == ["@loader_path/libdependency.dylib"]


def test_only_certifi_public_ca_bundle_is_allowed():
    from desktop.macos.packaging.verify_bundle import private_path

    assert not private_path(Path("python/lib/python3.12/site-packages/certifi/cacert.pem"))
    assert private_path(Path("python/lib/python3.12/site-packages/certifi/secret.pem"))


def test_known_governance_canary_does_not_hide_other_tokens(tmp_path):
    path = tmp_path / "backend/src/water_agent/runtime/governance/scenarios.py"
    path.parent.mkdir(parents=True)
    path.write_text('"sk-abcdef0123456789abcdef0123456789"')
    assert not scan_tree(tmp_path)
    path.write_text('"sk-' + "a1B9c4D8" * 5 + '"')
    assert any("credential material" in p for p in scan_tree(tmp_path))


def test_public_canary_is_rejected_outside_governance_scenario(tmp_path):
    (tmp_path / "other.py").write_text('"sk-abcdef0123456789abcdef0123456789"')
    assert any("credential material" in p for p in scan_tree(tmp_path))


def test_relative_native_dependency_must_exist_inside_bundle(tmp_path):
    from desktop.macos.packaging.verify_bundle import resolve_loader_dependency

    binary = tmp_path / "bin/tool"
    binary.parent.mkdir()
    binary.write_bytes(b"")
    with pytest.raises(ValueError):
        resolve_loader_dependency(tmp_path, binary, "@loader_path/../lib/missing.dylib", [])
    library = tmp_path / "lib/ok.dylib"
    library.parent.mkdir()
    library.write_bytes(b"")
    resolve_loader_dependency(tmp_path, binary, "@loader_path/../lib/ok.dylib", [])
    with pytest.raises(ValueError):
        resolve_loader_dependency(tmp_path, binary, "@loader_path/../../outside.dylib", [])


def test_source_download_cache_must_match_checksum(tmp_path):
    from desktop.macos.packaging.build_dependencies import fetch

    archive = tmp_path / "download.tar"
    archive.write_bytes(b"altered")
    with pytest.raises(ValueError):
        fetch("https://example.invalid/no-network-needed", archive, "0" * 64)


def test_public_ca_exemption_still_rejects_private_key_material(tmp_path):
    path = tmp_path / "python/lib/python3.12/site-packages/certifi/cacert.pem"
    path.parent.mkdir(parents=True)
    path.write_text("-----BEGIN PRIVATE KEY-----\npublic-ca-path-must-not-hide-this\n")
    assert any("credential material" in p for p in scan_tree(tmp_path))


def test_nested_manifest_name_cannot_hide_an_unlisted_file(tmp_path):
    write_manifest(tmp_path, {})
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "build-manifest.json").write_text("{}")
    assert verify_manifest(tmp_path)


@pytest.mark.parametrize(
    "prefix,suffix",
    [("PREFIX_", ""), ("", "_EXTRA_PRIVATE_TOKEN_12345678901234567890"), ("a", ""), ("", "a")],
)
def test_governance_canary_waiver_requires_complete_token(tmp_path, prefix, suffix):
    path = tmp_path / "backend/src/water_agent/runtime/governance/scenarios.py"
    path.parent.mkdir(parents=True)
    path.write_text('"' + prefix + "sk-abcdef0123456789abcdef0123456789" + suffix + '"')
    assert any("credential material" in p for p in scan_tree(tmp_path))


@pytest.mark.parametrize(
    "rpath",
    ["@loader_path/../../outside", "@loader_path/../missing", "@executable_path/../../../outside"],
)
def test_each_rpath_must_be_existing_and_internal_even_with_valid_fallback(tmp_path, rpath):
    from desktop.macos.packaging.verify_bundle import resolve_loader_dependency

    binary = tmp_path / "bin/tool"
    binary.parent.mkdir()
    binary.write_bytes(b"")
    library = tmp_path / "lib/ok.dylib"
    library.parent.mkdir()
    library.write_bytes(b"")
    with pytest.raises(ValueError):
        resolve_loader_dependency(
            tmp_path, binary, "@rpath/ok.dylib", [rpath, "@loader_path/../lib"]
        )


@pytest.mark.parametrize("prefix", ["", "nested/", "secrets/"])
def test_certifi_ca_exemption_requires_canonical_bundle_path(prefix):
    from desktop.macos.packaging.verify_bundle import private_path

    assert private_path(Path(prefix + "site-packages/certifi/cacert.pem"))
    assert private_path(Path("nested/python/lib/python3.12/site-packages/certifi/cacert.pem"))
    assert not private_path(
        Path("Contents/Resources/python/lib/python3.12/site-packages/certifi/cacert.pem")
    )


def test_existing_outside_rpath_cannot_precede_a_valid_internal_fallback(tmp_path):
    from desktop.macos.packaging.verify_bundle import resolve_loader_dependency

    root = tmp_path / "app"
    binary = root / "bin/tool"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    inside = root / "lib"
    inside.mkdir()
    (inside / "ok.dylib").write_bytes(b"")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ok.dylib").write_bytes(b"")
    with pytest.raises(ValueError, match="escaping bundled rpath"):
        resolve_loader_dependency(
            root, binary, "@rpath/ok.dylib", ["@loader_path/../../outside", "@loader_path/../lib"]
        )


def test_executable_rpath_resolves_from_the_bundle_executable_directory(tmp_path):
    from desktop.macos.packaging.verify_bundle import resolve_rpath

    binary = tmp_path / "Contents/MacOS/tool"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    resources = tmp_path / "Contents/Resources"
    resources.mkdir()
    assert (
        resolve_rpath(tmp_path, binary, "@executable_path/../Resources")
        == binary.parent / "../Resources"
    )
    assert resolve_rpath(tmp_path, binary, "/usr/lib/swift") is None

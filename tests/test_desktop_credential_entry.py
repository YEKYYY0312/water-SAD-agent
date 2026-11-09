"""Synthetic-only source provisioning and enforce-before-write rotation checks."""
from __future__ import annotations

import copy
import json
import logging
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest

from desktop.macos.runtime import credential_entry as entry
from desktop.macos.runtime import credential_entry_worker as worker
from desktop.macos.runtime import credential_handoff as handoff
from desktop.macos.runtime.paths import private_directory
from water_agent.config import credentials

BINDING = {
    "version": 1, "volume": "wa-a2-entry-" + "a" * 32,
    "image": "sha256:" + "b" * 64,
    "chat": {"base_url": "https://model-endpoint.invalid/v1", "model": "deepseek-v4.1-flash"},
    "embedding": {"base_url": "https://model-endpoint.invalid/v1", "model": "text-embedding-3-large"},
}
VALUES = dict(zip(handoff.SLOTS, ("EntrySyntheticNewChat", "EntrySyntheticNewEmbedding"), strict=True))


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    import urllib.request

    import httpx
    env, injected, level = dict(os.environ), set(credentials._INJECTED), logging.root.manager.disable
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("no network"))
    monkeypatch.setattr(httpx.Client, "send", lambda *_a, **_k: pytest.fail("no network"))
    yield
    os.environ.clear(); os.environ.update(env)
    credentials._INJECTED.clear(); credentials._INJECTED.update(injected)
    logging.disable(level)


def test_source_worker_uses_production_writer_and_no_extra_slots(tmp_path):
    root = private_directory(tmp_path / "source")
    worker.seed(BINDING, VALUES, root)
    assert {p.name for p in root.iterdir()} == {"llm_endpoint.json", *[credentials.CREDENTIALS[n] for n in VALUES]}
    for name, value in VALUES.items():
        p = root / credentials.CREDENTIALS[name]
        assert p.read_text() == value and stat.S_IMODE(p.stat().st_mode) == 0o600


@pytest.mark.parametrize("bad", [{}, {**VALUES, "DEEPSEEK_API_KEY": "EntrySyntheticOther"}, {**VALUES, handoff.SLOTS[0]: "bad value"}])
def test_source_worker_validates_whole_payload_before_writing(tmp_path, bad):
    root = private_directory(tmp_path / "source")
    with pytest.raises(ValueError):
        worker.seed(BINDING, bad, root)
    assert not list(root.iterdir())


def test_worker_never_overwrites_an_existing_source(tmp_path):
    root = private_directory(tmp_path / "source")
    (root / "existing").write_text("public")
    with pytest.raises(ValueError):
        worker.seed(BINDING, VALUES, root)
    assert (root / "existing").read_text() == "public"


def test_partial_seed_failure_cleans_only_own_source(tmp_path, monkeypatch):
    root = private_directory(tmp_path / "source")
    original = credentials.write_credential
    def fail_second(name, value):
        if name == handoff.SLOTS[1]:
            raise RuntimeError(value)
        original(name, value)
    monkeypatch.setattr(credentials, "write_credential", fail_second)
    with pytest.raises(RuntimeError):
        worker.seed(BINDING, VALUES, root)
    assert not list(root.iterdir())


@pytest.mark.parametrize("unchanged", [0, 1])
def test_require_different_refuses_partial_or_total_noop_before_any_write(tmp_path, monkeypatch, unchanged):
    root = private_directory(tmp_path / "models")
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(root))
    for name in credentials.CREDENTIALS:
        monkeypatch.delenv(name, raising=False); monkeypatch.delenv(name + "_FILE", raising=False)
    for i, name in enumerate(handoff.SLOTS):
        credentials.write_credential(name, VALUES[name] if i == unchanged else "EntrySyntheticOldValue")
    before = {p.name: (p.read_bytes(), p.stat().st_ino) for p in root.iterdir()}
    payload = {"version": 1, "chat": BINDING["chat"], "embedding": BINDING["embedding"], "values": VALUES}
    with pytest.raises(handoff.HandoffError, match="HANDOFF_NOT_DIFFERENT"):
        handoff.install_payload(payload, BINDING, require_different=True)
    assert {p.name: (p.read_bytes(), p.stat().st_ino) for p in root.iterdir()} == before


def test_rotation_records_only_boolean_differences(tmp_path, monkeypatch):
    root = private_directory(tmp_path / "models")
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(root))
    for name in credentials.CREDENTIALS:
        monkeypatch.delenv(name, raising=False); monkeypatch.delenv(name + "_FILE", raising=False)
    for name in handoff.SLOTS:
        credentials.write_credential(name, "EntrySyntheticOldValue")
    payload = {"version": 1, "chat": BINDING["chat"], "embedding": BINDING["embedding"], "values": VALUES}
    result = handoff.install_payload(payload, BINDING, require_different=True)
    assert result["different_from_previous"] == {name: True for name in handoff.SLOTS}
    assert all(value not in json.dumps(result) for value in VALUES.values())


def test_compare_only_proves_recovery_source_match_without_rewriting(tmp_path, monkeypatch):
    root = private_directory(tmp_path / "models")
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(root))
    for name in credentials.CREDENTIALS:
        monkeypatch.delenv(name, raising=False); monkeypatch.delenv(name + "_FILE", raising=False)
    for name, value in VALUES.items():
        credentials.write_credential(name, value)
    before = {p.name: p.stat().st_ino for p in root.iterdir()}
    payload = {"version": 1, "chat": BINDING["chat"], "embedding": BINDING["embedding"], "values": VALUES}
    result = handoff.install_payload(payload, BINDING, compare_only=True)
    assert result["matches_previous"] == dict.fromkeys(handoff.SLOTS, True)
    assert result["written"] is False
    assert {p.name: p.stat().st_ino for p in root.iterdir()} == before


def test_entry_command_changes_only_new_source_mount_and_worker():
    argv = entry.entry_command(BINDING, "wa-a2-entry-" + "a" * 32, "a" * 32)
    assert "--interactive" in argv and "--log-driver" in argv and "none" in argv
    assert f"type=volume,source={BINDING['volume']},target={handoff.SOURCE_ROOT}" in argv
    assert "readonly" not in argv[argv.index("--mount") + 1]
    assert all(v not in repr(argv) for v in VALUES.values())


def test_session_cannot_target_a_legacy_volume():
    session = {"version": 1, "nonce": "a" * 32, "binding": copy.deepcopy(BINDING), "status": "created"}
    session["binding"]["volume"] = "water-agent_app_credentials"
    with pytest.raises(handoff.HandoffError):
        entry.validate_session(session)


def test_secret_entry_refuses_noninteractive_without_calling_getpass(monkeypatch):
    monkeypatch.setattr(entry.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(entry.getpass, "getpass", lambda *_a, **_k: pytest.fail("prompted"))
    with pytest.raises(handoff.HandoffError, match="HANDOFF_TTY_REQUIRED"):
        entry.read_values()


class SourceTransport:
    def __init__(self, fail_cleanup=False):
        self.prefix = ["docker"]
        self.env = {}
        self.removed = False
        self.volume_removed = False
        self.fail_cleanup = fail_cleanup
        self.commands = []
    def call(self, args):
        self.commands.append(args)
        if args[0] == "ps":
            return b"" if self.removed else ("c" * 64).encode()
        if args[0] == "rm":
            if self.fail_cleanup:
                raise OSError("EntrySyntheticNewChat")
            self.removed = True
            return b""
        if args[:2] == ["volume", "inspect"]:
            return json.dumps({handoff.LABEL: "a" * 32}).encode()
        if args[:2] == ["volume", "ls"]:
            return b"" if self.volume_removed else BINDING["volume"].encode()
        if args[:2] == ["volume", "rm"]:
            self.volume_removed = True
            return b""
        raise AssertionError(args)


@pytest.mark.parametrize("failure", ["timeout", "cancel", "nonzero"])
def test_submitting_failure_cleans_pinned_container_and_owned_volume(monkeypatch, failure):
    docker = SourceTransport()
    monkeypatch.setattr(entry, "verify_entry", lambda *_a, **_k: "c" * 64)
    def run(*_a, **_k):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("docker", 30)
        if failure == "cancel":
            raise KeyboardInterrupt
        return SimpleNamespace(returncode=2)
    monkeypatch.setattr(entry.subprocess, "run", run)
    with pytest.raises((subprocess.TimeoutExpired, KeyboardInterrupt, handoff.HandoffError)):
        entry.submit_values(docker, {"binding": BINDING, "nonce": "a" * 32}, lambda: dict(VALUES))
    assert docker.removed and docker.volume_removed


def test_submission_binds_immutable_id_and_rechecks_after_prompt(monkeypatch):
    docker = SourceTransport()
    checks = []
    def verify(_docker, _session, *, container_id=None):
        checks.append(container_id)
        return "c" * 64
    monkeypatch.setattr(entry, "verify_entry", verify)
    def run(args, **kwargs):
        assert checks == [None, "c" * 64] and args[-1] == "c" * 64
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(entry.subprocess, "run", run)
    entry.submit_values(docker, {"binding": BINDING, "nonce": "a" * 32}, lambda: dict(VALUES))
    assert docker.removed and not docker.volume_removed


def test_cleanup_failure_never_reports_source_ready(monkeypatch):
    docker = SourceTransport(fail_cleanup=True)
    monkeypatch.setattr(entry, "verify_entry", lambda *_a, **_k: "c" * 64)
    monkeypatch.setattr(entry.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    with pytest.raises(handoff.HandoffError, match="HANDOFF_CLEANUP_FAILED"):
        entry.submit_values(docker, {"binding": BINDING, "nonce": "a" * 32}, lambda: dict(VALUES))
